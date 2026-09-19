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
bodies (``RecordingTransport``) or through a canned stub fetcher (``StubFetcher``); ledger writes go
to a DuckDB file under ``tmp_path``. Nothing places an order, moves funds, or speaks MTProto - the
listeners are read-only by construction. The only socket opened anywhere in this file is a loopback
one, by the test that serves the webhook receiver on ``127.0.0.1`` with a port the OS picks.

Coverage map (one section per module under ``agentoquant/data/early_signals/``):

* ``__init__.py``      - SignalEvent validation, ticker extraction, RateLimiter, HttpFetcher,
                         SignalWriter (dedupe + ledger write path), JsonlLog, ListenerStats, Listener
* ``bybit_listings``   - the pre-existing parser/listener tests, kept
* ``okx_listings``     - parser, malformed bodies, per-type poll, non-zero code
* ``kraken_listings``  - RSS parsing and failure, AssetPairs diffing, baseline-on-first-run, state file
* ``github_releases``  - parser, drafts/prereleases, per-repo failure isolation, token header
* ``google_news_rss``  - parser, per-ticker feeds, dedupe, article-time helpers
* ``telegram_previews``- ``t.me/s`` parsing, event classification, no-preview and failure paths
* ``onchain_webhooks`` - signatures, registry, Alchemy/Helius payloads, attribution, block lag,
                         the loopback server
* ``runner``           - build_listeners, Runner lifecycle, restart backoff, status/heartbeat,
                         fixture replay

GAP, REPORTED NOT HIDDEN: the on-chain receiver measures and returns a per-event block lag but
nothing enforces the "within two blocks" bound, and the frozen ``EarlySignalPayload`` has no
block column (the block number lives inside ``raw_text_or_ref``). See
``test_onchain_block_lag_is_reported_but_not_enforced`` and ``test_onchain_block_is_not_a_ledger_column``.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentoquant.data.early_signals import (
    EVENT_TYPES,
    FetchError,
    HttpFetcher,
    JsonlLog,
    Listener,
    RateLimiter,
    SignalEvent,
    SignalWriter,
    as_utc,
    extract_tickers,
    listener_cycle_id,
    single_ticker,
)
from agentoquant.enums import SourceClass, Stage
from agentoquant.ledger.schema import EarlySignalPayload
from agentoquant.ledger.store import DB_PATH_ENV, LedgerStore

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


class SequencedTransport(httpx.BaseTransport):
    """Replays a queue of responses in order, then repeats the last one. For retry/backoff tests.

    Each item is ``(status, body)`` (like :class:`RecordingTransport`), a ready-made
    :class:`httpx.Response`, or an exception instance to raise as a transport error.
    """

    def __init__(self, *responses: Any) -> None:
        self.responses: list[Any] = list(responses)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        status, body = item
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)


