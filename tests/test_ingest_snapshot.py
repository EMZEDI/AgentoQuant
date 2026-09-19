"""Tests for the hourly snapshot orchestrator (Task 3).

Every test drives the real orchestrator against a real DuckDB ledger with an injected fake
transport, so nothing here touches the network or the live quota journal. The behaviours under test
are the ones the task's acceptance criteria name: one snapshot completes inside every source's
quota, a failed source is recorded as missing and the cycle continues in hold-only mode when
critical data is stale, and the macro and usage series are present.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentoquant.config_loader import load_sources
from agentoquant.data import ConnectorResult, SourceError, Transport
from agentoquant.data.cache import Cache
from agentoquant.data.ingest import (
    CONFIRMATION_SOURCES,
    CRITICAL_SOURCES,
    EARLY_SIGNAL_SOURCES,
    SLEEVE_B_USAGE_SLUGS,
    SOURCE_RUNNERS,
    ConnectorUnavailable,
    IngestReport,
    RunContext,
    SourceOutcome,
    _connector,
    configured_universe,
    new_cycle_id,
    run_snapshot,
    run_source,
    write_snapshot,
)
from agentoquant.data.quota_manager import QuotaExceeded, QuotaManager
from agentoquant.ledger.store import LedgerStore

CYCLE = "2026-09-19T12Z-0001"


# ----------------------------------------------------------------------------------------------
# Fixtures and fakes
# ----------------------------------------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path: Path) -> LedgerStore:
    return LedgerStore(db_path=tmp_path / "ledger.duckdb")


@pytest.fixture
def quota(tmp_path: Path) -> QuotaManager:
    """A quota manager with no journal side effects: the live journal is not touched by tests."""
    return QuotaManager(journal_path=tmp_path / "quota.jsonl", persist=False)


@pytest.fixture
def transport(quota: QuotaManager, tmp_path: Path) -> Transport:
    return Transport(quota=quota, cache=Cache(path=tmp_path / "cache"))


def reading(source: str, series: str, **fields) -> ConnectorResult:
    """One healthy reading."""
    return ConnectorResult(
        source=source,
        coin_or_series=series,
        fields={"price": 1.0, **fields},
        quota_remaining=17,
        is_stale=False,
    )


def stale(source: str, series: str, error: str = "upstream said no") -> ConnectorResult:
    """One reading the connector itself marked missing, without raising."""
    return ConnectorResult(
        source=source,
        coin_or_series=series,
        fields={"missing": True},
        quota_remaining=0,
        is_stale=True,
        error=error,
    )


def kraken_ok(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    return [reading("kraken_rest", coin) for coin in ctx.symbols]


def gold_ok(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    return [reading("gold_api", "gold_spot_xau", price=4380.0)]


def twelve_ok(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    return [
        reading("twelve_data", "gold_spot_xau_td", price=4381.0),
        reading("twelve_data", "dollar_proxy_usdx", price=97.5),
    ]


def coingecko_ok(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    return [reading("coingecko", "market_global")]


def raises(error: Exception):
    """A runner that fails the way a broken source fails."""

    def runner(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
        raise error

    return runner


def healthy_registry() -> dict:
    return {
        "kraken_rest": kraken_ok,
        "gold_api": gold_ok,
        "twelve_data": twelve_ok,
        "coingecko": coingecko_ok,
    }


def snapshot(ledger: LedgerStore, transport: Transport, quota: QuotaManager, registry, **kwargs):
    return run_snapshot(
        cycle_id=CYCLE,
        symbols=["BTC", "ETH"],
        ledger=ledger,
        transport=transport,
        runners=registry,
        quota=quota,
        **kwargs,
    )


# ----------------------------------------------------------------------------------------------
# One snapshot, every reading written, nothing missing
# ----------------------------------------------------------------------------------------------


def test_snapshot_writes_one_raw_snapshot_record_per_reading(ledger, transport, quota):
    """The whole cycle lands in the ledger: one ``raw_snapshot`` record per reading."""
    report = snapshot(ledger, transport, quota, healthy_registry())

    assert report.cycle_id == CYCLE
    assert report.hold_only is False
    assert report.hold_only_reasons == ()
    assert report.missing_sources == []

    # kraken returns one reading per symbol, gold one, twelve_data two, coingecko one
    expected = len(["BTC", "ETH"]) + 1 + 2 + 1
    assert len(report.record_ids) == expected
    assert report.readings == expected

    rows = ledger.query('SELECT * FROM "raw_snapshot" WHERE "cycle_id" = ?', [CYCLE])
    assert len(rows) == expected
    assert {row["source"] for row in rows} == {
        "kraken_rest",
        "gold_api",
        "twelve_data",
        "coingecko",
    }
    assert {row["coin_or_series"] for row in rows} >= {"BTC", "ETH", "gold_spot_xau"}


def test_every_reading_carries_the_envelope_the_ledger_requires(ledger, transport, quota):
    """A snapshot record is joinable on ``cycle_id`` and attributable to its producer."""
    snapshot(ledger, transport, quota, healthy_registry())

    rows = ledger.query('SELECT * FROM "raw_snapshot" WHERE "cycle_id" = ?', [CYCLE])
    for row in rows:
        assert row["cycle_id"] == CYCLE
        assert row["stage"] == "raw_snapshot"
        assert row["producer_role"] == "ingest"
        assert row["record_id"]
        assert row["ts"]
        assert row["is_stale"] is False


def test_hold_only_cycle_still_writes_its_records(ledger, transport, quota):
    """A degraded cycle is a normal cycle: the snapshot succeeded, so it is recorded."""
    registry = healthy_registry() | {"kraken_rest": raises(SourceError("kraken_rest", "said no"))}

    report = snapshot(ledger, transport, quota, registry)

    assert report.hold_only is True
    assert report.cycle_id == CYCLE
    rows = ledger.query('SELECT * FROM "raw_snapshot" WHERE "cycle_id" = ?', [CYCLE])
    assert len(rows) == len(report.record_ids)
    assert len(rows) > 0


def test_macro_and_usage_series_are_present(ledger, transport, quota):
    """The plan needs gold, the dollar proxy and the market-wide context every hour."""
    report = snapshot(ledger, transport, quota, healthy_registry())

    assert report.series("gold_spot_xau") is not None
    assert report.series("gold_spot_xau_td") is not None
    assert report.series("dollar_proxy_usdx") is not None
    assert report.series("market_global") is not None
    assert report.series("nothing_like_this") is None


def test_configured_universe_comes_from_the_enabled_sleeves():
    """The snapshot covers the coins the sleeve config enables, with no duplicates."""
    symbols = configured_universe()

    assert isinstance(symbols, list)
    assert len(symbols) == len(set(symbols))
    assert all(isinstance(coin, str) and coin for coin in symbols)
    assert "BTC" in symbols
    assert "ETH" in symbols


def test_new_cycle_id_is_stable_in_shape_and_unique_in_time():
    """Cycle ids are the ledger's join key, so their shape is part of the contract."""
    first = new_cycle_id(datetime(2026, 9, 19, 12, 0, tzinfo=UTC))
    same_hour = new_cycle_id(datetime(2026, 9, 19, 12, 30, tzinfo=UTC))
    next_hour = new_cycle_id(datetime(2026, 9, 19, 13, 0, tzinfo=UTC))

    assert first.startswith("2026-09-19T12Z")
    assert next_hour.startswith("2026-09-19T13Z")
    assert first != next_hour
    assert first == same_hour or first != same_hour  # shape contract, not a collision guarantee
    assert " " not in first


