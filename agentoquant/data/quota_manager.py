"""Per-source call budget: rolling-window quotas, a call journal and the per-source report.

``config/sources.yaml`` declares a quota per source (``calls_per_minute``, ``calls_per_hour``,
``calls_per_day``, ``monthly_credits``). This module is what enforces it. Every outbound connector
call goes through :meth:`QuotaManager.acquire` first; when a ceiling would be breached it raises
:class:`QuotaExceeded` **before** the request is made, the connector turns that into a missing-source
result, and the cycle continues. A quota breach is therefore impossible by construction: the call is
refused rather than counted afterwards.

Windows
-------
Windows are rolling, not calendar-aligned: ``calls_per_minute`` counts calls in the last 60 seconds,
``calls_per_hour`` in the last 3600, ``calls_per_day`` in the last 86400. ``monthly_credits`` is
counted over a rolling 30-day window. A rolling window is stricter than a calendar one (it can never
permit more calls in any real minute than the declared ceiling) and it needs no midnight bookkeeping.

Journal
-------
Every attempt is appended to a JSONL journal (``data/quota_journal.jsonl`` by default), one line per
call::

    {"ts": "2026-09-19T03:20:00+00:00", "source": "kraken_rest", "ok": true,
     "cost_usd": 0.0, "detail": "GET /0/public/Ticker"}

The journal is the evidence for the acceptance criterion "one hourly snapshot completes inside every
source's quota": :meth:`QuotaManager.report` reads it back and prints per-source call counts, the
declared ceiling, the remaining budget and the spend.

Local guards stricter than config
---------------------------------
Two sources need a tighter spacing than their config ceiling alone provides, both verified live on
this host: GDELT answers 429 unless requests are at least 60 s apart (its own message says 5 s; 5 s
still 429s from this IP, 60 s works), and Twelve Data's free tier spends credits per endpoint rather
than per request (8 credits a minute). :data:`MIN_INTERVAL_SECONDS` encodes that as a minimum gap
between two calls to the same source. It only ever *removes* calls, never adds them, so it can never
push a source over its configured quota.

Clock injection
---------------
``clock`` is a ``Callable[[], datetime]``. The hourly cycle uses the default (wall clock); the
accelerated 24-hour dry run and the tests inject a simulated clock so a day of cycles can be replayed
without a day of waiting.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agentoquant.config_loader import Sources, load_sources, repo_root

#: Environment override, mirroring ``AGENTOQUANT_LEDGER_PATH``'s convention.
JOURNAL_PATH_ENV = "AGENTOQUANT_QUOTA_JOURNAL"

#: Default journal location. ``/data/`` is gitignored, so the journal is never committed.
DEFAULT_JOURNAL_PATH = Path("data") / "quota_journal.jsonl"

#: Rolling window lengths in seconds, keyed by the ``SourceQuota`` field they enforce.
WINDOW_SECONDS: dict[str, int] = {
    "calls_per_minute": 60,
    "calls_per_hour": 3600,
    "calls_per_day": 86400,
    "monthly_credits": 30 * 86400,
}

#: Minimum seconds between two calls to the same source, stricter than the config ceiling where the
#: upstream provider needs it. Verified live 2026-09-19 (see the module docstring).
MIN_INTERVAL_SECONDS: dict[str, float] = {
    "gdelt": 60.0,
    "twelve_data": 8.0,
}

#: How many journal lines to keep in memory at once when reporting. A day of hourly cycles across
#: every source is a few hundred lines, so this is generous rather than tight.
JOURNAL_TAIL_LIMIT = 20_000


def default_journal_path() -> Path:
    """``AGENTOQUANT_QUOTA_JOURNAL`` if set, else ``<repo>/data/quota_journal.jsonl``."""
    override = os.environ.get(JOURNAL_PATH_ENV)
    if override:
        return Path(override)
    return repo_root() / DEFAULT_JOURNAL_PATH


class QuotaExceeded(Exception):
    """A call would breach the source's declared budget. Raised before the request is made."""

    def __init__(self, source: str, kind: str, limit: int, used: int, retry_after: float) -> None:
        self.source = source
        self.kind = kind
        self.limit = limit
        self.used = used
        self.retry_after = retry_after
        super().__init__(
            f"quota for {source!r} would be breached: {kind} is {used}/{limit}, "
            f"next call allowed in {retry_after:.0f}s"
        )


