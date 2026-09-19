"""The hourly snapshot orchestrator: one cycle, every source, inside its quota.

This is the module the ``agentoquant ingest`` command resolves to
(``COMMAND_TARGETS["ingest"] = "agentoquant.data.ingest:main"``), and it is the first stage of the
hourly loop described in ``plan.md`` step 1.

Design rules, all of them load-bearing:

**Every source is attempted, and a failure is recorded rather than raised.** A connector that raises
:class:`~agentoquant.data.SourceError` or :class:`~agentoquant.data.quota_manager.QuotaExceeded` is
turned into a *missing* :class:`~agentoquant.data.ConnectorResult` (``is_stale=True``, ``error=...``)
and written to the ledger as such. The cycle always completes; what changes is whether it is allowed
to trade. Nothing here ever aborts the run because one upstream is down.

**Critical data missing means hold-only, not a guess.** ``kraken_rest`` carries the prices, the
account balances and the live fee tier; ``gold_api`` and ``twelve_data`` carry the gold price and the
dollar proxy that standing prior 5 reads the regime from. If any of those is missing the report sets
``hold_only`` and names the reason, and the decision layers downstream must not open new positions
from a half-blind snapshot. The confirmation-only sources (Alpha Vantage, GDELT, CryptoRank,
DefiLlama, Grok X Search) degrade to a missing reading without forcing hold-only: they corroborate,
they never lead.

**The quota manager is consulted before every call and its journal is the source of truth for the
per-source call counts in the report.** A refused call is recorded as a breach, not swallowed, and
the journal is the *same file* the early-signal listener runner spends from: the two processes share
one persisted budget, so this report covers the listener sources as well as the connectors.

**No credential value is ever logged, echoed or written to the ledger.** Connectors read keys
through :func:`agentoquant.config_loader.credentials`; this module only ever sees source *names*.

**Everything is written to the ledger.** Each reading becomes a ``raw_snapshot`` record under the
cycle id, so the daily review and the Reviewer agent can attribute a decision back to the exact
inputs that produced it.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agentoquant.config_loader import load_settings, load_sleeves
from agentoquant.data import ConnectorResult, SourceError, Transport
from agentoquant.data.cache import Cache
from agentoquant.data.quota_manager import QuotaExceeded, QuotaManager
from agentoquant.enums import Stage
from agentoquant.ledger.schema import RawSnapshotPayload
from agentoquant.ledger.store import LedgerStore

#: The role recorded on every ``raw_snapshot`` envelope written by this module.
PRODUCER_ROLE = "ingest"

#: Sources whose absence forces hold-only: prices and account state, then the two regime series
#: (gold and the dollar proxy) that prior 5 reads the regime from. Each of these connectors states
#: in its own docstring that a missing reading leaves the cycle half-blind, so the orchestrator
#: refuses to let a cycle trade on the remainder.
CRITICAL_SOURCES: tuple[str, ...] = ("kraken_rest", "gold_api", "twelve_data")

#: Sources that corroborate but never lead. A missing reading here is recorded and ignored.
CONFIRMATION_SOURCES: tuple[str, ...] = (
    "coingecko",
    "alpha_vantage",
    "gdelt",
    "defillama",
    "cryptorank",
    "grok_x_search",
)

#: Sources that arrive on the early-signal layer instead of the hourly snapshot (Task 4). Listed so
#: the report can show the full source map and so a reader can see what this stage deliberately
#: does not poll.
EARLY_SIGNAL_SOURCES: tuple[str, ...] = (
    "bybit_announcements",
    "okx_announcements",
    "kraken_listings_rss",
    "github_releases",
    "google_news_rss",
    "telegram_previews",
    "alchemy",
    "helius",
)

#: The usage series the plan names explicitly for sleeve B (``plan.md`` Task 3 acceptance).
SLEEVE_B_USAGE_SLUGS: tuple[str, ...] = ("venice", "akash-network", "bittensor", "render-token")


def new_cycle_id(moment: datetime | None = None) -> str:
    """A cycle id in the project's ``YYYY-MM-DDTHHZ-NNNN`` form.

    The suffix is a counter that the caller supplies when it is driving a numbered cycle; a bare
    timestamp is the honest default for a one-off manual run, and it is what the CLI produces.
    """
    when = (moment or datetime.now(UTC)).astimezone(UTC)
    return f"{when:%Y-%m-%dT%H}Z-0001"


@dataclass
class SourceOutcome:
    """What one source produced this cycle: its readings, or why it produced none."""

    source: str
    results: list[ConnectorResult] = field(default_factory=list)
    error: str | None = None
    skipped_reason: str | None = None
    calls: int = 0
    cache_hits: int = 0
    cost_usd: float = 0.0

    @property
    def critical(self) -> bool:
        return self.source in CRITICAL_SOURCES

    @property
    def missing(self) -> bool:
        """True when this source gave the cycle nothing usable."""
        if self.error is not None or self.skipped_reason is not None:
            return True
        return not self.results or all(result.missing for result in self.results)

    @property
    def stale_results(self) -> list[ConnectorResult]:
        return [result for result in self.results if result.missing]

    @property
    def missing_readings(self) -> int:
        """Readings that are missing: stale ones, plus one for a source that produced none at all."""
        if self.error is not None or self.skipped_reason is not None:
            return max(1, len(self.stale_results))
        return len(self.stale_results)

    @property
    def series(self) -> list[str]:
        return [result.coin_or_series for result in self.results]

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "critical": self.critical,
            "missing": self.missing,
            "error": self.error,
            "skipped_reason": self.skipped_reason,
            "readings": len(self.results),
            "missing_readings": len(self.stale_results),
            "series": self.series,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class IngestReport:
    """The whole snapshot: every source, the hold-only verdict, and the cost of the cycle."""

    cycle_id: str
    started_at: datetime
    finished_at: datetime
    outcomes: list[SourceOutcome] = field(default_factory=list)
    hold_only: bool = False
    hold_only_reasons: tuple[str, ...] = ()
    quota_breaches: dict[str, int] = field(default_factory=dict)
    record_ids: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- derived views -------------------------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def calls(self) -> int:
        return sum(outcome.calls for outcome in self.outcomes)

    @property
    def cache_hits(self) -> int:
        return sum(outcome.cache_hits for outcome in self.outcomes)

    @property
    def cost_usd(self) -> float:
        return round(sum(outcome.cost_usd for outcome in self.outcomes), 6)

    @property
    def missing_sources(self) -> list[str]:
        return [outcome.source for outcome in self.outcomes if outcome.missing]

    @property
    def readings(self) -> int:
        return sum(len(outcome.results) for outcome in self.outcomes)

    def outcome_for(self, source: str) -> SourceOutcome | None:
        for outcome in self.outcomes:
            if outcome.source == source:
                return outcome
        return None

    def readings_for(self, source: str) -> list[ConnectorResult]:
        outcome = self.outcome_for(source)
        return list(outcome.results) if outcome is not None else []

    def series(self, name: str) -> ConnectorResult | None:
        """The first reading whose ``coin_or_series`` matches ``name``, across every source."""
        for outcome in self.outcomes:
            for result in outcome.results:
                if result.coin_or_series == name:
                    return result
        return None

    def as_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id,
            "symbols": self.symbols,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "hold_only": self.hold_only,
            "hold_only_reasons": list(self.hold_only_reasons),
            "readings": self.readings,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "cost_usd": self.cost_usd,
            "missing_sources": self.missing_sources,
            "quota_breaches": self.quota_breaches,
            "records_written": len(self.record_ids),
            "sources": [outcome.as_dict() for outcome in self.outcomes],
            "notes": self.notes,
        }

    def summary_lines(self) -> list[str]:
        """The human-readable per-source call and cost report the task's verification asks for."""
        lines = [
            f"cycle {self.cycle_id}  {len(self.symbols)} symbols  "
            f"{self.elapsed_seconds:.2f}s  hold_only={self.hold_only}",
            "",
            f"{'source':<20} {'ok':>3} {'miss':>4} {'calls':>5} {'cache':>5} {'cost$':>8}  note",
            "-" * 78,
        ]
        for outcome in self.outcomes:
            note = outcome.error or outcome.skipped_reason or ""
            lines.append(
                f"{outcome.source:<20} {len(outcome.results) - len(outcome.stale_results):>3} "
                f"{outcome.missing_readings:>4} {outcome.calls:>5} {outcome.cache_hits:>5} "
                f"{outcome.cost_usd:>8.5f}  {note[:34]}"
            )
        lines.append("-" * 78)
        lines.append(
            f"{'TOTAL':<20} {self.readings - sum(len(o.stale_results) for o in self.outcomes):>3} "
            f"{sum(o.missing_readings for o in self.outcomes):>4} {self.calls:>5} "
            f"{self.cache_hits:>5} {self.cost_usd:>8.5f}"
        )
        if self.quota_breaches:
            lines.append(f"quota breaches: {self.quota_breaches}")
        if self.hold_only:
            lines.append(f"HOLD-ONLY: {'; '.join(self.hold_only_reasons)}")
        return lines


