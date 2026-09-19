"""The listener runner: runs every early-signal listener concurrently, logs restarts, never dies.

Invoked as a long-lived process (there is no CLI command for it - ``COMMAND_TARGETS`` has none)::

    python -m agentoquant.data.early_signals.runner --duration 3600
    python -m agentoquant.data.early_signals.runner replay --fixtures /path/to/fixtures

Behaviour, in the order the task's hard requirements state it:

* One thread per listener; the listeners are independent, so a slow or failing source cannot delay the
  others.
* A failing listener logs, backs off and retries; the runner only restarts a thread that actually
  exited. Every restart is written to the JSON-lines log (``logs/early_signals.jsonl`` by default) with
  its reason and backoff, which is the "restarts logged" evidence.
* A heartbeat line every ``--status-every`` seconds carries each listener's counters, so a week of
  uptime is one file to read rather than a claim.
* Every listener that fetches spends from one persisted
  :class:`~agentoquant.data.quota_manager.QuotaManager` budget -- the same journal file the hourly
  ingest process spends from -- so the seven early-signal sources are inside the ceilings
  ``config/sources.yaml`` declares for them, and a source over budget is skipped with a recorded
  reason instead of being fetched anyway.
* SIGINT/SIGTERM stop it cleanly.

The ``replay`` subcommand is the honest version of "replay of three past listing days": it takes
recorded announcement fixtures (a real captured response body plus the real first-article timestamp
from Google News RSS), runs them through the **same** parse and write path the live listeners use, and
reports the measured lead time. It never invents a timestamp.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agentoquant.config_loader import repo_root
from agentoquant.data.early_signals import (
    LOGGER,
    JsonlLog,
    SignalEvent,
    SignalWriter,
    as_utc,
    utcnow,
)
from agentoquant.data.quota_manager import QuotaManager
from agentoquant.enums import SourceClass
from agentoquant.ledger.store import LedgerStore

DEFAULT_LOG_DIR = "logs"
LOG_FILENAME = "early_signals.jsonl"

#: Restart backoff bounds for a listener thread that exited unexpectedly.
RESTART_BACKOFF_SECONDS = 5.0
MAX_RESTART_BACKOFF_SECONDS = 300.0


# ----------------------------------------------------------------------------------------------
# Wiring the listener set
# ----------------------------------------------------------------------------------------------


def build_listeners(
    writer: SignalWriter,
    *,
    log: JsonlLog | None = None,
    only: Sequence[str] | None = None,
    interval_scale: float = 1.0,
    include_onchain: bool = True,
    quota: QuotaManager | None = None,
) -> list[Any]:
    """Construct the default listener set, minus anything that cannot start.

    The on-chain receiver is skipped with a logged warning when no signing secret is configured: it
    refuses to run unauthenticated, and a missing secret must not stop the other five listeners.

    Every listener that fetches is handed the same :class:`QuotaManager`, so the seven sources of the
    early-signal layer spend from the same persisted budget as the hourly ingest rather than from a
    per-listener interval guess (the F14 defect). :func:`quota_audit` is the check for that.
    """
    from agentoquant.data.early_signals.bybit_listings import BybitListingsListener
    from agentoquant.data.early_signals.github_releases import GithubReleasesListener
    from agentoquant.data.early_signals.google_news_rss import GoogleNewsRssListener
    from agentoquant.data.early_signals.kraken_listings import KrakenListingsListener
    from agentoquant.data.early_signals.okx_listings import OkxListingsListener
    from agentoquant.data.early_signals.telegram_previews import TelegramPreviewsListener

    quota = quota if quota is not None else QuotaManager()
    factories: list[Callable[[], Any]] = [
        lambda: BybitListingsListener(
            writer, log=log, interval_seconds=20.0 * interval_scale, quota=quota
        ),
        lambda: OkxListingsListener(
            writer, log=log, interval_seconds=30.0 * interval_scale, quota=quota
        ),
        lambda: KrakenListingsListener(
            writer, log=log, interval_seconds=20.0 * interval_scale, quota=quota
        ),
        lambda: GithubReleasesListener(
            writer, log=log, interval_seconds=600.0 * interval_scale, quota=quota
        ),
        lambda: GoogleNewsRssListener(
            writer, log=log, interval_seconds=300.0 * interval_scale, quota=quota
        ),
        lambda: TelegramPreviewsListener(
            writer, log=log, interval_seconds=60.0 * interval_scale, quota=quota
        ),
    ]
    if include_onchain:
        factories.append(lambda: _build_onchain_receiver(writer, log))

    listeners = []
    wanted = {name.strip() for name in only} if only else None
    for factory in factories:
        listener = factory()
        if listener is None:
            continue
        if wanted is not None and listener.name not in wanted:
            continue
        listeners.append(listener)
    return listeners


def quota_audit(listeners: Sequence[Any]) -> list[str]:
    """Name every fetcher that would make a request outside the shared quota manager.

    Empty is the only acceptable answer for a listener that fetches: a fetcher with no manager (or no
    ``sources.yaml`` name) spends a budget nobody counts, which is exactly what F14 found. The
    push-only on-chain receiver owns no fetcher and is skipped.
    """
    unbudgeted: list[str] = []
    for listener in listeners:
        fetchers = listener.fetchers() if hasattr(listener, "fetchers") else []
        for fetcher in fetchers:
            if fetcher.quota is None or not fetcher.source:
                unbudgeted.append(f"{listener.name}:{fetcher.base_url or 'fetcher'}")
    return unbudgeted


def quota_sources(listeners: Sequence[Any]) -> list[str]:
    """The ``sources.yaml`` names the listener set spends from, sorted and deduplicated."""
    sources: set[str] = set()
    for listener in listeners:
        fetchers = listener.fetchers() if hasattr(listener, "fetchers") else []
        sources.update(fetcher.source for fetcher in fetchers if fetcher.source)
    return sorted(sources)


def _build_onchain_receiver(writer: SignalWriter, log: JsonlLog | None) -> Any:
    from agentoquant.data.early_signals.onchain_webhooks import WebhookReceiver

    try:
        return WebhookReceiver(writer, log=log)
    except RuntimeError as exc:
        LOGGER.warning("on-chain webhook receiver not started: %s", exc)
        if log is not None:
            log.write("listener_unavailable", listener="onchain_webhooks", reason=str(exc)[:300])
        return None


def default_writer(*, db_path: Path | str | None = None, log: JsonlLog | None = None) -> SignalWriter:
    """A writer on the configured ledger (``AGENTOQUANT_LEDGER_PATH`` or ``settings.ledger_path``)."""
    return SignalWriter(LedgerStore(db_path) if db_path is not None else LedgerStore(), log=log)


def default_log(log_dir: Path | str | None = None) -> JsonlLog:
    """``logs/early_signals.jsonl`` under the repo root unless ``log_dir`` says otherwise."""
    base = Path(log_dir) if log_dir is not None else repo_root() / DEFAULT_LOG_DIR
    return JsonlLog(base / LOG_FILENAME)


# ----------------------------------------------------------------------------------------------
# The supervisor
# ----------------------------------------------------------------------------------------------


class Runner:
    """Runs listeners concurrently in threads and restarts any that exit, logging every restart."""

    def __init__(
        self,
        listeners: Iterable[Any],
        *,
        log: JsonlLog | None = None,
        restart_backoff_seconds: float = RESTART_BACKOFF_SECONDS,
        max_restart_backoff_seconds: float = MAX_RESTART_BACKOFF_SECONDS,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        quota: QuotaManager | None = None,
    ) -> None:
        self.listeners = list(listeners)
        self.log = log
        self.restart_backoff_seconds = restart_backoff_seconds
        self.max_restart_backoff_seconds = max_restart_backoff_seconds
        self._clock = clock
        self._sleep = sleep
        #: The shared budget the listeners spend from; reported in every heartbeat so a refusal is
        #: visible in this process's own log, not only in the journal.
        self.quota = quota
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self.started_at: datetime | None = None
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        """Start one supervisor thread per listener. Idempotent per listener."""
        self.started_at = self._clock()
        self._log(
            "runner_start",
            listeners=[listener.name for listener in self.listeners],
            pid=os.getpid(),
        )
        for listener in self.listeners:
            thread = threading.Thread(
                target=self._supervise, args=(listener,), name=f"listener-{listener.name}", daemon=True
            )
            with self._lock:
                self._threads[listener.name] = thread
            thread.start()

    def stop(self, *, join_timeout: float = 10.0) -> None:
        """Ask every listener to stop and wait briefly for the threads."""
        self._stop.set()
        for thread in list(self._threads.values()):
            thread.join(timeout=join_timeout)
        self._log("runner_stop", status=self.status())

    def run(
        self, *, duration_seconds: float | None = None, heartbeat_seconds: float = 300.0
    ) -> None:
        """Start, then block until ``duration_seconds`` elapse (``None`` = until stopped)."""
        self.start()
        deadline = None if duration_seconds is None else time.monotonic() + duration_seconds
        last_heartbeat = time.monotonic()
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    break
                if heartbeat_seconds > 0 and now - last_heartbeat >= heartbeat_seconds:
                    last_heartbeat = now
                    self._log("heartbeat", status=self.status())
                if self._stop.wait(0.25):
                    break
        except KeyboardInterrupt:  # pragma: no cover - interactive use
            LOGGER.info("interrupted, stopping")
        finally:
            self.stop()

    # -- internals ----------------------------------------------------------------------------

    def _supervise(self, listener: Any) -> None:
        """Run one listener; if its loop exits while we are not stopping, restart it with backoff."""
        attempt = 0
        while not self._stop.is_set():
            try:
                listener.run(self._stop)
                if self._stop.is_set():
                    return
                reason = "listener loop returned while the runner was running"
            except Exception as exc:  # noqa: BLE001 - a listener crash must not kill the runner
                reason = f"{type(exc).__name__}: {exc}"
            attempt += 1
            with self._lock:
                listener.stats.restarts += 1
            delay = min(
                self.restart_backoff_seconds * (2 ** (attempt - 1)), self.max_restart_backoff_seconds
            )
            LOGGER.error("%s exited (%s); restarting in %.1fs", listener.name, reason, delay)
            self._log(
                "listener_restart",
                listener=listener.name,
                attempt=attempt,
                reason=reason[:300],
                backoff_seconds=delay,
            )
            if self._sleep_until_stop(delay):
                return

    def _sleep_until_stop(self, seconds: float) -> bool:
        return self._stop.wait(seconds)

    def _log(self, event: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.write(event, **fields)

    def status(self) -> dict[str, Any]:
        """Per-listener counters plus totals. The runner's uptime evidence."""
        now = self._clock()
        listeners = {listener.name: listener.stats.snapshot() for listener in self.listeners}
        return {
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "now": now.isoformat(),
            "uptime_seconds": (
                int((now - self.started_at).total_seconds()) if self.started_at else 0
            ),
            "listeners": listeners,
            "restarts_total": sum(entry["restarts"] for entry in listeners.values()),
            "events_total": sum(entry["events"] for entry in listeners.values()),
            "failures_total": sum(entry["failures"] for entry in listeners.values()),
            "quota_journal": str(self.quota.journal_path) if self.quota else None,
            "quota_breaches": self.quota.breaches() if self.quota else {},
        }

    def close(self) -> None:
        """Close any listener that owns an HTTP client."""
        for listener in self.listeners:
            close = getattr(listener, "close", None)
            if callable(close):
                close()


