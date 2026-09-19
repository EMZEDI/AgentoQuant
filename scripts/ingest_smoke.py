"""Quick smoke check of the ingest orchestrator: no network, real DuckDB ledger."""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentoquant.data import ConnectorResult, SourceError, Transport
from agentoquant.data.cache import Cache
from agentoquant.data.ingest import (
    CONFIRMATION_SOURCES,
    CRITICAL_SOURCES,
    IngestReport,
    RunContext,
    configured_universe,
    run_snapshot,
)
from agentoquant.data.quota_manager import QuotaExceeded, QuotaManager
from agentoquant.ledger.store import LedgerStore

tmp = Path(tempfile.mkdtemp(prefix="ingest-smoke-"))
ledger = LedgerStore(db_path=tmp / "ledger.duckdb")
quota = QuotaManager(journal_path=tmp / "quota.jsonl", persist=False)


def fake_transport() -> Transport:
    return Transport(quota=quota, cache=Cache(path=tmp / "cache"))


def ok(source: str, series: str, **fields) -> ConnectorResult:
    return ConnectorResult(
        source=source,
        coin_or_series=series,
        fields={"price": 1.0, **fields},
        quota_remaining=42,
        is_stale=False,
    )


def good_kraken(transport, ctx):
    return [ok("kraken_rest", c) for c in ctx.symbols]


def good_gold(transport, ctx):
    return [ok("gold_api", "gold_spot_xau", price=4380.0)]


def bad_twelve(transport, ctx):
    raise SourceError("twelve_data", "TWELVE_DATA_API_KEY is not configured")


def quota_blown(transport, ctx):
    raise QuotaExceeded("alpha_vantage", "day", 25, 25, 3600.0)


def good_coingecko(transport, ctx):
    return [ok("coingecko", "market_global")]


Runners = dict


# --- Case 1: everything up except one confirmation source -----------------------------
registry = {
    "kraken_rest": good_kraken,
    "gold_api": good_gold,
    "twelve_data": bad_twelve,
    "coingecko": good_coingecko,
    "alpha_vantage": quota_blown,
}
report = run_snapshot(
    cycle_id="2026-09-19T12Z-0001",
    symbols=["BTC", "ETH"],
    ledger=ledger,
    transport=fake_transport(),
    runners=registry,
    quota=quota,
)

print("=== case 1: a critical source (twelve_data) is down ===")
print("\n".join(report.summary_lines()))
print()
print("hold_only:", report.hold_only)
print("reasons:", report.hold_only_reasons)
print("missing:", report.missing_sources)
print("records written:", len(report.record_ids))
assert report.hold_only, "twelve_data is critical, so the cycle must be hold-only"
assert "twelve_data" in report.missing_sources
assert "alpha_vantage" in report.missing_sources
assert report.series("gold_spot_xau") is not None
assert report.outcome_for("alpha_vantage").error.startswith("quota for 'alpha_vantage'")

rows = ledger.query('SELECT * FROM "raw_snapshot" WHERE "cycle_id" = ?', ["2026-09-19T12Z-0001"])
stale = [r for r in rows if r["is_stale"]]
print(f"\nledger: {len(rows)} raw_snapshot rows, {len(stale)} stale")
assert len(rows) == len(report.record_ids)
assert len(stale) == 2, f"expected the two missing sources to be recorded stale, got {len(stale)}"

# --- Case 2: everything up ------------------------------------------------------------
healthy = {
    "kraken_rest": good_kraken,
    "gold_api": good_gold,
    "twelve_data": lambda t, c: [ok("twelve_data", "gold_spot_xau_td")],
}
report2 = run_snapshot(
    cycle_id="2026-09-19T13Z-0001",
    symbols=["BTC", "ETH"],
    ledger=ledger,
    transport=fake_transport(),
    runners=healthy,
    quota=quota,
)
print("\n=== case 2: every critical source up ===")
print("\n".join(report2.summary_lines()))
print()
print("hold_only:", report2.hold_only)
assert not report2.hold_only, "nothing critical is missing, so the cycle must be tradable"
assert not report2.missing_sources

# --- Case 3: the configured universe and the source map -------------------------------
universe = configured_universe()
print("\n=== case 3: config-driven universe ===")
print("universe:", universe)
assert universe == ["BTC", "ETH", "SOL", "VVV", "TAO", "RENDER", "AKT"], universe
assert set(CRITICAL_SOURCES) <= set(registry) | {"twelve_data"}
print("critical sources:", CRITICAL_SOURCES)
print("confirmation sources:", CONFIRMATION_SOURCES)

print("\nSMOKE OK")
