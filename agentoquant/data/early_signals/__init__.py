"""Early-signal listeners: exchange listing feeds, on-chain webhooks, GitHub releases, Google News
RSS and Telegram previews. Every event lands in the ledger as a ``Stage.EARLY_SIGNAL`` record carrying
its :class:`~agentoquant.enums.SourceClass`, so the Verifier (Task 11) can rank it by source class.

Where the shared helpers live (ownership note)
---------------------------------------------
``docs/phase0_implementation_plan.md`` section 4 gives Task 4 exactly the eight listener/runner modules
plus this package marker and ``tests/test_early_signals.py``, and nothing else. Every listener needs
the same small pieces (a rate-limited, backoff-wrapped httpx fetch, the ledger write path, a poll loop
that cannot die), so they live here in the package marker rather than in a new ``_base.py`` that the
file list does not name. Moving them to their own module later is a rename plus a re-export; the
alternative, a private module outside the ownership matrix, would have been a merge risk for the
parallel Task 3 worktree. Reported as a deviation.

Timestamps (how the acceptance criteria are measurable from the ledger alone)
----------------------------------------------------------------------------
Two timestamps matter per event and both are in the ledger:

* ``payload.detected_at`` - the event's own time as the source states it (Bybit ``publishTime``, OKX
  ``pTime``, a GitHub release's ``published_at``, an RSS ``pubDate``, a Telegram message's ``datetime``,
  a webhook's block time). When a source states no time, this is the moment the listener observed it.
* the envelope's ``ts`` - when the record was written.

So ``ts - detected_at`` is the detection latency, readable with a plain SQL query, which is how
"A new Kraken or Bybit listing appears in the ledger within one minute of the announcement" is
verified. :class:`SignalEvent` also keeps ``observed_at`` (local wall clock at detection) and the
listener log records it per event.

Safety: everything here is read-only against public endpoints. Nothing in this package can place,
modify or cancel an order, move funds, or touch a Kraken private endpoint. The on-chain receiver
verifies its webhook signature and fails closed, binds to loopback only, and never speaks MTProto.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx

from agentoquant.enums import Harness, ModelFamily, SourceClass, Stage
from agentoquant.ledger.schema import EarlySignalPayload
from agentoquant.ledger.store import LedgerStore

LOGGER = logging.getLogger("agentoquant.early_signals")

#: The addendum's ``event_type`` vocabulary (``tasks/schema_scaffold_addendum.md`` section 4). A value
#: outside this set is a bug in a listener, not a new event kind, so it raises at construction time.
EVENT_TYPES: tuple[str, ...] = (
    "listing",
    "unlock",
    "large_transfer",
    "release",
    "headline",
    "policy",
)

#: Sent on every public request. Identifies the project so a rate limiter can tell us apart from abuse.
USER_AGENT = "AgentoQuant/0.1 (+https://github.com/EMZEDI/AgentoQuant) early-signal listener"

#: Ledger producer role prefix. ``producer_role`` becomes ``listener_<name>`` (Task 4 spec).
PRODUCER_ROLE_PREFIX = "listener_"


def utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Coerce a datetime to aware UTC; a naive value is treated as UTC (sources publish UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def listener_cycle_id(moment: datetime | None = None) -> str:
    """The cycle id a between-cycles listener writes under, e.g. ``2026-09-19T03Z-listen``.

    Listeners run between hourly cycles, so they do not have the hourly cycle's id. One id per hour
    keeps every signal from the same hour grouped, and the hourly cycle that follows can pull them by
    id range. The format mirrors the envelope's ``2026-09-18T14Z-0001`` convention.
    """
    moment = as_utc(moment or utcnow())
    return f"{moment:%Y-%m-%dT%H}Z-listen"


# ----------------------------------------------------------------------------------------------
# The normalized event
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """One early signal, before it becomes a ledger record.

    ``source_class`` is the Verifier's ranking key, so it is a required field, never inferred at write
    time: each listener sets the class that matches its feed (an exchange announcement endpoint is
    ``EXCHANGE_ANNOUNCEMENT``, an Alchemy/Helius webhook is ``ONCHAIN``, and so on).
    """

    source_class: SourceClass
    event_type: str
    raw_text_or_ref: str
    detected_at: datetime
    ticker: str | None = None
    #: Listener name, e.g. ``bybit_listings``. Becomes ``producer_role = "listener_<source>"``.
    source: str = ""
    #: Local wall clock when the listener observed the event. Logged, not stored in the payload.
    observed_at: datetime | None = None
    #: Publication time the source stated, when it differs from ``detected_at`` (used by the runner's
    #: latency report and by the replay harness).
    published_at: datetime | None = None
    #: When the first news article about this event appeared, if it is already known. The writer turns
    #: it into ``latency_seconds_vs_first_article`` (positive means the signal was ahead).
    first_article_at: datetime | None = None
    #: Chain block the event was observed in (on-chain webhooks only). Not part of the frozen payload,
    #: so it is logged by the receiver and kept in ``raw_text_or_ref`` for the ledger row.
    block_number: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_class, SourceClass):
            raise TypeError(f"source_class must be a SourceClass member, got {self.source_class!r}")
        if self.event_type not in EVENT_TYPES:
            raise ValueError(
                f"event_type {self.event_type!r} is outside the ledger vocabulary "
                f"({', '.join(EVENT_TYPES)})"
            )
        if not isinstance(self.raw_text_or_ref, str) or not self.raw_text_or_ref.strip():
            raise ValueError("raw_text_or_ref must be a non-empty string")
        object.__setattr__(self, "detected_at", as_utc(self.detected_at))
        for name in ("observed_at", "published_at", "first_article_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, as_utc(value))

    @property
    def ref(self) -> str:
        """Short alias for ``raw_text_or_ref``."""
        return self.raw_text_or_ref

    @property
    def dedupe_key(self) -> tuple[str, str]:
        """Stable identity of an event: (source class, source reference). Pollers re-see everything."""
        return (self.source_class.value, self.raw_text_or_ref)

    def latency_vs_first_article(self) -> int | None:
        """Seconds the signal led the first article by, or ``None`` when no article time is known."""
        if self.first_article_at is None:
            return None
        return int((self.first_article_at - self.detected_at).total_seconds())

    def payload(self) -> EarlySignalPayload:
        """The frozen ledger payload (``tasks/schema_scaffold_addendum.md`` section 4)."""
        return EarlySignalPayload(
            source_class=self.source_class,
            ticker=self.ticker,
            event_type=self.event_type,
            raw_text_or_ref=self.raw_text_or_ref,
            detected_at=self.detected_at,
            latency_seconds_vs_first_article=self.latency_vs_first_article(),
        )

    def log_fields(self) -> dict[str, Any]:
        """JSON-safe fields for the listener log line."""
        return {
            "source": self.source,
            "source_class": self.source_class.value,
            "event_type": self.event_type,
            "ticker": self.ticker,
            "ref": self.raw_text_or_ref[:400],
            "detected_at": self.detected_at.isoformat(),
            "observed_at": (self.observed_at or self.detected_at).isoformat(),
            "block_number": self.block_number,
        }


# ----------------------------------------------------------------------------------------------
# HTTP: rate limiting and backoff, httpx only (no shared quota manager: Task 3 owns that, Task 6 wires)
# ----------------------------------------------------------------------------------------------


class FetchError(RuntimeError):
    """A source could not be fetched after the configured retries. Never fatal to a listener."""


class RateLimiter:
    """Minimum interval between calls, shared by a listener's threads.

    Deliberately tiny and local: Task 3 owns the repo-wide quota manager and Task 6 wires these
    listeners into it. Until then each listener enforces its own source's ceiling from
    ``config/sources.yaml`` (Bybit/OKX 10 calls/min, Kraken listings RSS 2 calls/min, GitHub 30 calls/h,
    Google News and Telegram 6 calls/min).
    """

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        """Block until this caller may make a call, then reserve the next slot."""
        with self._lock:
            now = self._clock()
            wait = self._next_allowed - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
            self._next_allowed = now + self.min_interval_seconds


@dataclass
class HttpFetcher:
    """A small httpx wrapper: rate limited, retried with exponential backoff, redirects followed.

    ``client`` may be injected (``httpx.Client(transport=httpx.MockTransport(...))`` in tests) so the
    default suite never touches the network.
    """

    base_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 15.0
    max_retries: int = 3
    backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    rate_limiter: RateLimiter | None = None
    client: httpx.Client | None = None
    sleep: Callable[[float], None] = time.sleep
    #: Status codes worth retrying. 429 and 5xx are transient; 4xx otherwise is a permanent answer.
    retry_statuses: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

    def _client(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(
                follow_redirects=True,
                timeout=self.timeout_seconds,
                headers={"User-Agent": USER_AGENT, **self.headers},
            )
        return self.client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def backoff_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Delay before retry ``attempt`` (1-based): exponential, jittered, capped."""
        if retry_after is not None:
            return min(max(0.0, retry_after), self.max_backoff_seconds)
        raw = self.backoff_seconds * (2 ** max(0, attempt - 1))
        return min(raw, self.max_backoff_seconds) * (1.0 + random.random() * 0.1)

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One rate-limited request, retried on transport errors and transient statuses."""
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else (
            f"{self.base_url.rstrip('/')}/{path_or_url.lstrip('/')}"
        )
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            try:
                response = self._client().request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.HTTPError as exc:  # transport, timeout, DNS, TLS
                last_error = exc
                LOGGER.warning("fetch failed (%s %s) attempt %d: %s", method, url, attempt, exc)
                if attempt < self.max_retries:
                    self.sleep(self.backoff_delay(attempt))
                continue
            if response.status_code in self.retry_statuses and attempt < self.max_retries:
                retry_after = _retry_after_seconds(response)
                LOGGER.warning(
                    "fetch %s %s -> HTTP %d, retrying", method, url, response.status_code
                )
                self.sleep(self.backoff_delay(attempt, retry_after))
                continue
            if response.status_code >= 400:
                raise FetchError(f"{method} {url} -> HTTP {response.status_code}")
            return response
        raise FetchError(f"{method} {url} failed after {self.max_retries} attempts: {last_error}")

    def get_text(self, path_or_url: str, *, params: dict[str, Any] | None = None) -> str:
        return self.request("GET", path_or_url, params=params).text

    def get_json(self, path_or_url: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self.request("GET", path_or_url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"GET {path_or_url} did not return JSON") from exc


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """``Retry-After`` in seconds, when the server sent one."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


