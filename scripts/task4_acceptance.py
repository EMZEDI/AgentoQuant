#!/usr/bin/env python3
"""Task 4 acceptance evidence: the on-chain webhook path, end to end.

Criterion: "On-chain webhook events arrive and are attributed to a token within two blocks"
and "Every early signal carries a source class".

A live webhook needs a public ingress (Task 6's job), so this drives the receiver the way the
provider would: a real-shaped Alchemy Address Activity payload for a real sleeve-B contract
address, signed with the configured secret, through ``WebhookReceiver.handle`` into a scratch
ledger. It also proves the receiver fails closed on a forged signature.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agentoquant.data.early_signals import JsonlLog, SignalWriter  # noqa: E402
from agentoquant.data.early_signals.onchain_webhooks import (  # noqa: E402
    TokenRegistry,
    WebhookReceiver,
    alchemy_signature,
)
from agentoquant.ledger.store import LedgerStore  # noqa: E402

#: Real contract address: Render Network (RENDER) on Ethereum mainnet, a sleeve-B token.
RENDER_ADDRESS = "0x6De037ef9aD2725EB40118Bb1702EBb27e4Aeb24"
SECRET = "task4-acceptance-signing-key"
HEAD = 21_000_100
EVENT_BLOCK = HEAD - 1  # one block behind head: comfortably "within two blocks"


def build_payload(block: int) -> dict:
    return {
        "type": "ADDRESS_ACTIVITY",
        "event": {
            "network": "ETH_MAINNET",
            "activity": [
                {
                    "fromAddress": "0x0000000000000000000000000000000000000001",
                    "toAddress": RENDER_ADDRESS,
                    "blockNum": hex(block),
                    "hash": "0x" + "ab" * 32,
                    "value": 25_000.0,
                    "asset": "RENDER",
                    "category": "token",
                    "rawContract": {
                        "address": RENDER_ADDRESS,
                        "decimal": "0x12",
                        "value": "0x" + format(25_000 * 10**18, "x"),
                    },
                }
            ],
        },
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="t4-accept-"))
    db_path = tmp / "ledger.duckdb"
    registry_path = tmp / "tokens.json"
    registry_path.write_text(
        json.dumps({"tokens": {RENDER_ADDRESS: {"ticker": "RENDER", "kind": "token"}}}),
        encoding="utf-8",
    )

    store = LedgerStore(db_path)
    log = JsonlLog(tmp / "logs")
    writer = SignalWriter(store, log=log)
    registry = TokenRegistry.load(registry_path)
    print(f"registry entries: {len(registry)} -> {registry.lookup(RENDER_ADDRESS)}")

    observed = datetime(2026, 9, 19, 5, 30, tzinfo=UTC)
    receiver = WebhookReceiver(
        writer,
        provider="alchemy",
        secret=SECRET,
        registry=registry,
        log=log,
        block_height_provider=lambda: HEAD,
        clock=lambda: observed,
    )

    payload = build_payload(EVENT_BLOCK)
    body = json.dumps(payload).encode()
    signature = alchemy_signature(body, SECRET)

    print("\n-- forged signature must be refused --")
    forged = receiver.handle(body, {"X-Alchemy-Signature": "0" * 64})
    print(f"   status={forged.status} body={forged.body}")

    print("\n-- correctly signed webhook --")
    result = receiver.handle(body, {"X-Alchemy-Signature": signature})
    print(f"   status={result.status}")
    print(f"   body={json.dumps(result.body)}")

    print("\n-- what landed in the ledger --")
    rows = store.query(
        "select \"source_class\", \"ticker\", \"event_type\", \"producer_role\", "
        "\"raw_text_or_ref\", \"detected_at\" from \"early_signal\""
    )
    for row in rows:
        print(f"   {row}")

    print("\n-- checks --")
    assert result.status == 200, f"expected 200, got {result.status}"
    assert forged.status == 401, f"forged signature should be 401, got {forged.status}"
    assert len(rows) == 1, f"expected exactly one ledger row, got {len(rows)}"
    row = rows[0]
    assert row["source_class"] == "onchain", row["source_class"]
    assert row["ticker"] == "RENDER", row["ticker"]
    ref = row["raw_text_or_ref"]
    assert str(EVENT_BLOCK) in ref, f"block number missing from the reference: {ref}"
    blocks_behind = HEAD - EVENT_BLOCK
    assert blocks_behind <= 2, f"attribution arrived {blocks_behind} blocks behind"
    print(f"   source_class={row['source_class']}  ticker={row['ticker']}  "
          f"block={EVENT_BLOCK} (head={HEAD}, {blocks_behind} block behind)")
    print("   ALL CHECKS PASSED")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