class StubFetcher:
    """A stand-in for :class:`HttpFetcher` with canned per-URL/per-query answers.

    Used where a test needs a *specific* request to fail (one repo, one ticker, one channel) - the
    path-keyed transports cannot express that. Raising :class:`FetchError` is what the real fetcher
    does for a permanent HTTP error, so the listeners' failure branches are exercised for real.
    """

    def __init__(
        self,
        *,
        texts: dict[str, str] | None = None,
        jsons: dict[str, Any] | None = None,
        default_text: str | None = None,
        default_json: Any = None,
    ) -> None:
        self.texts = dict(texts or {})
        self.jsons = dict(jsons or {})
        self.default_text = default_text
        self.default_json = default_json
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def get_text(self, path_or_url: str, *, params: dict[str, Any] | None = None) -> str:
        params = dict(params or {})
        self.calls.append(("text", path_or_url, params))
        key = params.get("q") or path_or_url
        if key in self.texts:
            return self.texts[key]
        if self.default_text is not None:
            return self.default_text
        raise FetchError(f"GET {path_or_url} -> HTTP 404 (no canned text)")

    def get_json(self, path_or_url: str, *, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        self.calls.append(("json", path_or_url, params))
        if path_or_url in self.jsons:
            return self.jsons[path_or_url]
        if self.default_json is not None:
            return self.default_json
        raise FetchError(f"GET {path_or_url} -> HTTP 404 (no canned json)")

    def close(self) -> None:
        self.closed = True


def make_fetcher(
    transport: RecordingTransport | SequencedTransport, *, base_url: str = "", **kwargs: Any
) -> HttpFetcher:
    """An :class:`HttpFetcher` whose only client is the canned transport."""
    return HttpFetcher(
        base_url=base_url,
        client=httpx.Client(transport=transport),
        rate_limiter=None,
        max_retries=int(kwargs.pop("max_retries", 1)),
        sleep=lambda _seconds: None,
        **kwargs,
    )


class FakeMonotonic:
    """A controllable ``time.monotonic`` for the rate limiter and backoff tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class RecordingSleep:
    """A stand-in for ``time.sleep`` that records the delays instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(float(seconds))


class StopAfterSleeps:
    """A ``sleep`` stand-in that sets the stop event after N calls: drives a listener loop in tests."""

    def __init__(self, stop_event: threading.Event, after: int) -> None:
        self.stop_event = stop_event
        self.after = after
        self.calls = 0
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(float(seconds))
        self.calls += 1
        if self.calls >= self.after:
            self.stop_event.set()


class CountingLimiter:
    """Stands in for :class:`RateLimiter` and counts ``acquire`` calls."""

    def __init__(self) -> None:
        self.acquires = 0

    def acquire(self) -> None:
        self.acquires += 1


# ----------------------------------------------------------------------------------------------
# Ledger helpers
# ----------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def scratch_ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the *default* ledger path at ``tmp_path``, so no test can reach ``data/ledger.duckdb``.

    Autouse on purpose: any listener, runner or ``SignalWriter()`` built without an explicit store
    resolves its path through ``AGENTOQUANT_LEDGER_PATH``, and it must resolve to a scratch file.
    """
    path = tmp_path / "env-ledger.duckdb"
    monkeypatch.setenv(DB_PATH_ENV, str(path))
    return path


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


def early_signal_columns(store: LedgerStore) -> set[str]:
    """The physical column names of the ``early_signal`` table."""
    return {name for name, _kind in store.table_columns(Stage.EARLY_SIGNAL)}


def make_event(**overrides: Any) -> SignalEvent:
    """A minimal valid event, with any field overridden per test."""
    fields: dict[str, Any] = {
        "source_class": SourceClass.HEADLINE,
        "event_type": "headline",
        "raw_text_or_ref": "https://news.example.invalid/article-1",
        "detected_at": datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
        "ticker": "PENGU",
        "source": "test",
    }
    fields.update(overrides)
    return SignalEvent(**fields)


class StubListener(Listener):
    """A :class:`Listener` whose ``poll`` returns canned events or raises a canned error."""

    name = "stub_listener"
    source_class = SourceClass.HEADLINE
    interval_seconds = 30.0

    def __init__(
        self,
        writer: SignalWriter,
        *,
        events: Any = (),
        error: BaseException | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(writer, **kwargs)
        self._events = list(events)
        self._error = error

    def poll(self) -> list[SignalEvent]:
        if self._error is not None:
            raise self._error
        return list(self._events)


# ----------------------------------------------------------------------------------------------
# Shared core: SignalEvent, ticker extraction, timestamps
# ----------------------------------------------------------------------------------------------


def test_event_type_vocabulary_matches_the_addendum() -> None:
    """``event_type`` is a frozen vocabulary; a listener cannot invent a seventh kind."""
    assert set(EVENT_TYPES) == {
        "listing",
        "unlock",
        "large_transfer",
        "release",
        "headline",
        "policy",
    }


def test_signal_event_rejects_a_non_source_class() -> None:
    """The Verifier's ranking key is required and typed, never a bare string."""
    with pytest.raises(TypeError):
        make_event(source_class="headline")


def test_signal_event_rejects_an_event_type_outside_the_vocabulary() -> None:
    with pytest.raises(ValueError, match="outside the ledger vocabulary"):
        make_event(event_type="delisting")


def test_signal_event_rejects_a_blank_reference() -> None:
    with pytest.raises(ValueError, match="raw_text_or_ref"):
        make_event(raw_text_or_ref="   ")


def test_signal_event_coerces_naive_timestamps_to_utc() -> None:
    naive = datetime(2026, 9, 16, 12, 0)
    event = make_event(detected_at=naive, observed_at=naive, published_at=naive)
    assert event.detected_at == datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    assert event.observed_at.tzinfo is UTC
    assert event.published_at.tzinfo is UTC
    assert event.first_article_at is None


def test_signal_event_dedupe_key_is_class_and_reference() -> None:
    event = make_event(source_class=SourceClass.ONCHAIN, raw_text_or_ref="helius:SOLANA:1 tx")
    assert event.dedupe_key == ("onchain", "helius:SOLANA:1 tx")
    assert event.ref == event.raw_text_or_ref


def test_signal_event_latency_vs_first_article() -> None:
    announcement = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    assert make_event(detected_at=announcement).latency_vs_first_article() is None
    ahead = make_event(
        detected_at=announcement, first_article_at=announcement + timedelta(minutes=45)
    )
    assert ahead.latency_vs_first_article() == 2700
    behind = make_event(
        detected_at=announcement, first_article_at=announcement - timedelta(seconds=30)
    )
    assert behind.latency_vs_first_article() == -30


def test_signal_event_payload_is_the_frozen_early_signal_payload() -> None:
    event = make_event(
        source_class=SourceClass.EXCHANGE_ANNOUNCEMENT,
        event_type="listing",
        first_article_at=datetime(2026, 9, 16, 13, 0, tzinfo=UTC),
    )
    payload = event.payload()
    assert isinstance(payload, EarlySignalPayload)
    assert payload.source_class is SourceClass.EXCHANGE_ANNOUNCEMENT
    assert payload.ticker == "PENGU"
    assert payload.event_type == "listing"
    assert payload.raw_text_or_ref == event.raw_text_or_ref
    assert payload.detected_at == event.detected_at
    assert payload.latency_seconds_vs_first_article == 3600


def test_signal_event_log_fields_are_json_safe() -> None:
    event = make_event(block_number=21_000_001)
    fields = event.log_fields()
    assert json.loads(json.dumps(fields)) == fields
    assert fields["block_number"] == 21_000_001
    assert fields["source_class"] == "headline"
    assert fields["observed_at"] == event.detected_at.isoformat()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bybit will list PENGU (PENGU) for spot trading", ["PENGU"]),
        ("OKX to list SLX/USDT (Solstice) for spot trading", ["SLX"]),
        ("Bybit will delist XYZUSDT perpetual", ["XYZ"]),
        ("TREAD is available for trading!", ["TREAD"]),
        ("FOO is now available for trading", ["FOO"]),
        ("Kraken new listing: BAR", ["BAR"]),
        ("Binance will launch BAZ", ["BAZ"]),
        ("OKX to list AAA/USDT and BBB/USDT", ["AAA", "BBB"]),
    ],
)
def test_extract_tickers_recognises_the_announcement_shapes(text: str, expected: list[str]) -> None:
    assert extract_tickers(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Bybit will list NEW tokens",
        "Trading fee update for all pairs",
        "OKX to list 13 new tokens in EUR and USD",
        "",
    ],
)
def test_extract_tickers_drops_stopwords_and_numeric_noise(text: str) -> None:
    assert extract_tickers(text) == []