# ----------------------------------------------------------------------------------------------
# Ticker extraction (shared by the exchange and headline listeners)
# ----------------------------------------------------------------------------------------------

#: Tokens that look like a symbol but are not one. Kept small and explicit.
_TICKER_STOPWORDS: frozenset[str] = frozenset(
    {
        "USD",
        "USDT",
        "USDC",
        "EUR",
        "CAD",
        "BTC",
        "ETH",
        "OKB",
        "NEW",
        "SPOT",
        "PERP",
        "PERPETUAL",
        "TRADING",
        "LIST",
        "LISTING",
        "WILL",
        "AVAILABLE",
        "API",
        "ETF",
        "CFD",
        "IPO",
        "EEA",
        "SEO",
        "FAQ",
    }
)

_QUOTE_SUFFIXES: tuple[str, ...] = ("USDT", "USDC", "USD", "EUR", "CAD", "BTC", "ETH")

_TICKER_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\b([A-Z0-9]{2,12})/(?:USDT|USDC|USD|EUR|CAD|BTC|ETH)\b", 0),  # SLX/USDT
    (r"\b([A-Z0-9]{2,12})(?:USDT|USDC|USD|EUR|CAD)\b", 0),  # TLTUSDT
    (r"\b([A-Za-z]{2,6}) is (?:now )?available for trading\b", re.IGNORECASE),  # Kraken RSS titles
    (r"\bto list ([A-Z0-9]{2,12})\b", re.IGNORECASE),
    (r"\blist ([A-Z0-9]{2,12})\b", re.IGNORECASE),
    (r"\blaunch ([A-Z0-9]{2,12})\b", re.IGNORECASE),
    (r"\bnew listing:? ([A-Z0-9]{2,12})\b", re.IGNORECASE),
    (r"\(([A-Z]{2,6})\)", 0),  # "OKX to list OneFootball Credits (OFC)"
)