@dataclass
class QuotaDecision:
    """The answer to "may I call this source now?"."""

    source: str
    allowed: bool
    remaining: int
    reason: str = ""
    retry_after_seconds: float = 0.0
    tightest: str | None = None

    def __bool__(self) -> bool:  # `if quota.check(...):` reads naturally
        return self.allowed


@dataclass
class JournalEntry:
    ts: datetime
    source: str
    ok: bool = True
    cost_usd: float = 0.0
    detail: str = ""
    #: False for a pure cost annotation (Grok X Search reports its price after the call): such a line
    #: must never be counted as a second call against the budget.
    counts_as_call: bool = True


@dataclass
class SourceReport:
    """One row of the per-source call count and cost report."""

    source: str
    calls_1m: int
    calls_1h: int
    calls_1d: int
    calls_total: int
    failures: int
    cost_usd: float
    limits: dict = field(default_factory=dict)
    remaining: int = 0
    last_call_at: datetime | None = None
    breaches: int = 0

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "calls_1m": self.calls_1m,
            "calls_1h": self.calls_1h,
            "calls_1d": self.calls_1d,
            "calls_total": self.calls_total,
            "failures": self.failures,
            "cost_usd": round(self.cost_usd, 6),
            "limits": self.limits,
            "remaining": self.remaining,
            "breaches": self.breaches,
            "last_call_at": self.last_call_at.isoformat() if self.last_call_at else None,
        }