def test_single_ticker_is_none_when_several_symbols_are_named() -> None:
    """An announcement naming several tokens is recorded unattributed, not guessed."""
    assert single_ticker("OKX to list AAA/USDT and BBB/USDT") is None
    assert single_ticker("TREAD is available for trading!") == "TREAD"


def test_listener_cycle_id_groups_signals_by_hour() -> None:
    assert listener_cycle_id(datetime(2026, 9, 19, 3, 45, tzinfo=UTC)) == "2026-09-19T03Z-listen"
    assert listener_cycle_id(datetime(2026, 9, 19, 3, 45)) == "2026-09-19T03Z-listen"


def test_as_utc_treats_a_naive_value_as_utc() -> None:
    assert as_utc(datetime(2026, 9, 19, 3, 0)) == datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
    assert as_utc(datetime(2026, 9, 19, 5, 0, tzinfo=timezone_plus_two())).hour == 3


def timezone_plus_two() -> Any:
    """A fixed +02:00 tzinfo, without pulling in ``zoneinfo`` (no tzdata dependency in tests)."""
    return timezone(timedelta(hours=2))


# ----------------------------------------------------------------------------------------------
# Shared core: RateLimiter
# ----------------------------------------------------------------------------------------------


def test_rate_limiter_waits_between_calls() -> None:
    clock = FakeMonotonic()
    slept = RecordingSleep()
    limiter = RateLimiter(2.0, clock=clock, sleep=lambda seconds: (slept(seconds), clock.advance(seconds)))

    limiter.acquire()
    assert slept.delays == []  # the first call never waits
    limiter.acquire()
    assert slept.delays == [2.0]
    limiter.acquire()
    assert slept.delays == [2.0, 2.0]


def test_rate_limiter_clamps_a_negative_interval() -> None:
    slept = RecordingSleep()
    limiter = RateLimiter(-5.0, clock=FakeMonotonic(), sleep=slept)
    assert limiter.min_interval_seconds == 0.0
    limiter.acquire()
    limiter.acquire()
    assert slept.delays == []


# ----------------------------------------------------------------------------------------------
# Shared core: HttpFetcher (retries, backoff, rate limiting)
# ----------------------------------------------------------------------------------------------


def test_fetcher_retries_a_transient_status_then_succeeds() -> None:
    transport = SequencedTransport((503, {"error": "unavailable"}), (200, {"ok": True}))
    slept = RecordingSleep()
    fetcher = HttpFetcher(
        base_url="https://source.example.invalid",
        client=httpx.Client(transport=transport),
        max_retries=3,
        sleep=slept,
    )
    assert fetcher.get_json("/v1/thing") == {"ok": True}
    assert len(transport.requests) == 2
    assert 1.0 <= slept.delays[0] <= 1.1