def _clean_symbol(raw: str) -> str | None:
    symbol = raw.strip().upper()
    for suffix in _QUOTE_SUFFIXES:
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            symbol = symbol[: -len(suffix)]
            break
    if len(symbol) < 2 or len(symbol) > 10 or symbol in _TICKER_STOPWORDS:
        return None
    if not any(character.isalpha() for character in symbol):
        return None
    return symbol


def extract_tickers(text: str) -> list[str]:
    """Distinct base symbols named in an announcement title, in order of appearance.

    A title that names several tokens ("OKX to list 13 new tokens in EUR and USD") yields several, and
    the caller records the event with ``ticker=None`` rather than guessing which one it was about.
    """
    found: list[str] = []
    for pattern, flags in _TICKER_PATTERNS:
        for match in re.finditer(pattern, text, flags):
            symbol = _clean_symbol(match.group(1))
            if symbol and symbol not in found:
                found.append(symbol)
    return found


def single_ticker(text: str) -> str | None:
    """The ticker when the text names exactly one symbol, else ``None`` (an unattributed signal)."""
    tickers = extract_tickers(text)
    return tickers[0] if len(tickers) == 1 else None


# ----------------------------------------------------------------------------------------------
# The ledger write path
# ----------------------------------------------------------------------------------------------


