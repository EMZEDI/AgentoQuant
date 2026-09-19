"""Early-signal listeners (Task 4): exchange listing feeds, on-chain webhooks, GitHub releases,
Google News headlines, Telegram previews and the runner that supervises them all.

Task 4 acceptance criteria (``tasks/todo.md``):
  - a new Kraken or Bybit listing appears in the ledger within one minute of the announcement
  - on-chain webhook events arrive and are attributed to a token within two blocks
  - every early signal carries a source class (exchange, on-chain, official handle, release,
    headline) for the Verifier

Task 4 verification steps:
  - replay of three past listing days shows the signal ahead of the first article
  - one week of listener uptime with restarts logged

Every test here is offline. HTTP goes through a local recording transport that replays canned
bodies (``RecordingTransport``); ledger writes go to a DuckDB file under ``tmp_path``. Nothing
places an order, moves funds, or speaks MTProto - the listeners are read-only by construction.

GAP, REPORTED NOT HIDDEN: the on-chain receiver measures and returns a per-event block lag but
nothing enforces the "within two blocks" bound, and the frozen ``EarlySignalPayload`` has no
block column (the block number lives inside ``raw_text_or_ref``). See
``test_onchain_block_lag_is_reported_but_not_enforced`` and ``test_onchain_block_is_not_a_ledger_column``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentoquant.data.early_signals import (
    HttpFetcher,
    JsonlLog,
    SignalWriter,
    bybit_listings,
)
from agentoquant.enums import SourceClass
from agentoquant.ledger.store import LedgerStore

# ----------------------------------------------------------------------------------------------
# Offline HTTP: a recording transport plus a fetcher that can only reach it
# ----------------------------------------------------------------------------------------------


class RecordingTransport(httpx.BaseTransport):
    """Records every request and replays a canned response per URL path. Never touches the network.

    ``routes`` maps a URL path to ``(status, body)``; ``body`` may be a str (sent as text), bytes or
    a JSON-serialisable object.
    """

    def __init__(self, routes: dict[str, tuple[int, Any]] | None = None) -> None:
        self.routes: dict[str, tuple[int, Any]] = dict(routes or {})
        self.requests: list[httpx.Request] = []

    def add(self, path: str, status: int, body: Any) -> RecordingTransport:
        self.routes[path] = (status, body)
        return self

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"error": f"no canned route for {request.url.path}"})
        status, body = route
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    @property
    def query_params(self) -> list[dict[str, str]]:
        return [dict(request.url.params) for request in self.requests]


def make_fetcher(transport: RecordingTransport, *, base_url: str = "", **kwargs: Any) -> HttpFetcher:
    """An :class:`HttpFetcher` whose only client is the canned transport."""
    return HttpFetcher(
        base_url=base_url,
        client=httpx.Client(transport=transport),
        rate_limiter=None,
        max_retries=int(kwargs.pop("max_retries", 1)),
        sleep=lambda _seconds: None,
        **kwargs,
    )


# ----------------------------------------------------------------------------------------------
# Ledger helpers
# ----------------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Any:
    """A real DuckDB ledger under ``tmp_path``: the repo's ``data/ledger.duckdb`` is never touched."""
    ledger = LedgerStore(db_path=tmp_path / "ledger.duckdb")
    yield ledger
    ledger.close()


@pytest.fixture
def log(tmp_path: Path) -> JsonlLog:
    """The listener JSON-lines log, under ``tmp_path`` rather than ``logs/``."""
    return JsonlLog(tmp_path / "early_signals.jsonl")


@pytest.fixture
def writer(store: LedgerStore, log: JsonlLog) -> SignalWriter:
    return SignalWriter(store, log=log)


def signal_rows(store: LedgerStore, cycle_id: str | None = None) -> list[dict]:
    """Every ``early_signal`` row, oldest first, optionally filtered to one cycle."""
    sql = (
        'SELECT "record_id" AS record_id, "cycle_id" AS cycle_id, "stage" AS stage, "ts" AS ts, '
        '"producer_role" AS producer_role, "producer_model_family" AS family, "harness" AS harness, '
        '"source_class" AS source_class, "ticker" AS ticker, "event_type" AS event_type, '
        '"raw_text_or_ref" AS raw_text_or_ref, "detected_at" AS detected_at, '
        '"latency_seconds_vs_first_article" AS latency FROM "early_signal"'
    )
    params: list[Any] = []
    if cycle_id is not None:
        sql += ' WHERE "cycle_id" = ?'
        params.append(cycle_id)
    return store.query(sql + ' ORDER BY "ts", "record_id"', params)


def log_events(log: JsonlLog, name: str | None = None) -> list[dict]:
    """Log records, optionally only those with a given ``event`` name."""
    records = log.tail(limit=5000)
    return [record for record in records if name is None or record.get("event") == name]


# ----------------------------------------------------------------------------------------------
# Exchange listing announcements: Bybit and OKX
# ----------------------------------------------------------------------------------------------