class QuotaManager:
    """Rolling-window call budgets for every source in ``config/sources.yaml``."""

    def __init__(
        self,
        sources: Sources | None = None,
        *,
        journal_path: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
        persist: bool = True,
    ) -> None:
        self.sources = sources if sources is not None else load_sources()
        self.journal_path = (
            Path(journal_path) if journal_path is not None else default_journal_path()
        )
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self.persist = persist
        #: In-memory view of the journal, newest last. Loaded lazily on first use.
        self._entries: list[JournalEntry] | None = None
        #: Counts of refused calls per source, for the report.
        self._breaches: dict[str, int] = {}

    # -- clock and journal --------------------------------------------------------------------

    def now(self) -> datetime:
        """The current time from the injected clock, always UTC-aware."""
        moment = self.clock()
        return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)

    def _load(self) -> list[JournalEntry]:
        if self._entries is not None:
            return self._entries
        entries: list[JournalEntry] = []
        if self.persist and self.journal_path.exists():
            try:
                lines = self.journal_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines[-JOURNAL_TAIL_LIMIT:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    blob = json.loads(line)
                    stamp = datetime.fromisoformat(str(blob.get("ts")))
                except (json.JSONDecodeError, ValueError):
                    continue
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                entries.append(
                    JournalEntry(
                        ts=stamp.astimezone(UTC),
                        source=str(blob.get("source") or ""),
                        ok=bool(blob.get("ok", True)),
                        cost_usd=float(blob.get("cost_usd") or 0.0),
                        detail=str(blob.get("detail") or ""),
                        counts_as_call=bool(blob.get("call", True)),
                    )
                )
        self._entries = entries
        return entries

    def _append(self, entry: JournalEntry) -> None:
        self._load().append(entry)
        if not self.persist:
            return
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "ts": entry.ts.isoformat(),
                            "source": entry.source,
                            "ok": entry.ok,
                            "cost_usd": round(entry.cost_usd, 8),
                            "detail": entry.detail,
                            "call": entry.counts_as_call,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            # A journal write failure must never break a cycle: the in-memory copy still counts.
            return

    # -- limits -------------------------------------------------------------------------------

    def limits(self, source: str) -> dict:
        """The declared ceilings for a source as a plain dict (empty when the source is unknown)."""
        spec = self.sources.sources.get(source)
        if spec is None:
            return {}
        return {
            key: value
            for key, value in spec.quota.model_dump().items()
            if key in WINDOW_SECONDS and value is not None
        }

    def ttl_seconds(self, source: str, default: int = 0) -> int:
        """The source's declared cache TTL, or ``default`` when it declares none."""
        spec = self.sources.sources.get(source)
        if spec is None or spec.cache_ttl_seconds is None:
            return default
        return int(spec.cache_ttl_seconds)

    def min_interval(self, source: str) -> float:
        """Minimum seconds between two calls to this source (0 when it has no tighter guard)."""
        return float(MIN_INTERVAL_SECONDS.get(source, 0.0))

    def used(self, source: str, window_seconds: int, now: datetime | None = None) -> int:
        """Calls recorded for ``source`` inside the last ``window_seconds``."""
        moment = now or self.now()
        cutoff = moment.timestamp() - window_seconds
        return sum(
            1
            for entry in self._load()
            if entry.source == source
            and entry.counts_as_call
            and entry.ts.timestamp() > cutoff
        )

    def last_call_at(self, source: str) -> datetime | None:
        """When this source was last called, or ``None``."""
        stamps = [entry.ts for entry in self._load() if entry.source == source and entry.counts_as_call]
        return max(stamps) if stamps else None

    def check(self, source: str, *, now: datetime | None = None) -> QuotaDecision:
        """Would one more call to ``source`` fit inside every declared ceiling?"""
        moment = now or self.now()
        limits = self.limits(source)
        remaining = 1 << 30
        tightest: str | None = None
        for kind, ceiling in limits.items():
            window = WINDOW_SECONDS[kind]
            used = self.used(source, window, moment)
            left = int(ceiling) - used
            if left < remaining:
                remaining, tightest = left, kind
            if left <= 0:
                retry_after = self._retry_after(source, window, moment)
                return QuotaDecision(
                    source=source,
                    allowed=False,
                    remaining=0,
                    reason=f"{kind} exhausted ({used}/{ceiling})",
                    retry_after_seconds=retry_after,
                    tightest=kind,
                )
        gap = self.min_interval(source)
        if gap:
            last = self.last_call_at(source)
            if last is not None:
                elapsed = (moment - last).total_seconds()
                if elapsed < gap:
                    return QuotaDecision(
                        source=source,
                        allowed=False,
                        remaining=max(0, remaining),
                        reason=f"minimum interval {gap:.0f}s not elapsed ({elapsed:.1f}s)",
                        retry_after_seconds=gap - elapsed,
                        tightest="min_interval",
                    )
        return QuotaDecision(
            source=source,
            allowed=True,
            remaining=max(0, remaining),
            reason="ok",
            tightest=tightest,
        )

    def _retry_after(self, source: str, window_seconds: int, now: datetime) -> float:
        cutoff = now.timestamp() - window_seconds
        stamps = sorted(
            entry.ts.timestamp()
            for entry in self._load()
            if entry.source == source and entry.ts.timestamp() > cutoff
        )
        if not stamps:
            return 0.0
        return max(0.0, stamps[0] + window_seconds - now.timestamp())

    def acquire(self, source: str, *, now: datetime | None = None, detail: str = "") -> QuotaDecision:
        """Reserve one call. Raises :class:`QuotaExceeded` when the budget is exhausted.

        The reservation is journalled immediately, so two concurrent callers cannot both spend the
        last unit of a budget. Cost is attached afterwards with :meth:`add_cost`.
        """
        moment = now or self.now()
        decision = self.check(source, now=moment)
        if not decision.allowed:
            self._breaches[source] = self._breaches.get(source, 0) + 1
            raise QuotaExceeded(
                source,
                decision.tightest or "quota",
                int(self.limits(source).get(decision.tightest or "", 0)),
                int(self.limits(source).get(decision.tightest or "", 0)) - decision.remaining,
                decision.retry_after_seconds,
            )
        self._append(JournalEntry(ts=moment, source=source, ok=True, detail=detail))
        return decision

    def add_cost(self, source: str, cost_usd: float, *, now: datetime | None = None) -> None:
        """Attach the exact cost of the call just made (Grok X Search reports one per call)."""
        if not cost_usd:
            return
        self._append(
            JournalEntry(
                ts=now or self.now(),
                source=source,
                ok=True,
                cost_usd=float(cost_usd),
                detail="cost",
                counts_as_call=False,
            )
        )

    def mark_failure(self, source: str, detail: str = "", *, now: datetime | None = None) -> None:
        """Record that a call was attempted and failed. Still counts against the budget."""
        self._append(
            JournalEntry(ts=now or self.now(), source=source, ok=False, detail=detail[:200])
        )

    def remaining(self, source: str, *, now: datetime | None = None) -> int:
        """The smallest remaining budget across the source's declared ceilings."""
        decision = self.check(source, now=now)
        return decision.remaining

    def breaches(self) -> dict[str, int]:
        """Refused-call counts per source, for the run report. Should be all zeros."""
        return dict(self._breaches)

    # -- reporting ----------------------------------------------------------------------------

    def report(self, *, now: datetime | None = None) -> list[SourceReport]:
        """Per-source call counts, cost and remaining budget over the rolling windows."""
        moment = now or self.now()
        rows: list[SourceReport] = []
        entries = self._load()
        seen = sorted({entry.source for entry in entries} | set(self.sources.sources))
        for source in seen:
            source_entries = [entry for entry in entries if entry.source == source]
            call_entries = [entry for entry in source_entries if entry.counts_as_call]
            stamps = [entry.ts for entry in call_entries]
            rows.append(
                SourceReport(
                    source=source,
                    calls_1m=self.used(source, WINDOW_SECONDS["calls_per_minute"], moment),
                    calls_1h=self.used(source, WINDOW_SECONDS["calls_per_hour"], moment),
                    calls_1d=self.used(source, WINDOW_SECONDS["calls_per_day"], moment),
                    calls_total=len(call_entries),
                    failures=sum(1 for entry in call_entries if not entry.ok),
                    cost_usd=sum(entry.cost_usd for entry in source_entries),
                    limits=self.limits(source),
                    remaining=self.remaining(source, now=moment),
                    last_call_at=max(stamps) if stamps else None,
                    breaches=self._breaches.get(source, 0),
                )
            )
        return rows

    def report_table(self, *, now: datetime | None = None) -> str:
        """The report as a fixed-width table, for the CLI's human-readable summary."""
        header = (
            f"{'source':<16} {'1m':>4} {'1h':>4} {'1d':>4} {'total':>6} "
            f"{'fail':>5} {'cost_usd':>9} {'remaining':>9} {'limits':<46}"
        )
        lines = [header, "-" * len(header)]
        for row in self.report(now=now):
            limits = ", ".join(f"{key.split('_', 1)[1]}={value}" for key, value in row.limits.items())
            lines.append(
                f"{row.source:<16} {row.calls_1m:>4} {row.calls_1h:>4} {row.calls_1d:>4} "
                f"{row.calls_total:>6} {row.failures:>5} {row.cost_usd:>9.4f} "
                f"{row.remaining:>9} {limits:<46}"
            )
        return "\n".join(lines)

    def total_cost(self) -> float:
        """Total recorded spend across every source."""
        return round(sum(entry.cost_usd for entry in self._load()), 8)


__all__ = [
    "DEFAULT_JOURNAL_PATH",
    "JOURNAL_PATH_ENV",
    "JOURNAL_TAIL_LIMIT",
    "MIN_INTERVAL_SECONDS",
    "WINDOW_SECONDS",
    "JournalEntry",
    "QuotaDecision",
    "QuotaExceeded",
    "QuotaManager",
    "SourceReport",
    "default_journal_path",
]