class SignalWriter:
    """Writes :class:`SignalEvent` objects as ``Stage.EARLY_SIGNAL`` ledger records.

    ``producer_role`` is ``listener_<source>`` and the model family / harness are ``NONE``: a listener
    is deterministic code, not a model call. De-duplication is by (source class, reference) because
    every poller re-sees the same announcements on every pass; the ledger has no unique constraint, so
    a duplicate would inflate the Verifier's corroboration counts.
    """

    def __init__(
        self,
        store: LedgerStore | None = None,
        *,
        cycle_id: str | None = None,
        dedupe: bool = True,
        log: JsonlLog | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self.cycle_id = cycle_id
        self.dedupe = dedupe
        self.log = log
        self._clock = clock

    @property
    def store(self) -> LedgerStore:
        if self._store is None:
            self._store = LedgerStore()
        return self._store

    def cycle_id_for(self, moment: datetime | None = None) -> str:
        return self.cycle_id or listener_cycle_id(moment or self._clock())

    def seen(self, event: SignalEvent) -> bool:
        """True when this exact event is already in the ledger."""
        if not self.dedupe:
            return False
        rows = self.store.query(
            'SELECT "record_id" FROM "early_signal" '
            'WHERE "source_class" = ? AND "raw_text_or_ref" = ? LIMIT 1',
            [event.source_class.value, event.raw_text_or_ref],
        )
        return bool(rows)

    def write(self, event: SignalEvent, *, cycle_id: str | None = None) -> str | None:
        """Write one event. Returns its ``record_id``, or ``None`` when it was a duplicate."""
        if self.seen(event):
            LOGGER.debug("duplicate early signal skipped: %s", event.dedupe_key)
            return None
        record_id = self.store.write(
            Stage.EARLY_SIGNAL,
            cycle_id or self.cycle_id_for(event.observed_at or event.detected_at),
            event.payload(),
            producer_role=f"{PRODUCER_ROLE_PREFIX}{event.source or 'unknown'}",
            producer_model_family=ModelFamily.NONE,
            harness=Harness.NONE,
        )
        if self.log is not None:
            self.log.write(
                "signal",
                record_id=record_id,
                latency_vs_first_article_s=event.latency_vs_first_article(),
                **event.log_fields(),
            )
        return record_id

    def write_all(
        self, events: Iterable[SignalEvent], *, cycle_id: str | None = None
    ) -> list[str]:
        """Write many events, skipping duplicates. Returns the ids of the rows actually written."""
        written: list[str] = []
        for event in events:
            record_id = self.write(event, cycle_id=cycle_id)
            if record_id is not None:
                written.append(record_id)
        return written

    def detection_latency_seconds(self, record_id: str) -> float | None:
        """``ts - detected_at`` for one written record: how late the ledger learned of the event."""
        rows = self.store.query(
            'SELECT "ts" AS ts, "detected_at" AS detected_at FROM "early_signal" '
            'WHERE "record_id" = ?',
            [record_id],
        )
        if not rows:
            return None
        return (as_utc(rows[0]["ts"]) - as_utc(rows[0]["detected_at"])).total_seconds()


# ----------------------------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------------------------


class JsonlLog:
    """Append-only JSON-lines log, one object per line, thread safe.

    The runner's restart/uptime evidence and every written signal go here, so "one week of listener
    uptime with restarts logged" is a file you can read, not a claim.
    """

    def __init__(self, path: Path | str | None = None, *, echo: bool = False) -> None:
        self.path = Path(path) if path is not None else None
        self.echo = echo
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"ts": utcnow().isoformat(), "event": event, **fields}
        line = json.dumps(record, default=str, ensure_ascii=False)
        if self.path is not None:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        if self.echo:
            LOGGER.info("%s", line)
        return record

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """The last ``limit`` records, for the runner's status output and for tests."""
        if self.path is None or not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:  # pragma: no cover - a torn write at most
                continue
        return records