BYBIT_LISTING_MS = 1_789_000_000_000  # 2026-09-16T12:26:40Z
BYBIT_PUBLISHED_AT = datetime.fromtimestamp(BYBIT_LISTING_MS / 1000, tz=UTC)
BYBIT_OBSERVED_AT = BYBIT_PUBLISHED_AT + timedelta(seconds=25)

BYBIT_BODY: dict[str, Any] = {
    "retCode": 0,
    "retMsg": "OK",
    "result": {
        "list": [
            {
                "type": {"key": "new_crypto", "title": "New Crypto"},
                "title": "Bybit will list PENGU (PENGU) for spot trading",
                "url": "https://announcements.bybit.com/en-US/article/listing-pengu",
                "publishTime": BYBIT_LISTING_MS,
            },
            {
                "type": {"key": "delistings", "title": "Delistings"},
                "title": "Bybit will delist XYZUSDT perpetual",
                "url": "https://announcements.bybit.com/en-US/article/delist-xyz",
                "publishTime": BYBIT_LISTING_MS + 1000,
            },
            {
                "type": {"key": "product_updates", "title": "Product updates"},
                "title": "Bybit margin tier update",
                "url": "https://announcements.bybit.com/en-US/article/margin",
                "publishTime": BYBIT_LISTING_MS + 2000,
            },
        ]
    },
}


def test_bybit_new_listing_is_an_exchange_announcement_with_a_ticker() -> None:
    events = bybit_listings.parse_bybit_announcements(BYBIT_BODY, observed_at=BYBIT_OBSERVED_AT)
    listings = [event for event in events if event.event_type == "listing"]
    assert len(listings) == 1
    event = listings[0]
    assert event.source_class is SourceClass.EXCHANGE_ANNOUNCEMENT
    assert event.ticker == "PENGU"
    assert event.raw_text_or_ref.endswith("listing-pengu")
    assert event.detected_at == BYBIT_PUBLISHED_AT
    assert event.published_at == BYBIT_PUBLISHED_AT
    assert event.source == "bybit_listings"
    assert event.block_number is None


def test_bybit_delisting_is_policy_and_other_types_are_ignored() -> None:
    events = bybit_listings.parse_bybit_announcements(BYBIT_BODY, observed_at=BYBIT_OBSERVED_AT)
    policies = [event for event in events if event.event_type == "policy"]
    assert len(policies) == 1
    assert policies[0].ticker == "XYZ"
    assert all("margin" not in event.raw_text_or_ref for event in events)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "not-a-mapping",
        {},
        {"retCode": 10001, "retMsg": "params error", "result": {}},
        {"retCode": 0, "result": {"list": []}},
        {"retCode": 0, "result": {"list": ["not-a-dict", {"type": "not-a-dict"}]}},
        {"retCode": 0, "result": {"list": [{"type": {"key": "new_crypto"}, "title": "", "url": ""}]}},
    ],
)
def test_bybit_malformed_and_empty_bodies_yield_no_events(payload: Any) -> None:
    assert bybit_listings.parse_bybit_announcements(payload, observed_at=BYBIT_OBSERVED_AT) == []


def test_bybit_listener_poll_once_writes_both_events_to_the_ledger(
    writer: SignalWriter, log: JsonlLog
) -> None:
    transport = RecordingTransport().add(
        bybit_listings.ANNOUNCEMENTS_PATH, 200, BYBIT_BODY
    )
    listener = bybit_listings.BybitListingsListener(
        writer,
        fetcher=make_fetcher(transport, base_url=bybit_listings.BASE_URL),
        log=log,
        clock=lambda: BYBIT_OBSERVED_AT,
    )
    written = listener.poll_once()

    assert len(written) == 2
    assert transport.paths == [bybit_listings.ANNOUNCEMENTS_PATH]
    assert transport.query_params[0]["locale"] == "en-US"
    rows = signal_rows(writer.store)
    assert {row["event_type"] for row in rows} == {"listing", "policy"}
    assert {row["source_class"] for row in rows} == {SourceClass.EXCHANGE_ANNOUNCEMENT.value}
    assert {row["producer_role"] for row in rows} == {"listener_bybit_listings"}


def test_bybit_listener_poll_raises_on_a_nonzero_retcode_but_poll_once_absorbs_it(
    writer: SignalWriter, log: JsonlLog
) -> None:
    transport = RecordingTransport().add(
        bybit_listings.ANNOUNCEMENTS_PATH, 200, {"retCode": 10001, "retMsg": "params error"}
    )
    listener = bybit_listings.BybitListingsListener(
        writer, fetcher=make_fetcher(transport, base_url=bybit_listings.BASE_URL), log=log
    )
    with pytest.raises(RuntimeError):
        listener.poll()

    assert listener.poll_once() == []
    assert listener.stats.failures == 1
    assert listener.stats.consecutive_failures == 1
    assert signal_rows(writer.store) == []
    assert log_events(log, "poll_failed")


# __SENTINEL__