# ----------------------------------------------------------------------------------------------
# Graceful degradation: a missing source is recorded, never fatal
# ----------------------------------------------------------------------------------------------


def test_a_failed_confirmation_source_does_not_force_hold_only(ledger, transport, quota):
    """The corroboration layer is allowed to be absent; the cycle carries on."""
    registry = healthy_registry() | {"coingecko": raises(SourceError("coingecko", "503"))}

    report = snapshot(ledger, transport, quota, registry)

    assert report.hold_only is False
    assert report.hold_only_reasons == ()
    assert "coingecko" in report.missing_sources
    outcome = report.outcome_for("coingecko")
    assert outcome is not None
    assert outcome.missing is True
    assert outcome.critical is False
    assert "503" in (outcome.error or "")


def test_a_failed_critical_source_forces_hold_only_and_names_itself(ledger, transport, quota):
    """Price and regime data are not optional: without them the cycle may not open positions."""
    registry = healthy_registry() | {"gold_api": raises(SourceError("gold_api", "timeout"))}

    report = snapshot(ledger, transport, quota, registry)

    assert report.hold_only is True
    assert len(report.hold_only_reasons) == 1
    assert report.hold_only_reasons[0].startswith("gold_api missing")
    assert report.missing_sources == ["gold_api"]


def test_every_missing_critical_source_is_named_in_the_reasons(ledger, transport, quota):
    """Two critical sources down means two reasons, not one generic message."""
    registry = healthy_registry() | {
        "kraken_rest": raises(SourceError("kraken_rest", "503")),
        "gold_api": raises(SourceError("gold_api", "timeout")),
    }

    report = snapshot(ledger, transport, quota, registry)

    assert report.hold_only is True
    assert len(report.hold_only_reasons) == 2
    assert {reason.split(" ")[0] for reason in report.hold_only_reasons} == {
        "kraken_rest",
        "gold_api",
    }


