"""Per-module tests for ``agentoquant.data.early_signals.onchain_webhooks`` (Task 4, Phase 0).

Offline by construction: no provider is ever contacted. The receiver is driven through its pure
``handle(body, headers)`` entry point, and the one test that exercises the HTTP server binds the loopback
interface the module hard-codes and posts to it over ``httpx`` - that is the only socket in this file.
The ledger is a scratch DuckDB file under ``tmp_path``. No decorators in this file (see the at-sign rule
in ``.hermes.md``): the scratch ledger, the payload builders and the receiver factory are plain helpers.

Acceptance criterion 2 (``tasks/todo.md`` Task 4) - "On-chain webhook events arrive and are attributed to
a token within two blocks" - is verified by
``test_an_onchain_event_is_attributed_with_a_block_lag_inside_two_blocks``: the receiver measures the lag
between the event's block and the chain head, reports it in the response, and has already written the
attributed ledger row by the time the 200 is returned.

GAP, REPORTED NOT HIDDEN: the lag is measured and reported but nothing *enforces* the two-block bound,
and the frozen ``EarlySignalPayload`` has no block column - the block number lives inside
``raw_text_or_ref``. See ``test_a_block_lag_beyond_two_blocks_is_reported_but_not_enforced`` and
``test_the_block_number_is_not_a_ledger_column``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentoquant.data.early_signals import JsonlLog, ListenerStats, SignalWriter
from agentoquant.data.early_signals.onchain_webhooks import (
    ALCHEMY_PATH,
    ALCHEMY_SIGNATURE_HEADER,
    DEFAULT_HOST,
    DEFAULT_REGISTRY_PATH,
    HEALTH_PATH,
    HELIUS_AUTH_HEADER,
    HELIUS_PATH,
    KIND_TOKEN,
    KIND_UNLOCK,
    LOOPBACK_HOSTS,
    TokenRegistry,
    WebhookReceiver,
    alchemy_signature,
    parse_alchemy_webhook,
    parse_helius_webhook,
    parse_webhook,
    resolve_webhook_secret,
    verify_alchemy_signature,
    verify_helius_auth,
    verify_signature,
)
from agentoquant.enums import SourceClass
from agentoquant.ledger.store import DB_PATH_ENV, LedgerStore
from tests.test_early_signals import early_signal_columns, log_events, signal_rows

# ----------------------------------------------------------------------------------------------
# Plain helpers (no fixtures: this file must not emit a decorator)
# ----------------------------------------------------------------------------------------------

OBSERVED_AT = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
SECRET = "test-signing-secret-not-a-real-one"
WATCHED_ADDRESS = "0x1111111111111111111111111111111111111111"
CONTRACT_ADDRESS = "0x9999999999999999999999999999999999999999"
UNKNOWN_ADDRESS = "0xffffffffffffffffffffffffffffffffffffffff"
SOL_MINT = "So11111111111111111111111111111111111111112"
BLOCK_HEX = "0x140a1b2"
BLOCK = 21012914
SLOT = 250000000

#: Known answer for :func:`alchemy_signature` (HMAC-SHA256, key "signing-key"), computed independently
#: with the standard library. A change to the signature construction fails this test.
SIGNED_BODY = b'{"type": "ADDRESS_ACTIVITY", "event": {"network": "ETH_MAINNET"}}'
SIGNED_BODY_DIGEST = "b46d36e1114402fed063c253a3765c9557dd7630b5bcfe87a6d20a25aa7e01a8"


def scratch_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> LedgerStore:
    """A real DuckDB ledger under ``tmp_path``, plus the env override for implicit stores."""
    monkeypatch.setenv(DB_PATH_ENV, str(tmp_path / "env-ledger.duckdb"))
    return LedgerStore(db_path=tmp_path / "ledger.duckdb")


def writer_for(store: LedgerStore, tmp_path: Any) -> SignalWriter:
    return SignalWriter(store, log=JsonlLog(tmp_path / "early_signals.jsonl"))


def registry_with(mapping: dict[str, Any]) -> TokenRegistry:
    """A registry from an address -> ticker/kind mapping."""
    return TokenRegistry.from_mapping(mapping)


def alchemy_payload(
    *,
    to_address: str = WATCHED_ADDRESS,
    block: Any = BLOCK_HEX,
    created: Any = "2026-09-16T12:00:00.000Z",
) -> dict[str, Any]:
    """An Alchemy Address Activity body in the documented shape."""
    return {
        "type": "ADDRESS_ACTIVITY",
        "event": {
            "network": "ETH_MAINNET",
            "activity": [
                {
                    "fromAddress": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "toAddress": to_address,
                    "blockNum": block,
                    "hash": "0xdeadbeef",
                    "value": 1.5,
                    "asset": "VVV",
                    "category": "token",
                    "rawContract": {"address": CONTRACT_ADDRESS, "decimal": "0x12", "value": "0x0"},
                }
            ],
        },
        "createdAt": created,
    }


def helius_payload(*, mint: str = SOL_MINT, tx_type: str = "TRANSFER", slot: Any = SLOT) -> list[dict[str, Any]]:
    """A Helius enhanced-transaction body in the documented shape."""
    return [
        {
            "signature": "5xSignatureExample",
            "slot": slot,
            "type": tx_type,
            "description": "1.2 SOL transferred",
            "tokenTransfers": [
                {"mint": mint, "fromUserAccount": "Aaaa", "toUserAccount": "Bbbb", "tokenAmount": 1.2}
            ],
            "nativeTransfers": [{"fromUserAccount": "Aaaa", "toUserAccount": "Bbbb", "amount": 1200000}],
        }
    ]


def make_receiver(
    writer: SignalWriter,
    *,
    provider: str = "alchemy",
    registry: TokenRegistry | None = None,
    block_height: int | None = None,
    **kwargs: Any,
) -> WebhookReceiver:
    """A receiver with an explicit secret and, optionally, a chain-head probe."""
    if registry is not None:
        kwargs["registry"] = registry
    if block_height is not None:
        kwargs["block_height_provider"] = lambda: block_height
    return WebhookReceiver(writer, provider=provider, secret=SECRET, **kwargs)


def signed_headers(body: bytes, *, provider: str = "alchemy") -> dict[str, str]:
    """The header the provider would send for ``body``."""
    if provider == "alchemy":
        return {ALCHEMY_SIGNATURE_HEADER: alchemy_signature(body, SECRET)}
    return {HELIUS_AUTH_HEADER: SECRET}


def wait_for(predicate: Any, *, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it is true or the deadline passes (loopback server readiness)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ----------------------------------------------------------------------------------------------
# Signature verification
# ----------------------------------------------------------------------------------------------


def test_alchemy_signature_is_hmac_sha256_of_the_raw_body() -> None:
    assert alchemy_signature(SIGNED_BODY, "signing-key") == SIGNED_BODY_DIGEST
    assert alchemy_signature(SIGNED_BODY, "signing-key") == hmac.new(
        b"signing-key", SIGNED_BODY, hashlib.sha256
    ).hexdigest()
    assert alchemy_signature(SIGNED_BODY + b" ", "signing-key") != SIGNED_BODY_DIGEST
    assert alchemy_signature(SIGNED_BODY, "other-key") != SIGNED_BODY_DIGEST
    assert len(SIGNED_BODY_DIGEST) == 64


def test_verify_alchemy_signature_accepts_only_the_right_digest() -> None:
    good = alchemy_signature(SIGNED_BODY, SECRET)
    assert verify_alchemy_signature(SIGNED_BODY, good, SECRET) is True
    assert verify_alchemy_signature(SIGNED_BODY, good.upper(), SECRET) is True  # case-insensitive
    assert verify_alchemy_signature(SIGNED_BODY, f"  {good}  ", SECRET) is True
    assert verify_alchemy_signature(SIGNED_BODY, good[:-1] + "0", SECRET) is False
    assert verify_alchemy_signature(SIGNED_BODY, "", SECRET) is False
    assert verify_alchemy_signature(SIGNED_BODY, None, SECRET) is False
    assert verify_alchemy_signature(SIGNED_BODY, good, "") is False  # fail closed on a missing key
    assert verify_alchemy_signature(SIGNED_BODY, None, "") is False


def test_verify_helius_auth_accepts_the_echoed_header() -> None:
    assert verify_helius_auth(SECRET, SECRET) is True
    assert verify_helius_auth(f"Bearer {SECRET}", SECRET) is True
    assert verify_helius_auth(f"bearer {SECRET}", SECRET) is True
    assert verify_helius_auth("wrong", SECRET) is False
    assert verify_helius_auth(None, SECRET) is False
    assert verify_helius_auth(SECRET, "") is False


def test_verify_signature_dispatches_per_provider_and_fails_closed() -> None:
    body = json.dumps(alchemy_payload()).encode("utf-8")
    good = {ALCHEMY_SIGNATURE_HEADER: alchemy_signature(body, SECRET)}
    assert verify_signature("alchemy", body, good, SECRET) is True
    assert verify_signature("helius", body, {HELIUS_AUTH_HEADER: SECRET}, SECRET) is True
    # A valid Alchemy signature is not valid for Helius, and an unknown provider never passes.
    assert verify_signature("helius", body, good, SECRET) is False
    assert verify_signature("unknown", body, good, SECRET) is False
    assert verify_signature("alchemy", body, good, None) is False
    assert verify_signature("alchemy", body, good, "") is False
    assert verify_signature("alchemy", body, {}, SECRET) is False


def test_resolve_webhook_secret_reports_nothing_rather_than_guessing(monkeypatch: Any) -> None:
    monkeypatch.delenv("AGENTOQUANT_ALCHEMY_WEBHOOK_SIGNING_KEY", raising=False)
    monkeypatch.delenv("AGENTOQUANT_HELIUS_WEBHOOK_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("agentoquant.data.early_signals.onchain_webhooks.credentials", dict)
    assert resolve_webhook_secret("alchemy") is None
    assert resolve_webhook_secret("helius") is None
    assert resolve_webhook_secret("not-a-provider") is None
    monkeypatch.setenv("AGENTOQUANT_ALCHEMY_WEBHOOK_SIGNING_KEY", " from-env ")
    assert resolve_webhook_secret("alchemy") == "from-env"


# ----------------------------------------------------------------------------------------------
# The token registry
# ----------------------------------------------------------------------------------------------


def test_registry_normalises_and_types_its_entries() -> None:
    registry = TokenRegistry.from_mapping(
        {
            WATCHED_ADDRESS.upper(): "vvv",
            SOL_MINT: {"ticker": "render", "kind": KIND_UNLOCK},
            "   ": "ignored",
            UNKNOWN_ADDRESS: {"kind": KIND_TOKEN},
            "0x3": 42,
        }
    )
    assert len(registry) == 2
    assert registry.lookup(WATCHED_ADDRESS) == {"ticker": "VVV", "kind": KIND_TOKEN}
    assert registry.lookup(WATCHED_ADDRESS.upper()) == {"ticker": "VVV", "kind": KIND_TOKEN}
    assert registry.lookup(SOL_MINT) == {"ticker": "RENDER", "kind": KIND_UNLOCK}
    assert registry.lookup(UNKNOWN_ADDRESS) is None
    assert registry.lookup(None) is None
    assert registry.lookup(42) is None


def test_registry_load_reads_the_file_and_survives_garbage(tmp_path: Any) -> None:
    path = tmp_path / "token_registry.json"
    path.write_text(json.dumps({"tokens": {WATCHED_ADDRESS: "VVV"}}), encoding="utf-8")
    assert TokenRegistry.load(path).lookup(WATCHED_ADDRESS) == {"ticker": "VVV", "kind": KIND_TOKEN}

    path.write_text(json.dumps({WATCHED_ADDRESS: {"ticker": "TAO"}}), encoding="utf-8")
    assert TokenRegistry.load(path).lookup(WATCHED_ADDRESS) == {"ticker": "TAO", "kind": KIND_TOKEN}

    for body in ["not json", "[]", '"a string"', "42"]:
        path.write_text(body, encoding="utf-8")
        assert len(TokenRegistry.load(path)) == 0, body

    assert len(TokenRegistry.load(tmp_path / "missing.json")) == 0
    assert DEFAULT_REGISTRY_PATH.endswith("token_registry.json")


def test_registry_attribute_takes_the_first_match() -> None:
    registry = registry_with({WATCHED_ADDRESS: "VVV", CONTRACT_ADDRESS: "TAO"})
    assert registry.attribute(None, WATCHED_ADDRESS, CONTRACT_ADDRESS) == {
        "ticker": "VVV",
        "kind": KIND_TOKEN,
    }
    assert registry.attribute(CONTRACT_ADDRESS, WATCHED_ADDRESS) == {"ticker": "TAO", "kind": KIND_TOKEN}
    assert registry.attribute(None, 42) is None
    assert len(TokenRegistry()) == 0


# ----------------------------------------------------------------------------------------------
# Alchemy payloads
# ----------------------------------------------------------------------------------------------


def test_parse_alchemy_webhook_reads_the_documented_shape() -> None:
    registry = registry_with({WATCHED_ADDRESS: "VVV"})
    events = parse_alchemy_webhook(alchemy_payload(), observed_at=OBSERVED_AT, registry=registry)

    assert len(events) == 1
    event = events[0]
    assert event.source_class is SourceClass.ONCHAIN
    assert event.event_type == "large_transfer"
    assert event.ticker == "VVV"
    assert event.block_number == BLOCK
    assert event.detected_at == CREATED_AT
    assert event.observed_at == OBSERVED_AT
    assert event.source == "onchain_webhooks"
    assert event.raw_text_or_ref.startswith(f"alchemy:ETH_MAINNET:0xdeadbeef block={BLOCK} ")
    assert "attributed=VVV" in event.raw_text_or_ref
    assert event.payload().source_class is SourceClass.ONCHAIN


def test_an_unlock_registry_entry_becomes_an_unlock_event() -> None:
    registry = registry_with({WATCHED_ADDRESS: {"ticker": "VVV", "kind": KIND_UNLOCK}})
    events = parse_alchemy_webhook(alchemy_payload(), observed_at=OBSERVED_AT, registry=registry)
    assert events[0].event_type == "unlock"
    assert events[0].ticker == "VVV"


def test_an_unattributed_transfer_is_recorded_not_dropped() -> None:
    """An address that is not in the registry is still evidence; it is written with no ticker."""
    events = parse_alchemy_webhook(
        alchemy_payload(to_address=UNKNOWN_ADDRESS), observed_at=OBSERVED_AT, registry=registry_with()
    )
    assert len(events) == 1
    assert events[0].ticker is None
    assert events[0].event_type == "large_transfer"
    assert "attributed=unattributed" in events[0].raw_text_or_ref
    assert events[0].block_number == BLOCK


def test_parse_alchemy_webhook_handles_malformed_empty_and_wrong_type_bodies() -> None:
    registry = registry_with({WATCHED_ADDRESS: "VVV"})
    for label, payload in [
        ("none", None),
        ("list", []),
        ("string", "nope"),
        ("int", 42),
        ("empty dict", {}),
        ("no activity", {"type": "ADDRESS_ACTIVITY", "event": {"network": "ETH_MAINNET"}}),
        ("empty activity", {"type": "ADDRESS_ACTIVITY", "event": {"activity": []}}),
        ("activity is not a list", {"type": "ADDRESS_ACTIVITY", "event": {"activity": "nope"}}),
        ("activity items are not dicts", {"type": "ADDRESS_ACTIVITY", "event": {"activity": [1, None]}}),
        ("event is not a dict", {"type": "ADDRESS_ACTIVITY", "event": "nope", "network": "ETH_MAINNET"}),
    ]:
        assert parse_alchemy_webhook(payload, observed_at=OBSERVED_AT, registry=registry) == [], label


def test_an_unknown_alchemy_shape_is_recorded_rather_than_lost() -> None:
    """A provider schema change must show up in the ledger, not vanish."""
    payload = {"type": "GRAPHQL", "event": {"network": "ETH_MAINNET", "data": {"block": {"number": BLOCK_HEX}}}}
    events = parse_alchemy_webhook(payload, observed_at=OBSERVED_AT, registry=registry_with())
    assert len(events) == 1
    assert events[0].ticker is None
    assert events[0].event_type == "large_transfer"
    assert events[0].block_number == BLOCK
    assert "unparsed-GRAPHQL" in events[0].raw_text_or_ref
    assert '"type": "GRAPHQL"' in events[0].raw_text_or_ref


def test_alchemy_block_numbers_accept_int_hex_and_garbage() -> None:
    registry = registry_with({WATCHED_ADDRESS: "VVV"})
    for raw, expected in [(BLOCK_HEX, BLOCK), (BLOCK, BLOCK), (str(BLOCK), BLOCK), (None, None),
                          ("", None), ("0xzz", None), ("not-a-block", None), (True, None)]:
        events = parse_alchemy_webhook(
            alchemy_payload(block=raw), observed_at=OBSERVED_AT, registry=registry
        )
        assert events[0].block_number == expected, raw
        if expected is None:
            assert " block=" not in events[0].raw_text_or_ref


# ----------------------------------------------------------------------------------------------
# Helius payloads
# ----------------------------------------------------------------------------------------------


def test_parse_helius_webhook_reads_the_documented_shape() -> None:
    registry = registry_with({SOL_MINT: {"ticker": "RENDER", "kind": KIND_TOKEN}})
    events = parse_helius_webhook(helius_payload(), observed_at=OBSERVED_AT, registry=registry)

    assert len(events) == 1
    event = events[0]
    assert event.source_class is SourceClass.ONCHAIN
    assert event.event_type == "large_transfer"
    assert event.ticker == "RENDER"
    assert event.block_number == SLOT
    assert event.detected_at == OBSERVED_AT  # Helius states no event time in this shape
    assert event.raw_text_or_ref.startswith(f"helius:SOLANA:5xSignatureExample block={SLOT} ")
    assert "transfers=2" in event.raw_text_or_ref
    assert "attributed=RENDER" in event.raw_text_or_ref


def test_helius_unlock_transaction_types_become_unlock_events() -> None:
    """The transaction type is a hint even when the registry does not know the account."""
    events = parse_helius_webhook(
        helius_payload(mint=UNKNOWN_ADDRESS, tx_type="TOKEN_UNLOCK"), observed_at=OBSERVED_AT
    )
    assert events[0].event_type == "unlock"
    assert events[0].ticker is None
    assert "attributed=unattributed" in events[0].raw_text_or_ref

    vesting = parse_helius_webhook(helius_payload(tx_type="VESTING"), observed_at=OBSERVED_AT)
    assert vesting[0].event_type == "unlock"


def test_parse_helius_webhook_handles_malformed_empty_and_wrong_type_bodies() -> None:
    registry = registry_with({SOL_MINT: "RENDER"})
    assert parse_helius_webhook(None, observed_at=OBSERVED_AT, registry=registry) == []
    assert parse_helius_webhook([], observed_at=OBSERVED_AT, registry=registry) == []
    assert parse_helius_webhook([1, "nope", None], observed_at=OBSERVED_AT, registry=registry) == []

    # A single transaction object (not wrapped in a list) is still parsed.
    single = parse_helius_webhook(
        helius_payload()[0], observed_at=OBSERVED_AT, registry=registry
    )
    assert len(single) == 1
    assert single[0].ticker == "RENDER"

    # A transaction with no transfers at all is still recorded, unattributed.
    bare = parse_helius_webhook(
        [{"signature": "sig", "slot": SLOT, "type": "TRANSFER"}], observed_at=OBSERVED_AT
    )
    assert len(bare) == 1
    assert bare[0].ticker is None
    assert "transfers=0" in bare[0].raw_text_or_ref
    assert parse_helius_webhook([{"tokenTransfers": "nope", "nativeTransfers": 7}])[0].block_number is None


def test_parse_webhook_dispatches_per_provider() -> None:
    registry = registry_with({WATCHED_ADDRESS: "VVV"})
    assert len(parse_webhook("alchemy", alchemy_payload(), registry=registry)) == 1
    assert len(parse_webhook("helius", helius_payload(), registry=registry)) == 1
    assert parse_webhook("unknown", alchemy_payload(), registry=registry) == []
    assert parse_webhook("alchemy", None, registry=registry) == []


# ----------------------------------------------------------------------------------------------
# The receiver: fail-closed construction
# ----------------------------------------------------------------------------------------------


def test_the_receiver_refuses_to_start_without_a_signing_secret(tmp_path: Any, monkeypatch: Any) -> None:
    """An unauthenticated webhook receiver would accept forged events."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    monkeypatch.delenv("AGENTOQUANT_ALCHEMY_WEBHOOK_SIGNING_KEY", raising=False)
    monkeypatch.setattr("agentoquant.data.early_signals.onchain_webhooks.credentials", dict)
    with pytest.raises(RuntimeError, match="refusing to start unauthenticated"):
        WebhookReceiver(writer, provider="alchemy")