__all__ = [
    "CONFIRMATION_SOURCES",
    "CRITICAL_SOURCES",
    "EARLY_SIGNAL_SOURCES",
    "IngestReport",
    "PRODUCER_ROLE",
    "SLEEVE_B_USAGE_SLUGS",
    "SOURCE_RUNNERS",
    "SourceOutcome",
    "RunContext",
    "new_cycle_id",
]


# ----------------------------------------------------------------------------------------------
# The connector registry
# ----------------------------------------------------------------------------------------------
#
# Each entry is one source, run in the order listed (critical sources first, so a quota or auth
# problem surfaces at the top of the report rather than after eight confirmation calls). A runner
# returns the readings that source produced this cycle. It may raise: the orchestrator catches
# SourceError and QuotaExceeded and records the source as missing, which is the whole point of the
# graceful-degradation rule.


@dataclass(frozen=True)
class RunContext:
    """What every runner needs to know: the universe to cover and whether to bypass the cache."""

    symbols: list[str] = field(default_factory=list)
    force: bool = False


#: A runner: given the shared transport and the run context, produce this source's readings.
SourceRunner = Callable[[Transport, RunContext], list[ConnectorResult]]


class ConnectorUnavailable(SourceError):
    """A connector module or method is not present yet. Recorded as missing, never fatal."""

    def __init__(self, source: str, message: str) -> None:
        super().__init__(source, message)