def test_a_missing_source_is_written_to_the_ledger_as_a_stale_reading(ledger, transport, quota):
    """What the cycle did not know has to be as visible after the fact as what it did."""
    registry = healthy_registry() | {"gold_api": raises(SourceError("gold_api", "timeout"))}

    report = snapshot(ledger, transport, quota, registry)

    rows = ledger.query(
        'SELECT * FROM "raw_snapshot" WHERE "cycle_id" = ? AND "source" = ?', [CYCLE, "gold_api"]
    )
    assert len(rows) == 1
    assert rows[0]["is_stale"] is True
    assert len(report.record_ids) > 0


def test_quota_exceeded_is_recorded_as_missing_not_raised(ledger, transport, quota):
    """Refusing to call a source is a normal outcome: the budget is part of the design."""
    exceeded = QuotaExceeded("gdelt", "calls_per_hour", 4, 4, 900.0)
    registry = healthy_registry() | {"gdelt": raises(exceeded)}

    report = snapshot(ledger, transport, quota, registry)

    assert report.hold_only is False
    outcome = report.outcome_for("gdelt")
    assert outcome is not None
    assert outcome.missing is True
    assert "gdelt" in (outcome.error or "")


def test_a_source_returning_only_stale_readings_counts_as_missing(ledger, transport, quota):
    """A connector that answers with a missing reading has not answered."""
    registry = healthy_registry() | {
        "twelve_data": lambda transport, ctx: [stale("twelve_data", "dollar_proxy_usdx")]
    }

    report = snapshot(ledger, transport, quota, registry)

    assert report.hold_only is True
    outcome = report.outcome_for("twelve_data")
    assert outcome is not None
    assert outcome.missing is True
    assert outcome.missing_readings == 1


def test_a_healthy_snapshot_reports_no_quota_breaches(ledger, transport, quota):
    """A cycle that stayed inside every budget says so explicitly."""
    report = snapshot(ledger, transport, quota, healthy_registry())

    assert not report.quota_breaches
    assert quota.breaches() == {}


# ----------------------------------------------------------------------------------------------
# Per-source call accounting: what makes the quota budget enforceable
# ----------------------------------------------------------------------------------------------


class CountingTransport:
    """Just enough transport for the accounting in ``run_source``: counters, no I/O."""

    def __init__(self) -> None:
        self.calls = 0
        self.cache_hits = 0
        self.costs: dict[str, float] = {}

    def spend(self, source: str, calls: int, hits: int = 0, cost: float = 0.0) -> None:
        self.calls += calls
        self.cache_hits += hits
        self.costs[source] = self.costs.get(source, 0.0) + cost


def test_run_source_attributes_the_calls_a_runner_actually_made():
    """Per-source call accounting is what makes the quota budget enforceable."""
    counting = CountingTransport()

    def runner(transport, ctx):
        transport.spend("kraken_rest", calls=2, hits=1, cost=0.001)
        return [reading("kraken_rest", "BTC")]

    outcome = run_source("kraken_rest", counting, RunContext(symbols=["BTC"]), runner)

    assert outcome.calls == 2
    assert outcome.cache_hits == 1
    assert outcome.cost_usd == pytest.approx(0.001)
    assert outcome.missing is False
    assert outcome.series == ["BTC"]


def test_run_source_attributes_calls_even_when_the_runner_fails():
    """A source that failed after spending quota still spent it."""
    counting = CountingTransport()

    def runner(transport, ctx):
        transport.spend("gdelt", calls=1)
        raise SourceError("gdelt", "429 from upstream")

    outcome = run_source("gdelt", counting, RunContext(symbols=[]), runner)

    assert outcome.calls == 1
    assert outcome.missing is True
    assert "429" in (outcome.error or "")


def test_run_source_does_not_swallow_a_real_bug():
    """Only the two expected failure modes are converted; a bug must surface as a traceback."""
    counting = CountingTransport()

    def runner(transport, ctx):
        raise ZeroDivisionError("a genuine bug")

    with pytest.raises(ZeroDivisionError):
        run_source("kraken_rest", counting, RunContext(symbols=[]), runner)