# ----------------------------------------------------------------------------------------------
# The listener base class
# ----------------------------------------------------------------------------------------------


@dataclass
class ListenerStats:
    """Per-listener counters. Restarts and failures are here so the runner can log them."""

    polls: int = 0
    events: int = 0
    written: int = 0
    duplicates: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    restarts: int = 0
    started_at: datetime | None = None
    last_poll_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    backoff_seconds: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "polls": self.polls,
            "events": self.events,
            "written": self.written,
            "duplicates": self.duplicates,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "restarts": self.restarts,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "backoff_seconds": round(self.backoff_seconds, 3),
        }


class Listener(ABC):
    """Base class for a poller: one ``poll()`` implementation, a loop that cannot die.

    Failure policy (hard requirement 4): a listener never raises out of :meth:`poll_once` or
    :meth:`run`. It logs the failure, backs off exponentially with jitter and tries again, while the
    other listeners keep running. The runner logs a restart when a listener thread has to be replaced.
    """

    #: Listener name, used as the ledger producer role suffix and in logs.
    name: ClassVar[str] = "listener"
    #: The source class every event from this listener carries.
    source_class: ClassVar[SourceClass] = SourceClass.HEADLINE
    #: Default poll interval in seconds.
    interval_seconds: ClassVar[float] = 60.0
    #: Backoff bounds for consecutive failures.
    backoff_base_seconds: ClassVar[float] = 5.0
    max_backoff_seconds: ClassVar[float] = 900.0

    def __init__(
        self,
        writer: SignalWriter,
        *,
        interval_seconds: float | None = None,
        log: JsonlLog | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.writer = writer
        self.interval_seconds = float(
            interval_seconds if interval_seconds is not None else self.interval_seconds
        )
        self.log = log
        self._sleep = sleep
        self._clock = clock
        self.stats = ListenerStats()

    # -- to implement -------------------------------------------------------------------------

    @abstractmethod
    def poll(self) -> Sequence[SignalEvent]:
        """Fetch once and return the events found. May raise; the loop handles it."""

    # -- the loop -----------------------------------------------------------------------------

    def backoff_for(self, consecutive_failures: int) -> float:
        """Exponential backoff with jitter, capped. Never zero, so a dead source is not hammered."""
        raw = self.backoff_base_seconds * (2 ** max(0, consecutive_failures - 1))
        capped = min(raw, self.max_backoff_seconds)
        return capped * (1.0 + random.random() * 0.25)

    def poll_once(self) -> list[str]:
        """One poll, one write pass. Never raises: a failure is logged and counted."""
        self.stats.polls += 1
        self.stats.last_poll_at = self._clock()
        try:
            events = list(self.poll())
        except Exception as exc:  # noqa: BLE001 - the whole point: no listener failure may escape
            self.stats.failures += 1
            self.stats.consecutive_failures += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"[:300]
            self.stats.last_error_at = self._clock()
            self.stats.backoff_seconds = self.backoff_for(self.stats.consecutive_failures)
            LOGGER.error(
                "%s poll failed (%d in a row), backing off %.1fs: %s",
                self.name,
                self.stats.consecutive_failures,
                self.stats.backoff_seconds,
                self.stats.last_error,
            )
            if self.log is not None:
                self.log.write(
                    "poll_failed",
                    listener=self.name,
                    consecutive_failures=self.stats.consecutive_failures,
                    backoff_seconds=round(self.stats.backoff_seconds, 3),
                    error=self.stats.last_error,
                )
            return []

        self.stats.events += len(events)
        self.stats.consecutive_failures = 0
        self.stats.backoff_seconds = 0.0
        self.stats.last_success_at = self._clock()
        self.stats.last_error = None
        written = self.writer.write_all(events)
        self.stats.written += len(written)
        self.stats.duplicates += len(events) - len(written)
        if self.log is not None:
            self.log.write(
                "poll_ok",
                listener=self.name,
                events=len(events),
                written=len(written),
                duplicates=len(events) - len(written),
            )
        return written

    def run(self, stop_event: threading.Event | None = None) -> None:
        """Poll until ``stop_event`` is set. Catches everything: the runner must never see a raise."""
        stop_event = stop_event or threading.Event()
        self.stats.started_at = self._clock()
        if self.log is not None:
            self.log.write(
                "listener_start", listener=self.name, interval_seconds=self.interval_seconds
            )
        LOGGER.info("%s started (interval %.1fs)", self.name, self.interval_seconds)
        while not stop_event.is_set():
            self.poll_once()
            delay = self.stats.backoff_seconds or self.interval_seconds
            if _sleep_until(stop_event, delay, self._sleep):
                break
        if self.log is not None:
            self.log.write("listener_stop", listener=self.name, **self.stats.snapshot())
        LOGGER.info("%s stopped", self.name)


def _sleep_until(stop_event: threading.Event, seconds: float, sleep: Callable[[float], None]) -> bool:
    """Sleep up to ``seconds`` in slices, returning True as soon as ``stop_event`` is set."""
    remaining = max(0.0, seconds)
    while remaining > 0:
        if stop_event.is_set():
            return True
        slice_seconds = min(0.5, remaining)
        sleep(slice_seconds)
        remaining -= slice_seconds
    return stop_event.is_set()


__all__ = [
    "EVENT_TYPES",
    "PRODUCER_ROLE_PREFIX",
    "USER_AGENT",
    "FetchError",
    "HttpFetcher",
    "JsonlLog",
    "Listener",
    "ListenerStats",
    "RateLimiter",
    "SignalEvent",
    "SignalWriter",
    "as_utc",
    "extract_tickers",
    "listener_cycle_id",
    "single_ticker",
    "utcnow",
]