def test_the_receiver_refuses_a_non_loopback_bind_and_an_unknown_provider(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    with pytest.raises(ValueError, match="loopback only"):
        make_receiver(writer, host="example.invalid")
    with pytest.raises(ValueError, match="unknown webhook provider"):
        make_receiver(writer, provider="quicknode")
    assert DEFAULT_HOST in LOOPBACK_HOSTS
    assert {"::1", "localhost"} <= LOOPBACK_HOSTS


def test_the_receiver_has_the_same_surface_as_a_listener(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    receiver = make_receiver(writer_for(store, tmp_path))
    assert receiver.name == "onchain_webhooks"
    assert isinstance(receiver.stats, ListenerStats)
    assert callable(receiver.run)
    assert receiver.path == ALCHEMY_PATH
    assert make_receiver(writer_for(store, tmp_path), provider="helius").path == HELIUS_PATH


# ----------------------------------------------------------------------------------------------
# The receiver: the request path
# ----------------------------------------------------------------------------------------------


def test_a_valid_webhook_is_written_and_reports_its_counters(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    receiver = make_receiver(
        writer,
        registry=registry_with({WATCHED_ADDRESS: "VVV"}),
        block_height=BLOCK + 1,
        log=writer.log,
    )
    body = json.dumps(alchemy_payload()).encode("utf-8")

    result = receiver.handle(body, signed_headers(body))

    assert result.status == 200
    assert result.body["provider"] == "alchemy"
    assert result.body["events"] == 1
    assert result.body["written"] == 1
    assert result.body["attributed"] == 1
    assert result.body["unattributed"] == 0
    assert result.body["block_lags"] == [1]
    rows = signal_rows(store)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "VVV"
    assert rows[0]["source_class"] == SourceClass.ONCHAIN.value
    assert rows[0]["event_type"] == "large_transfer"
    assert rows[0]["producer_role"] == "listener_onchain_webhooks"
    assert receiver.stats.polls == 1
    assert receiver.stats.written == 1
    assert receiver.stats.consecutive_failures == 0
    assert log_events(writer.log, "webhook_accepted")[0]["attributed"] == 1


def test_a_second_identical_webhook_is_deduplicated(tmp_path: Any, monkeypatch: Any) -> None:
    """Providers retry; the same event must not inflate the Verifier's corroboration count."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    receiver = make_receiver(writer, registry=registry_with({WATCHED_ADDRESS: "VVV"}))
    body = json.dumps(alchemy_payload()).encode("utf-8")

    assert receiver.handle(body, signed_headers(body)).body["written"] == 1
    retry = receiver.handle(body, signed_headers(body))

    assert retry.status == 200
    assert retry.body["written"] == 0
    assert retry.body["unattributed"] == 0
    assert len(signal_rows(store)) == 1
    assert receiver.stats.duplicates == 1


def test_a_bad_signature_fails_closed_and_writes_nothing(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    receiver = make_receiver(writer, registry=registry_with({WATCHED_ADDRESS: "VVV"}), log=writer.log)
    body = json.dumps(alchemy_payload()).encode("utf-8")

    for label, headers in [
        ("wrong digest", {ALCHEMY_SIGNATURE_HEADER: "0" * 64}),
        ("truncated digest", {ALCHEMY_SIGNATURE_HEADER: alchemy_signature(body, SECRET)[:32]}),
        ("missing header", {}),
        ("unrelated header", {"x-something-else": "1"}),
    ]:
        result = receiver.handle(body, headers)
        assert result.status == 401, label
        assert result.body == {"error": "invalid signature"}
    assert receiver.handle(body, signed_headers(body, provider="helius")).status == 401

    assert signal_rows(store) == []
    assert receiver.stats.written == 0
    assert receiver.stats.failures == 4
    assert receiver.stats.consecutive_failures == 4
    assert receiver.stats.last_error == "signature_missing_or_invalid"
    rejected = log_events(writer.log, "webhook_rejected")
    assert [record["reason"] for record in rejected] == [
        "signature_present_but_invalid",
        "signature_present_but_invalid",
        "signature_missing",
        "signature_missing",
    ]
    # The secret and the presented value are never logged.
    logged = json.dumps(writer.log.tail(limit=100))
    assert SECRET not in logged
    assert "0" * 64 not in logged


def test_a_malformed_body_is_rejected_after_the_signature_is_verified(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    receiver = make_receiver(writer, log=writer.log)
    body = b"<html>not json</html>"

    result = receiver.handle(body, signed_headers(body))

    assert result.status == 400
    assert signal_rows(store) == []
    assert receiver.stats.failures == 1
    assert log_events(writer.log, "webhook_rejected")[0]["reason"] == "malformed_json"


def test_an_oversized_body_is_refused_before_anything_else(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    receiver = make_receiver(writer, max_body_bytes=16, log=writer.log)
    body = json.dumps(alchemy_payload()).encode("utf-8")

    result = receiver.handle(body, signed_headers(body))

    assert result.status == 413
    assert signal_rows(store) == []
    assert receiver.stats.failures == 1
    assert log_events(writer.log, "webhook_rejected")[0]["reason"] == "body_too_large"


def test_an_empty_body_with_a_valid_signature_is_a_malformed_json_rejection(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    receiver = make_receiver(writer)
    result = receiver.handle(b"", signed_headers(b""))
    assert result.status == 400
    assert signal_rows(store) == []


def test_health_reports_the_registry_without_touching_the_ledger(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    receiver = make_receiver(writer_for(store, tmp_path), registry=registry_with({WATCHED_ADDRESS: "VVV"}))
    result = receiver.handle_health()
    assert result.status == 200
    assert result.body["status"] == "ok"
    assert result.body["registry_entries"] == 1
    assert result.body["uptime_since"] is None  # nothing has run yet
    assert signal_rows(store) == []


def test_a_failing_block_height_probe_never_fails_a_webhook(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)

    def explode() -> int:
        raise RuntimeError("rpc down")

    receiver = WebhookReceiver(
        writer,
        provider="alchemy",
        secret=SECRET,
        registry=registry_with({WATCHED_ADDRESS: "VVV"}),
        block_height_provider=explode,
    )
    body = json.dumps(alchemy_payload()).encode("utf-8")
    result = receiver.handle(body, signed_headers(body))
    assert result.status == 200
    assert result.body["block_lags"] == []
    assert len(signal_rows(store)) == 1


# ----------------------------------------------------------------------------------------------
# Acceptance criterion 2: attribution with a block lag inside two blocks
# ----------------------------------------------------------------------------------------------


def test_an_onchain_event_is_attributed_with_a_block_lag_inside_two_blocks(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Acceptance criterion 2: attributed to a token, with a measured lag inside two blocks.

    The chain head is one block past the event, so the lag the receiver reports is 1. The ledger row is
    asserted to exist as soon as ``handle`` returns, i.e. the attribution happened inside the request
    rather than in some later pass - and a regression that stopped attributing would leave
    ``ticker`` NULL while a regression that stopped measuring would leave ``block_lags`` empty.
    """
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    receiver = make_receiver(
        writer,
        registry=registry_with({WATCHED_ADDRESS: "VVV"}),
        block_height=BLOCK + 1,
        log=writer.log,
    )
    body = json.dumps(alchemy_payload()).encode("utf-8")

    result = receiver.handle(body, signed_headers(body))

    assert result.status == 200
    assert result.body["attributed"] == 1
    lags = result.body["block_lags"]
    assert lags == [1]
    assert max(lags) <= 2, "the event must be attributed inside the two-block bound"

    row = signal_rows(store)[0]
    assert row["ticker"] == "VVV"
    assert row["source_class"] == SourceClass.ONCHAIN.value
    assert f"block={BLOCK}" in row["raw_text_or_ref"]
    assert log_events(writer.log, "webhook_accepted")[0]["block_lags"] == [1]


def test_a_block_lag_beyond_two_blocks_is_reported_but_not_enforced(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """GAP, REPORTED NOT HIDDEN: the lag is measured, nothing rejects a late event.

    The frozen ``EarlySignalPayload`` has no block column and no bound, so the receiver cannot enforce
    "within two blocks" - it can only report the lag for the operator and the Verifier. A 50-block lag
    still returns 200 and still reaches the ledger.
    """
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    receiver = make_receiver(
        writer,
        registry=registry_with({WATCHED_ADDRESS: "VVV"}),
        block_height=BLOCK + 50,
    )
    body = json.dumps(alchemy_payload()).encode("utf-8")

    result = receiver.handle(body, signed_headers(body))

    assert result.status == 200
    assert result.body["block_lags"] == [50]
    assert result.body["written"] == 1
    assert len(signal_rows(store)) == 1


def test_the_block_number_is_not_a_ledger_column(tmp_path: Any, monkeypatch: Any) -> None:
    """GAP: the block lives inside ``raw_text_or_ref`` because the payload is frozen."""
    store = scratch_ledger(tmp_path, monkeypatch)
    columns = early_signal_columns(store)
    assert "raw_text_or_ref" in columns
    assert "source_class" in columns
    assert "block_number" not in columns
    assert "block_lag" not in columns


def test_a_helius_webhook_is_attributed_and_reports_its_slot(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    receiver = make_receiver(
        writer,
        provider="helius",
        registry=registry_with({SOL_MINT: "RENDER"}),
        block_height=SLOT + 2,
    )
    body = json.dumps(helius_payload()).encode("utf-8")

    result = receiver.handle(body, {HELIUS_AUTH_HEADER: SECRET})

    assert result.status == 200
    assert result.body["attributed"] == 1
    assert result.body["block_lags"] == [2]
    row = signal_rows(store)[0]
    assert row["ticker"] == "RENDER"
    assert row["source_class"] == SourceClass.ONCHAIN.value


# ----------------------------------------------------------------------------------------------
# The loopback server
# ----------------------------------------------------------------------------------------------


def test_the_loopback_server_serves_the_webhook_health_and_404_paths(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The only socket in this file: the receiver's own loopback listener, on a port the OS picks."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    receiver = make_receiver(
        writer,
        registry=registry_with({WATCHED_ADDRESS: "VVV"}),
        block_height=BLOCK + 1,
        host=DEFAULT_HOST,
        port=0,
        log=writer.log,
    )
    stop = threading.Event()
    thread = threading.Thread(target=receiver.run, args=(stop,), name="webhook-test-server", daemon=True)
    thread.start()
    assert wait_for(lambda: receiver.bound_port != 0), "the receiver did not bind a port"
    base = f"http://{DEFAULT_HOST}:{receiver.bound_port}"
    body = json.dumps(alchemy_payload()).encode("utf-8")

    try:
        accepted = httpx.post(base + ALCHEMY_PATH, content=body, headers=signed_headers(body), timeout=5.0)
        assert accepted.status_code == 200
        # Attribution happened inside the request: the row is readable as soon as the 200 is back.
        assert signal_rows(store)[0]["ticker"] == "VVV"
        assert accepted.json()["block_lags"] == [1]

        forged = httpx.post(
            base + ALCHEMY_PATH, content=body, headers={ALCHEMY_SIGNATURE_HEADER: "0" * 64}, timeout=5.0
        )
        assert forged.status_code == 401
        assert len(signal_rows(store)) == 1

        health = httpx.get(base + HEALTH_PATH, timeout=5.0)
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        wrong_path = httpx.post(base + "/webhooks/quicknode", content=body, timeout=5.0)
        assert wrong_path.status_code == 404
        assert httpx.get(base + "/", timeout=5.0).status_code == 404
    finally:
        stop.set()
        thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert receiver.bound_port != 0  # remembered for the operator's status output
    assert log_events(writer.log, "listener_start")[0]["registry_entries"] == 1
    assert log_events(writer.log, "listener_stop")[0]["written"] == 1


def test_a_bind_failure_is_logged_not_raised(tmp_path: Any, monkeypatch: Any) -> None:
    """A listener must never raise out of run(); a taken port is a logged failure."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    taken = make_receiver(writer_for(store, tmp_path), host=DEFAULT_HOST, port=0)
    stop = threading.Event()
    thread = threading.Thread(target=taken.run, args=(stop,), daemon=True)
    thread.start()
    assert wait_for(lambda: taken.bound_port != 0)
    clash_thread: threading.Thread | None = None
    try:
        clashing = make_receiver(writer, host=DEFAULT_HOST, port=taken.bound_port, log=writer.log)
        clash_stop = threading.Event()
        clash_thread = threading.Thread(target=clashing.run, args=(clash_stop,), daemon=True)
        clash_thread.start()
        assert wait_for(lambda: bool(log_events(writer.log, "listener_start_failed"))), (
            "binding a taken port must log a failure rather than raise"
        )
        assert clashing.stats.failures == 1
        assert clashing.stats.last_error is not None
        assert "bind failed" in clashing.stats.last_error
        clash_stop.set()
        clash_thread.join(timeout=5.0)
    finally:
        stop.set()
        thread.join(timeout=5.0)
        if clash_thread is not None:
            clash_thread.join(timeout=5.0)


def test_the_registry_default_path_is_under_the_gitignored_data_directory() -> None:
    assert DEFAULT_REGISTRY_PATH == str(Path("data") / "early_signals" / "token_registry.json")