def test_fetcher_honours_retry_after() -> None:
    throttled = httpx.Response(429, headers={"retry-after": "7"}, json={"error": "slow down"})
    transport = SequencedTransport(throttled, (200, {"ok": True}))
    slept = RecordingSleep()
    fetcher = HttpFetcher(
        base_url="https://source.example.invalid",
        client=httpx.Client(transport=transport),
        max_retries=2,
        sleep=slept,
    )
    assert fetcher.get_json("/v1/thing") == {"ok": True}
    assert slept.delays == [7.0]


def test_fetcher_gives_up_after_max_retries() -> None:
    transport = SequencedTransport((500, {"error": "boom"}))
    fetcher = HttpFetcher(
        base_url="https://source.example.invalid",
        client=httpx.Client(transport=transport),
        max_retries=2,
        sleep=RecordingSleep(),
    )
    with pytest.raises(FetchError, match="HTTP 500"):
        fetcher.get_json("/v1/thing")
    assert len(transport.requests) == 2


def test_fetcher_retries_a_transport_error_then_raises() -> None:
    transport = SequencedTransport(httpx.ConnectError("no route to host"))
    fetcher = HttpFetcher(
        base_url="https://source.example.invalid",
        client=httpx.Client(transport=transport),
        max_retries=3,
        sleep=RecordingSleep(),
    )
    with pytest.raises(FetchError, match="failed after 3 attempts"):
        fetcher.get_text("/v1/thing")
    assert len(transport.requests) == 3


def test_fetcher_does_not_retry_a_permanent_4xx() -> None:
    transport = SequencedTransport((404, {"error": "gone"}))
    fetcher = HttpFetcher(
        base_url="https://source.example.invalid",
        client=httpx.Client(transport=transport),
        max_retries=3,
        sleep=RecordingSleep(),
    )
    with pytest.raises(FetchError, match="HTTP 404"):
        fetcher.get_json("/v1/thing")
    assert len(transport.requests) == 1


def test_fetcher_raises_on_a_non_json_body() -> None:
    fetcher = make_fetcher(
        RecordingTransport().add("/v1/thing", 200, "<html>not json</html>"),
        base_url="https://source.example.invalid",
    )
    with pytest.raises(FetchError, match="did not return JSON"):
        fetcher.get_json("/v1/thing")


def test_fetcher_uses_the_rate_limiter_on_every_attempt() -> None:
    limiter = CountingLimiter()
    transport = SequencedTransport((503, {}), (200, {"ok": True}))
    fetcher = HttpFetcher(
        base_url="https://source.example.invalid",
        client=httpx.Client(transport=transport),
        rate_limiter=limiter,
        max_retries=3,
        sleep=RecordingSleep(),
    )
    fetcher.get_json("/v1/thing")
    assert limiter.acquires == 2


def test_fetcher_backoff_is_exponential_jittered_and_capped() -> None:
    fetcher = HttpFetcher(backoff_seconds=1.0, max_backoff_seconds=10.0)
    assert 1.0 <= fetcher.backoff_delay(1) <= 1.1
    assert 2.0 <= fetcher.backoff_delay(2) <= 2.2
    assert 8.0 <= fetcher.backoff_delay(4) <= 8.8
    assert 10.0 <= fetcher.backoff_delay(20) <= 11.0  # capped, then jittered
    assert fetcher.backoff_delay(1, retry_after=100.0) == 10.0  # Retry-After is capped too


def test_fetcher_joins_the_base_url_and_accepts_an_absolute_url() -> None:
    transport = (
        RecordingTransport()
        .add("/api/v1/thing", 200, {"ok": True})
        .add("/absolute", 200, {"ok": True})
    )
    fetcher = make_fetcher(transport, base_url="https://source.example.invalid")
    fetcher.get_json("/api/v1/thing")
    fetcher.get_json("https://other.example.invalid/absolute")
    assert [str(request.url) for request in transport.requests] == [
        "https://source.example.invalid/api/v1/thing",
        "https://other.example.invalid/absolute",
    ]


def test_fetcher_with_zero_retries_fails_immediately() -> None:
    transport = SequencedTransport((200, {"ok": True}))
    fetcher = HttpFetcher(
        base_url="https://source.example.invalid",
        client=httpx.Client(transport=transport),
        max_retries=0,
    )
    with pytest.raises(FetchError):
        fetcher.get_json("/v1/thing")
    assert transport.requests == []


def test_fetcher_close_releases_the_client() -> None:
    fetcher = make_fetcher(RecordingTransport())
    assert fetcher.client is not None
    fetcher.close()
    assert fetcher.client is None


# __SENTINEL__