def _connector(source: str, module: str, class_name: str) -> Any:
    """Import one connector class lazily, with a clear failure when it is not there yet.

    Lazy import keeps the orchestrator usable while sibling connector work is still landing, and it
    means a single broken connector module cannot stop the whole snapshot from being built.
    """
    try:
        imported = importlib.import_module(f"agentoquant.data.connectors.{module}")
    except ImportError as exc:  # pragma: no cover - exercised by the missing-connector test
        raise ConnectorUnavailable(
            source, f"connector module {module!r} is not available: {exc}"
        ) from exc
    connector_class = getattr(imported, class_name, None)
    if connector_class is None:  # pragma: no cover - defensive
        raise ConnectorUnavailable(source, f"{class_name} is not defined in {module!r}")
    return connector_class


def _call(connector: Any, source: str, *names: str, **kwargs: Any) -> list[ConnectorResult]:
    """Call the first method present from ``names`` and normalise the answer to a result list.

    Connectors are allowed to expose either a single ``fetch`` returning one
    :class:`~agentoquant.data.ConnectorResult` or a family of ``fetch_*`` methods returning several
    (Kraken returns market, fee-tier and account readings from three calls). Normalising here keeps
    every runner three lines long and keeps the ledger writer uniform.
    """
    for name in names:
        method = getattr(connector, name, None)
        if method is None:
            continue
        produced = method(**kwargs)
        if produced is None:
            return []
        if isinstance(produced, ConnectorResult):
            return [produced]
        return list(produced)
    raise ConnectorUnavailable(source, f"connector exposes none of {', '.join(names)}")