# ----------------------------------------------------------------------------------------------
# Replay harness
# ----------------------------------------------------------------------------------------------


@dataclass
class ReplayResult:
    """One replayed listing day: the announcement, the first article, and the measured lead."""

    name: str
    source_class: SourceClass
    announcement_at: datetime | None
    first_article_at: datetime | None
    lead_seconds: int | None
    record_id: str | None
    ref: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_class": self.source_class.value,
            "announcement_at": self.announcement_at.isoformat() if self.announcement_at else None,
            "first_article_at": self.first_article_at.isoformat() if self.first_article_at else None,
            "lead_seconds": self.lead_seconds,
            "lead_human": _human_delta(self.lead_seconds),
            "record_id": self.record_id,
            "ref": self.ref,
            "note": self.note,
        }


def _human_delta(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    sign = "+" if seconds >= 0 else "-"
    magnitude = abs(seconds)
    hours, remainder = divmod(magnitude, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{sign}{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{sign}{minutes}m{secs:02d}s"
    return f"{sign}{secs}s"


def parse_replay_body(kind: str, body: Any, *, observed_at: datetime | None = None) -> list[SignalEvent]:
    """Run a recorded response body through the **same** parser the live listener uses."""
    from agentoquant.data.early_signals import (
        bybit_listings,
        github_releases,
        google_news_rss,
        kraken_listings,
        okx_listings,
        telegram_previews,
    )

    observed_at = observed_at or utcnow()
    if kind == "bybit_announcements":
        return bybit_listings.parse_bybit_announcements(body, observed_at=observed_at)
    if kind == "okx_announcements":
        return okx_listings.parse_okx_announcements(body, observed_at=observed_at)
    if kind == "kraken_rss":
        return kraken_listings.parse_kraken_rss(str(body), observed_at=observed_at)
    if kind == "kraken_assetpairs":
        payload = body if isinstance(body, dict) else json.loads(str(body))
        pairs = kraken_listings.parse_asset_pairs(payload)
        return [
            SignalEvent(
                source_class=SourceClass.EXCHANGE_ANNOUNCEMENT,
                event_type="listing",
                raw_text_or_ref=f"kraken:assetpairs:{key}:{info['wsname'] or info['altname'] or key}",
                detected_at=observed_at,
                ticker=kraken_listings.pair_ticker(info),
                source="kraken_listings",
                observed_at=observed_at,
            )
            for key, info in sorted(pairs.items())
        ]
    if kind == "github_releases":
        payload = body if isinstance(body, list) else json.loads(str(body))
        return github_releases.parse_github_releases(payload, observed_at=observed_at)
    if kind == "google_news_rss":
        return google_news_rss.parse_google_news_rss(str(body), observed_at=observed_at)
    if kind == "telegram_preview":
        return telegram_previews.parse_telegram_preview(str(body), observed_at=observed_at)
    raise ValueError(f"unknown replay fixture kind {kind!r}")


def replay_fixture(
    fixture: dict[str, Any],
    *,
    writer: SignalWriter,
    log: JsonlLog | None = None,
) -> ReplayResult:
    """Replay one recorded fixture: parse, pick the announcement, measure the lead, write the record.

    Fixture shape (all fields real, none invented)::

        {"name": "kraken-tread-2026-09-16",
         "kind": "kraken_rss",                  # which parser to use, see parse_replay_body
         "observed_at": "2026-09-19T03:00:00Z", # when the fixture was captured
         "body": "<raw captured response>",     # string or parsed JSON
         "announcement_ref": "https://blog.kraken.com/.../tread-is-available-for-trading",
         "first_article": {"pub_date": "Wed, 16 Sep 2026 23:44:11 GMT",
                           "title": "Tread.fi Exchanges TREAD Markets - CryptoRank",
                           "source": "google_news_rss"}}

    The event's ``first_article_at`` is attached before the write, so the ledger row carries
    ``latency_seconds_vs_first_article`` - the field the Verifier (Task 11) reads.
    """

    name = str(fixture.get("name") or "fixture")
    kind = str(fixture.get("kind") or "")
    observed_at = _parse_datetime(fixture.get("observed_at")) or utcnow()
    events = parse_replay_body(kind, fixture.get("body"), observed_at=observed_at)
    if not events:
        result = ReplayResult(
            name=name,
            source_class=SourceClass.HEADLINE,
            announcement_at=None,
            first_article_at=None,
            lead_seconds=None,
            record_id=None,
            ref="",
            note=f"no events parsed from the {kind} fixture",
        )
        if log is not None:
            log.write("replay", **result.as_dict())
        return result

    wanted = fixture.get("announcement_ref")
    chosen = None
    if wanted:
        chosen = next((event for event in events if event.raw_text_or_ref == wanted), None)
        if chosen is None:
            chosen = next((event for event in events if str(wanted) in event.raw_text_or_ref), None)
    chosen = chosen or events[0]

    article = fixture.get("first_article") or {}
    first_article_at = _parse_datetime(article.get("pub_date"))
    if first_article_at is not None:
        chosen = SignalEvent(
            source_class=chosen.source_class,
            event_type=chosen.event_type,
            raw_text_or_ref=chosen.raw_text_or_ref,
            detected_at=chosen.detected_at,
            ticker=chosen.ticker,
            source=chosen.source,
            observed_at=chosen.observed_at,
            published_at=chosen.published_at,
            first_article_at=first_article_at,
        )
    record_id = writer.write(chosen)
    note = "" if record_id else "already in the ledger (duplicate skipped)"
    result = ReplayResult(
        name=name,
        source_class=chosen.source_class,
        announcement_at=chosen.detected_at,
        first_article_at=first_article_at,
        lead_seconds=chosen.latency_vs_first_article(),
        record_id=record_id,
        ref=chosen.raw_text_or_ref,
        note=note,
    )
    if log is not None:
        log.write("replay", **result.as_dict())
    return result


def _parse_datetime(value: Any) -> datetime | None:
    """ISO-8601 or RFC-822 to aware UTC; anything else is ``None`` (never guessed)."""
    from email.utils import parsedate_to_datetime

    if isinstance(value, datetime):
        return as_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    try:
        return as_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError):
        return None


def run_replay(
    fixtures: Iterable[dict[str, Any]], *, writer: SignalWriter, log: JsonlLog | None = None
) -> list[ReplayResult]:
    """Replay every fixture in order and return the measured results."""
    return [replay_fixture(fixture, writer=writer, log=log) for fixture in fixtures]


def load_fixtures(path: Path | str) -> list[dict[str, Any]]:
    """Load every ``*.json`` fixture in a directory (sorted by filename)."""
    directory = Path(path)
    if directory.is_file():
        files = [directory]
    else:
        files = sorted(directory.glob("*.json"))
    fixtures: list[dict[str, Any]] = []
    for file in files:
        data = json.loads(file.read_text(encoding="utf-8"))
        if isinstance(data, list):
            fixtures.extend(data)
        else:
            fixtures.append(data)
    return fixtures


def format_replay_report(results: Sequence[ReplayResult]) -> str:
    """A fixed-width table of the measured lead times, for the report and for the log."""
    lines = [
        f"{'case':<32} {'source class':<22} {'announcement (UTC)':<21} "
        f"{'first article (UTC)':<21} {'lead':>12}",
        "-" * 112,
    ]
    for result in results:
        lines.append(
            f"{result.name[:31]:<32} {result.source_class.value:<22} "
            f"{(result.announcement_at.isoformat() if result.announcement_at else '-'):<21} "
            f"{(result.first_article_at.isoformat() if result.first_article_at else '-'):<21} "
            f"{_human_delta(result.lead_seconds):>12}"
        )
        if result.note:
            lines.append(f"{'':<32} note: {result.note}")
    leads = [r.lead_seconds for r in results if r.lead_seconds is not None]
    ahead = sum(1 for lead in leads if lead > 0)
    lines.append("-" * 112)
    lines.append(
        f"{len(results)} case(s) replayed, {len(leads)} with a real article timestamp, "
        f"{ahead} of those with the signal ahead of the first article"
    )
    if leads:
        lines.append(f"median lead: {_human_delta(sorted(leads)[len(leads) // 2])}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentoquant.data.early_signals.runner",
        description="Run the AgentoQuant early-signal listeners (paper-only, read-only sources).",
    )
    parser.add_argument("--db", default=None, help="ledger path (default: settings.ledger_path)")
    parser.add_argument("--log-dir", default=None, help="directory for early_signals.jsonl")
    parser.add_argument("--duration", type=float, default=None, help="seconds to run (default: forever)")
    parser.add_argument("--status-every", type=float, default=300.0, help="heartbeat interval, seconds")
    parser.add_argument("--only", default=None, help="comma-separated listener names")
    parser.add_argument("--interval-scale", type=float, default=1.0, help="scale every poll interval")
    parser.add_argument("--no-onchain", action="store_true", help="skip the on-chain webhook receiver")
    parser.add_argument("--verbose", action="store_true", help="INFO logging to stderr")
    sub = parser.add_subparsers(dest="command")
    replay = sub.add_parser("replay", help="replay recorded announcement fixtures")
    replay.add_argument("--fixtures", required=True, help="fixture file or directory")
    replay.add_argument("--json", action="store_true", help="print results as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code; never raises for a listener failure."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = default_log(args.log_dir)
    store = LedgerStore(args.db) if args.db else LedgerStore()

    if args.command == "replay":
        writer = SignalWriter(store, log=log)
        results = run_replay(load_fixtures(args.fixtures), writer=writer, log=log)
        if args.json:
            print(json.dumps([result.as_dict() for result in results], indent=2))
        else:
            print(format_replay_report(results))
        return 0

    writer = SignalWriter(store, log=log)
    quota = QuotaManager()
    listeners = build_listeners(
        writer,
        log=log,
        only=args.only.split(",") if args.only else None,
        interval_scale=args.interval_scale,
        include_onchain=not args.no_onchain,
        quota=quota,
    )
    if not listeners:
        print("no listeners could be started", flush=True)
        return 2
    unbudgeted = quota_audit(listeners)
    if unbudgeted:
        LOGGER.error("listeners fetching outside the quota manager: %s", ", ".join(unbudgeted))
    log.write(
        "quota_wired",
        journal=str(quota.journal_path),
        sources=quota_sources(listeners),
        unbudgeted=unbudgeted,
    )
    runner = Runner(listeners, log=log, quota=quota)

    def _handle_signal(signum: int, _frame: Any) -> None:
        LOGGER.warning("signal %s received, stopping", signum)
        runner.stop(join_timeout=2.0)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, _handle_signal)
        except ValueError:  # pragma: no cover - not the main thread
            pass

    try:
        runner.run(duration_seconds=args.duration, heartbeat_seconds=args.status_every)
    finally:
        runner.close()
        store.close()
    print(json.dumps(runner.status(), indent=2), flush=True)
    return 0


__all__ = [
    "DEFAULT_LOG_DIR",
    "LOG_FILENAME",
    "MAX_RESTART_BACKOFF_SECONDS",
    "RESTART_BACKOFF_SECONDS",
    "ReplayResult",
    "Runner",
    "build_listeners",
    "default_log",
    "default_writer",
    "format_replay_report",
    "load_fixtures",
    "main",
    "parse_replay_body",
    "quota_audit",
    "quota_sources",
    "replay_fixture",
    "run_replay",
]


if __name__ == "__main__":  # pragma: no cover - exercised by the runner's own invocation
    raise SystemExit(main())