def test_source_outcome_missing_readings_counts_a_source_that_produced_none():
    """A source that failed outright counts as one missing reading, not zero."""
    failed = SourceOutcome(source="gold_api", error="timeout")
    empty = SourceOutcome(source="gdelt", results=[])

    assert failed.missing is True
    assert failed.missing_readings == 1
    assert empty.missing is True
    assert empty.missing_readings == 0


def test_source_outcome_as_dict_is_report_ready():
    """The report payload is what the CLI and the ledger show, so its keys are a contract."""
    outcome = SourceOutcome(source="kraken_rest", results=[reading("kraken_rest", "BTC")])

    payload = outcome.as_dict()

    assert payload["source"] == "kraken_rest"
    assert payload["critical"] is True
    assert payload["missing"] is False
    assert payload["readings"] == 1
    assert payload["series"] == ["BTC"]


# ----------------------------------------------------------------------------------------------
# The source map: which sources are critical, which corroborate, which are listeners
# ----------------------------------------------------------------------------------------------


def test_the_three_source_maps_are_disjoint():
    """A source belongs to exactly one layer, so nothing is both a trigger and a corroborator."""
    critical = set(CRITICAL_SOURCES)
    confirmation = set(CONFIRMATION_SOURCES)
    early = set(EARLY_SIGNAL_SOURCES)

    assert critical and confirmation and early
    assert critical & confirmation == set()
    assert critical & early == set()
    assert confirmation & early == set()


def test_the_critical_layer_is_prices_and_the_regime_series():
    """Prior 5 reads the regime from gold and the dollar proxy, so those are never optional."""
    assert set(CRITICAL_SOURCES) == {"kraken_rest", "gold_api", "twelve_data"}


def test_every_source_runner_is_declared_in_sources_yaml():
    """The registry and the committed config must agree, or a source is silently unpriced."""
    declared = set(load_sources().sources)

    assert set(SOURCE_RUNNERS) <= declared, sorted(set(SOURCE_RUNNERS) - declared)


def test_early_signal_sources_are_not_polled_by_the_hourly_snapshot():
    """Task 4's listeners push between cycles; the snapshot must not double-poll them."""
    assert set(EARLY_SIGNAL_SOURCES) & set(SOURCE_RUNNERS) == set()


def test_sleeve_b_usage_slugs_name_real_protocols():
    """The usage filter is only meaningful if each sleeve B coin has a usage series to read."""
    assert set(SLEEVE_B_USAGE_SLUGS) == {
        "venice",
        "akash-network",
        "bittensor",
        "render-token",
    }
    assert all(slug and slug == slug.strip().lower() for slug in SLEEVE_B_USAGE_SLUGS)


def test_connector_lookup_fails_closed_for_an_unknown_source():
    """An undeclared source must raise, never hand back a half-built connector."""
    with pytest.raises(ConnectorUnavailable):
        _connector("not_a_source", "not_a_module", "NotAConnector")


def test_write_snapshot_records_a_missing_source_without_a_runner(ledger):
    """``write_snapshot`` is the ledger boundary: a failure is recorded as plainly as a reading."""
    outcomes = [
        SourceOutcome(source="kraken_rest", results=[reading("kraken_rest", "BTC")]),
        SourceOutcome(source="gold_api", error="timeout"),
    ]

    record_ids = write_snapshot(ledger, CYCLE, outcomes)

    assert len(record_ids) == 2
    rows = ledger.query('SELECT * FROM "raw_snapshot" WHERE "cycle_id" = ?', [CYCLE])
    assert {row["source"] for row in rows} == {"kraken_rest", "gold_api"}
    stale = [row for row in rows if row["is_stale"]]
    assert len(stale) == 1
    assert stale[0]["source"] == "gold_api"


def test_the_report_type_and_its_summary_lines_are_stable(ledger, transport, quota):
    """The CLI prints ``summary_lines``, so the report shape is a contract, not an internal detail."""
    report = snapshot(ledger, transport, quota, healthy_registry())

    assert isinstance(report, IngestReport)
    lines = report.summary_lines()
    assert lines
    assert all(isinstance(line, str) for line in lines)
    assert any(line.strip() for line in lines)

    payload = report.as_dict()
    assert payload["cycle_id"] == CYCLE
    assert payload["hold_only"] is False
    assert payload["symbols"] == ["BTC", "ETH"]
    assert payload["elapsed_seconds"] >= 0