# -- critical sources -------------------------------------------------------------------------


def run_kraken(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    """Prices for the universe, the live fee tier and the account state: the critical core."""
    from agentoquant.data.connectors.kraken import KrakenConnector

    connector = KrakenConnector(transport)
    results: list[ConnectorResult] = list(
        _call(connector, "kraken_rest", "fetch_market", symbols=ctx.symbols, force=ctx.force)
    )
    results.extend(_call(connector, "kraken_rest", "fetch_fee_tier", force=ctx.force))
    results.extend(_call(connector, "kraken_rest", "fetch_account"))
    return results


def run_gold_api(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    """The keyless gold spot price: one half of the prior-5 regime reading."""
    connector_class = _connector("gold_api", "gold_api", "GoldApiConnector")
    connector = connector_class(transport)
    return _call(connector, "gold_api", "fetch", force=ctx.force)


def run_twelve_data(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    """Gold (XAU/USD) and the dollar proxy: the other half of the prior-5 regime reading."""
    connector_class = _connector("twelve_data", "twelve_data", "TwelveDataConnector")
    connector = connector_class(transport)
    return _call(connector, "twelve_data", "fetch_macro", force=ctx.force)


# -- confirmation sources ---------------------------------------------------------------------


def run_coingecko(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    """Market-wide context (total cap, dominance) plus a per-coin cross-check of Kraken."""
    connector_class = _connector("coingecko", "coingecko", "CoinGeckoConnector")
    connector = connector_class(transport)
    results: list[ConnectorResult] = list(
        _call(connector, "coingecko", "fetch_market_global", force=ctx.force)
    )
    results.extend(
        _call(connector, "coingecko", "fetch_markets", symbols=ctx.symbols, force=ctx.force)
    )
    return results


def run_alpha_vantage(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    """News sentiment. Confirmation layer only: 25 calls a day is the whole free budget."""
    connector_class = _connector("alpha_vantage", "alpha_vantage", "AlphaVantageConnector")
    connector = connector_class(transport)
    return _call(
        connector, "alpha_vantage", "fetch", "news_sentiment", force=ctx.force
    )


def run_gdelt(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    """Conflict events and tone. Confirmation layer only, and rate-limited to once a minute."""
    connector_class = _connector("gdelt", "gdelt", "GdeltConnector")
    connector = connector_class(transport)
    return _call(connector, "gdelt", "fetch", "conflict_events", force=ctx.force)


def run_defillama(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    """Fees, revenue and TVL: the usage filter behind prior 3, for the sleeve B names."""
    connector_class = _connector("defillama", "defillama", "DefiLlamaConnector")
    connector = connector_class(transport)
    results: list[ConnectorResult] = []
    for slug in SLEEVE_B_USAGE_SLUGS:
        results.extend(_call(connector, "defillama", "usage_for", slug=slug, force=ctx.force))
    return results


def run_cryptorank(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    """Global metrics and the unlock attempt. Unlocks are Pro-only, so absence is recorded."""
    connector_class = _connector("cryptorank", "cryptorank", "CryptoRankConnector")
    connector = connector_class(transport)
    return _call(connector, "cryptorank", "fetch", "global_metrics", force=ctx.force)


def run_grok_x_search(transport: Transport, ctx: RunContext) -> list[ConnectorResult]:
    """Official-handle X posts through Grok's native web plugin, cited and cost-metered."""
    connector_class = _connector("grok_x_search", "grok_x_search", "GrokXSearchConnector")
    connector = connector_class(transport)
    return _call(
        connector,
        "grok_x_search",
        "fetch",
        "ticker_sentiment",
        force=ctx.force,
    )


#: The full registry, in run order. Critical sources are attempted first.
SOURCE_RUNNERS: dict[str, SourceRunner] = {
    "kraken_rest": run_kraken,
    "gold_api": run_gold_api,
    "twelve_data": run_twelve_data,
    "coingecko": run_coingecko,
    "alpha_vantage": run_alpha_vantage,
    "gdelt": run_gdelt,
    "defillama": run_defillama,
    "cryptorank": run_cryptorank,
    "grok_x_search": run_grok_x_search,
}


# ----------------------------------------------------------------------------------------------
# The snapshot
# ----------------------------------------------------------------------------------------------


def configured_universe() -> list[str]:
    """Every coin in the enabled sleeves, in sleeve order, deduplicated.

    Read from ``config/sleeves.yaml`` rather than hard-coded, so adding a coin is a reviewed config
    change and this module never needs editing.
    """
    sleeves = load_sleeves()
    symbols: list[str] = []
    for spec in sleeves.sleeves.values():
        if not getattr(spec, "enabled", False):
            continue
        for coin in getattr(spec, "coins", []) or []:
            if coin not in symbols:
                symbols.append(coin)
    return symbols


def run_source(
    source: str,
    transport: Transport,
    ctx: RunContext,
    runner: SourceRunner,
) -> SourceOutcome:
    """Run one source, converting any failure into a recorded missing reading.

    The two expected failure modes are :class:`SourceError` (the upstream answered, badly) and
    :class:`QuotaExceeded` (we refused to call it because the budget was spent). Both leave the
    source missing. Nothing else is caught: a genuine bug should surface as a traceback in tests
    rather than be silently laundered into a "missing source".
    """
    calls_before = transport.calls
    hits_before = transport.cache_hits
    cost_before = sum(transport.costs.values())
    try:
        results = runner(transport, ctx)
    except (SourceError, QuotaExceeded) as exc:
        return SourceOutcome(
            source=source,
            error=str(exc),
            calls=transport.calls - calls_before,
            cache_hits=transport.cache_hits - hits_before,
            cost_usd=sum(transport.costs.values()) - cost_before,
        )
    return SourceOutcome(
        source=source,
        results=list(results),
        calls=transport.calls - calls_before,
        cache_hits=transport.cache_hits - hits_before,
        cost_usd=sum(transport.costs.values()) - cost_before,
    )


def _hold_only_reasons(outcomes: list[SourceOutcome]) -> tuple[str, ...]:
    """Why this cycle may not open new positions, or an empty tuple when it may.

    Only critical sources can force hold-only. A confirmation source being down is recorded in the
    report and changes nothing, which is exactly the asymmetry the plan asks for: the corroboration
    layer is allowed to be absent, the price and regime layer is not.
    """
    reasons: list[str] = []
    for outcome in outcomes:
        if outcome.source not in CRITICAL_SOURCES or not outcome.missing:
            continue
        detail = outcome.error or outcome.skipped_reason or "no reading"
        reasons.append(f"{outcome.source} missing ({detail})")
    return tuple(reasons)


def write_snapshot(
    ledger: LedgerStore,
    cycle_id: str,
    outcomes: list[SourceOutcome],
) -> list[str]:
    """Write every reading to the ledger as a ``raw_snapshot`` record. Returns the record ids.

    A missing reading is written too, with ``is_stale=True`` and the failure reason in ``fields``,
    so the ledger shows what the cycle did *not* know as plainly as what it did. Attribution later
    depends on that: a decision made on a stale price has to be visible after the fact.
    """
    record_ids: list[str] = []
    for outcome in outcomes:
        if outcome.error is not None or outcome.skipped_reason is not None:
            record_ids.append(
                ledger.write(
                    Stage.RAW_SNAPSHOT,
                    cycle_id,
                    RawSnapshotPayload(
                        source=outcome.source,
                        coin_or_series=outcome.source,
                        fields={
                            "missing": True,
                            "reason": outcome.error or outcome.skipped_reason,
                            "checked_at": datetime.now(UTC).isoformat(),
                        },
                        quota_remaining=0,
                        is_stale=True,
                    ),
                    producer_role=PRODUCER_ROLE,
                )
            )
            continue
        for result in outcome.results:
            record_ids.append(
                ledger.write(
                    Stage.RAW_SNAPSHOT,
                    cycle_id,
                    RawSnapshotPayload(
                        source=result.source,
                        coin_or_series=result.coin_or_series,
                        fields=dict(result.fields),
                        quota_remaining=result.quota_remaining,
                        is_stale=result.is_stale,
                    ),
                    producer_role=PRODUCER_ROLE,
                )
            )
    return record_ids


def run_snapshot(
    *,
    cycle_id: str | None = None,
    symbols: list[str] | None = None,
    force: bool = False,
    ledger: LedgerStore | None = None,
    transport: Transport | None = None,
    runners: dict[str, SourceRunner] | None = None,
    quota: QuotaManager | None = None,
    cache: Cache | None = None,
) -> IngestReport:
    """One hourly snapshot across every source, inside its quota.

    ``transport``, ``runners``, ``quota`` and ``cache`` are injectable so the test suite can drive
    the whole snapshot against a replay transport with no network and no journal side effects.
    """
    settings = load_settings()
    started = datetime.now(UTC)
    resolved_cycle = cycle_id or new_cycle_id(started)
    universe = list(symbols) if symbols else configured_universe()
    registry = runners if runners is not None else SOURCE_RUNNERS

    if quota is None:
        quota = QuotaManager()
    if transport is None:
        transport = Transport(quota=quota, cache=cache if cache is not None else Cache())
    if ledger is None:
        ledger = LedgerStore()

    ctx = RunContext(symbols=universe, force=force)

    outcomes: list[SourceOutcome] = []
    for source, runner in registry.items():
        outcomes.append(run_source(source, transport, ctx, runner))

    report = IngestReport(
        cycle_id=resolved_cycle,
        started_at=started,
        finished_at=datetime.now(UTC),
        outcomes=outcomes,
        hold_only_reasons=_hold_only_reasons(outcomes),
        quota_breaches=quota.breaches(),
        symbols=universe,
        notes=[
            f"venue={settings.venue.exchange} mode={settings.venue.mode}",
            "confirmation sources are allowed to be absent; critical sources are not",
            f"quota: {len(quota.sources.sources)} declared sources enforced from one journal "
            f"({quota.journal_path}), shared with the early-signal listener runner",
        ],
    )
    report.hold_only = bool(report.hold_only_reasons)
    report.record_ids = write_snapshot(ledger, resolved_cycle, outcomes)
    report.finished_at = datetime.now(UTC)
    # A source the quota manager refused is reported with the reason it was skipped, not merely as a
    # low call count: the refusal may have happened in the other process, and the journal carries it.
    refusals = quota.refusals()
    if refusals:
        reasons = {
            row.source: row.last_refusal_reason or "quota exhausted"
            for row in quota.report()
            if row.breaches
        }
        report.notes.append(f"quota refusals (sources skipped this window): {reasons}")
    return report


def main(
    *,
    cycle_id: str | None = None,
    symbols: list[str] | None = None,
    force: bool = False,
) -> Any:
    """The ``agentoquant ingest`` entry point.

    Returns a :class:`~agentoquant.cli.CommandOutput`. ``status`` is ``"ok"`` even when the cycle is
    hold-only, because the snapshot itself succeeded; hold-only is a *field* of the payload, not a
    command failure. That distinction matters to the scheduler: a hold-only cycle is a normal cycle.
    """
    from agentoquant.cli import CommandOutput

    report = run_snapshot(cycle_id=cycle_id, symbols=list(symbols or []), force=force)
    message = "\n".join(report.summary_lines())
    return CommandOutput(
        command="ingest",
        status="ok",
        message=message,
        payload=report.as_dict(),
    )


def cli() -> None:  # pragma: no cover - the typer command in cli.py is the real entry point
    """Print one snapshot summary. Kept for ``python -m agentoquant.data.ingest``."""
    report = run_snapshot()
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False, default=str))


__all__ += [
    "ConnectorUnavailable",
    "SourceRunner",
    "configured_universe",
    "main",
    "run_snapshot",
    "run_source",
    "write_snapshot",
]
