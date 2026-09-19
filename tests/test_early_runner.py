"""Per-module tests for ``agentoquant.data.early_signals.runner`` (Task 4, Phase 0).

Offline by construction: the CLI paths are driven with the canned stub fetchers from
``tests/test_early_signals.py`` (imported, never duplicated) or with ``HttpFetcher.request`` patched to
fail, and every ledger is a scratch DuckDB file under ``tmp_path``. The replay fixtures are real captured
response shapes with real timestamps, never invented ones. No decorators in this file (see the at-sign
rule in ``.hermes.md``): the scratch ledger, the fake listeners and the fixture builders are plain
helpers.

Acceptance criterion 3 (``tasks/todo.md`` Task 4) - "Every early signal carries a source class (exchange,
on-chain, official handle, release, headline) for the Verifier" - is verified by
``test_every_early_signal_written_to_the_ledger_carries_a_source_class``: every listener is polled once
into one ledger and every row is checked against the enum, per producer role. ``OFFICIAL_HANDLE`` is not
produced by any Task 4 listener (an X post is Task 3's connector, ranked by Task 11's Verifier) - recorded
as a gap, not hidden.

Coverage map: ``build_listeners`` (the default set, the interval scale, the ``--only`` filter, the
on-chain receiver's skip-without-a-secret path and its inclusion when a secret is configured),
``default_writer``/``default_log``, the ``Runner`` lifecycle, restart backoff (including the cap) for a
listener that exits and one that raises, ``status`` and the heartbeat line, the fixture loader, the
replay harness (measured lead time, duplicate handling, an empty fixture), the ``parse_replay_body``
dispatch over every kind, the report formatter, and the CLI.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agentoquant.data.early_signals import (
    FetchError,
    HttpFetcher,
    JsonlLog,
    ListenerStats,
    SignalEvent,
    SignalWriter,
    listener_cycle_id,
)
from agentoquant.data.early_signals.bybit_listings import BybitListingsListener
from agentoquant.data.early_signals.github_releases import GithubReleasesListener
from agentoquant.data.early_signals.google_news_rss import GoogleNewsRssListener
from agentoquant.data.early_signals.kraken_listings import KrakenListingsListener
from agentoquant.data.early_signals.okx_listings import OkxListingsListener
from agentoquant.data.early_signals.onchain_webhooks import (
    ALCHEMY_SIGNATURE_HEADER,
    TokenRegistry,
    WebhookReceiver,
    alchemy_signature,
)
from agentoquant.data.early_signals.runner import (
    LOG_FILENAME,
    MAX_RESTART_BACKOFF_SECONDS,
    RESTART_BACKOFF_SECONDS,
    Runner,
    build_listeners,
    default_log,
    default_writer,
    format_replay_report,
    load_fixtures,
    main,
    parse_replay_body,
    replay_fixture,
    run_replay,
)
from agentoquant.data.early_signals.telegram_previews import TelegramPreviewsListener
from agentoquant.enums import SourceClass
from agentoquant.ledger.store import DB_PATH_ENV, LedgerStore
from tests.test_early_signals import StubFetcher, log_events, signal_rows

# ----------------------------------------------------------------------------------------------
# Plain helpers (no fixtures: this file must not emit a decorator)
# ----------------------------------------------------------------------------------------------

OBSERVED_AT = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
ANNOUNCEMENT_AT = datetime(2026, 9, 16, 18, 48, 58, tzinfo=UTC)
FIRST_ARTICLE_AT = datetime(2026, 9, 16, 23, 44, 11, tzinfo=UTC)
LEAD_SECONDS = int((FIRST_ARTICLE_AT - ANNOUNCEMENT_AT).total_seconds())
SECRET = "test-signing-secret-not-a-real-one"
WATCHED_ADDRESS = "0x1111111111111111111111111111111111111111"

KRAKEN_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Kraken Blog</title>
<item><title>TREAD is available for trading!</title>
<link>https://blog.kraken.com/tread-is-available-for-trading</link>
<category>Asset Listings</category>
<pubDate>Wed, 16 Sep 2026 18:48:58 +0000</pubDate></item>
</channel></rss>"""

KRAKEN_RSS_NO_LISTING = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Kraken Blog</title>
<item><title>Kraken monthly market review</title><link>https://blog.kraken.com/review</link>
<category>Market Update</category>
<pubDate>Tue, 15 Sep 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""

BYBIT_BODY = {
    "retCode": 0,
    "retMsg": "OK",
    "result": {
        "list": [
            {
                "title": "Bybit will list PENGU (PENGU) for spot trading",
                "url": "https://announcements.bybit.com/pengu",
                "type": {"key": "new_crypto", "title": "New Cryptocurrency"},
                "tags": ["Listing"],
                "dateTimestamp": 1789728312000,
                "publishTime": 1789728312000,
            }
        ]
    },
}

OKX_BODY = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "details": [
                {
                    "annType": "announcements-new-listings",
                    "title": "OKX to list SLX/USDT (Solstice) for spot trading",
                    "url": "https://www.okx.com/en-eu/help/okx-to-list-slx-usdt",
                    "pTime": "1783648816124",
                }
            ]
        }
    ],
}

KRAKEN_PAIRS = {
    "error": [],
    "result": {
        "TREADUSD": {"altname": "TREADUSD", "wsname": "TREAD/USD", "base": "TREAD", "quote": "ZUSD"}
    },
}

GITHUB_RELEASES = [
    {
        "tag_name": "v8.0.0",
        "name": "Bittensor 8.0.0",
        "html_url": "https://github.com/RaoFoundation/bittensor/releases/tag/v8.0.0",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-09-15T12:00:00Z",
    }
]

GOOGLE_NEWS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Tread.fi Exchanges TREAD Markets - CryptoRank</title>
<link>https://news.example.invalid/tread</link>
<pubDate>Wed, 16 Sep 2026 23:44:11 GMT</pubDate></item>
</channel></rss>"""

TELEGRAM_PAGE = (
    '<div class="tgme_widget_message js-widget_message" data-post="binance_announcements/8948">'
    '<div class="tgme_widget_message_text js-message_text">'
    "Binance will list PENGU (PENGU) for spot trading</div>"
    '<time datetime="2026-09-16T05:01:58+00:00"></time></div>'
)

ALCHEMY_BODY = {
    "type": "ADDRESS_ACTIVITY",
    "event": {
        "network": "ETH_MAINNET",
        "activity": [
            {
                "fromAddress": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "toAddress": WATCHED_ADDRESS,
                "blockNum": "0x140a1b2",
                "hash": "0xdeadbeef",
                "value": 1.5,
                "asset": "VVV",
                "category": "token",
            }
        ],
    },
    "createdAt": "2026-09-16T12:00:00.000Z",
}


class FakeClock:
    """A controllable ``datetime`` source for the runner's status output."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime(2026, 9, 19, 3, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeListener:
    """A stand-in listener: honours the stop event, can exit or raise to drive the restart path."""

    def __init__(
        self,
        name: str = "fake_listener",
        *,
        exits_immediately: bool = False,
        raises: BaseException | None = None,
        stats: ListenerStats | None = None,
    ) -> None:
        self.name = name
        self.stats = stats or ListenerStats()
        self.exits_immediately = exits_immediately
        self.raises = raises
        self.started = 0
        self.stopped = 0
        self.closed = False
        self.stop_event: threading.Event | None = None

    def run(self, stop_event: threading.Event | None = None) -> None:
        self.started += 1
        self.stop_event = stop_event or threading.Event()
        if self.raises is not None:
            raise self.raises
        if self.exits_immediately:
            return
        self.stop_event.wait(10.0)
        self.stopped += 1

    def close(self) -> None:
        self.closed = True


def scratch_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> LedgerStore:
    """A real DuckDB ledger under ``tmp_path``, plus the env override for implicit stores."""
    monkeypatch.setenv(DB_PATH_ENV, str(tmp_path / "env-ledger.duckdb"))
    return LedgerStore(db_path=tmp_path / "ledger.duckdb")


def writer_for(store: LedgerStore, tmp_path: Any) -> SignalWriter:
    return SignalWriter(store, log=JsonlLog(tmp_path / "early_signals.jsonl"))


def wait_for(predicate: Any, *, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it is true or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def no_secret_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``resolve_webhook_secret`` find nothing, whatever the box's credentials file holds."""
    monkeypatch.delenv("AGENTOQUANT_ALCHEMY_WEBHOOK_SIGNING_KEY", raising=False)
    monkeypatch.delenv("AGENTOQUANT_HELIUS_WEBHOOK_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("agentoquant.data.early_signals.onchain_webhooks.credentials", dict)


def offline_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every real fetch fail immediately: the CLI tests must not touch the network."""
    def refuse(self: HttpFetcher, method: str, url: str, **kwargs: Any) -> Any:
        raise FetchError(f"{method} {url} refused by the offline test")

    monkeypatch.setattr(HttpFetcher, "request", refuse)


# ----------------------------------------------------------------------------------------------
# build_listeners
# ----------------------------------------------------------------------------------------------


def test_build_listeners_returns_the_six_pollers_with_scaled_intervals(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    listeners = build_listeners(writer_for(store, tmp_path), interval_scale=2.0, include_onchain=False)

    assert [listener.name for listener in listeners] == [
        "bybit_listings",
        "okx_listings",
        "kraken_listings",
        "github_releases",
        "google_news_rss",
        "telegram_previews",
    ]
    assert [listener.interval_seconds for listener in listeners] == [40.0, 60.0, 40.0, 1200.0, 600.0, 120.0]
    assert [listener.source_class.value for listener in listeners] == [
        SourceClass.EXCHANGE_ANNOUNCEMENT.value,
        SourceClass.EXCHANGE_ANNOUNCEMENT.value,
        SourceClass.EXCHANGE_ANNOUNCEMENT.value,
        SourceClass.GITHUB_RELEASE.value,
        SourceClass.HEADLINE.value,
        SourceClass.TELEGRAM_PREVIEW.value,
    ]
    for listener in listeners:
        listener.close()


def test_build_listeners_skips_the_onchain_receiver_when_no_secret_is_configured(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A missing signing secret must not stop the other listeners."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    no_secret_configured(monkeypatch)

    listeners = build_listeners(writer, log=writer.log)

    assert [listener.name for listener in listeners] == [
        "bybit_listings",
        "okx_listings",
        "kraken_listings",
        "github_releases",
        "google_news_rss",
        "telegram_previews",
    ]
    unavailable = log_events(writer.log, "listener_unavailable")
    assert len(unavailable) == 1
    assert unavailable[0]["listener"] == "onchain_webhooks"
    assert "refusing to start unauthenticated" in unavailable[0]["reason"]
    for listener in listeners:
        listener.close()


def test_build_listeners_includes_the_onchain_receiver_when_a_secret_is_configured(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    monkeypatch.setenv("AGENTOQUANT_ALCHEMY_WEBHOOK_SIGNING_KEY", SECRET)

    listeners = build_listeners(writer, log=writer.log)

    assert [listener.name for listener in listeners][-1] == "onchain_webhooks"
    receiver = listeners[-1]
    assert isinstance(receiver, WebhookReceiver)
    assert receiver.path == "/webhooks/alchemy"
    assert receiver.port == 8787  # never bound until the runner starts it
    for listener in listeners:
        close = getattr(listener, "close", None)
        if callable(close):
            close()


def test_build_listeners_filters_by_name(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    only = build_listeners(writer, only=["okx_listings", "github_releases"], include_onchain=False)
    assert [listener.name for listener in only] == ["okx_listings", "github_releases"]
    assert build_listeners(writer, only=["nothing_like_this"], include_onchain=False) == []
    for listener in only:
        listener.close()


def test_default_writer_and_log_stay_off_the_repo(tmp_path: Any, monkeypatch: Any) -> None:
    env_path = scratch_ledger(tmp_path, monkeypatch).db_path
    implicit = default_writer()
    assert implicit.store.db_path == env_path, "the env override is what keeps tests off data/ledger.duckdb"
    explicit = default_writer(db_path=tmp_path / "other.duckdb")
    assert explicit.store.db_path == tmp_path / "other.duckdb"

    assert default_log(tmp_path).path == tmp_path / LOG_FILENAME
    monkeypatch.setattr("agentoquant.data.early_signals.runner.repo_root", lambda: tmp_path)
    assert default_log().path == tmp_path / "logs" / LOG_FILENAME


# ----------------------------------------------------------------------------------------------
# Runner lifecycle
# ----------------------------------------------------------------------------------------------


def test_the_runner_starts_every_listener_and_stops_them(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    first = FakeListener("first")
    second = FakeListener("second")
    runner = Runner([first, second], log=writer.log)

    runner.start()
    assert wait_for(lambda: first.started == 1 and second.started == 1)
    assert runner.started_at is not None
    assert runner.status()["listeners"]["first"]["polls"] == 0

    runner.stop(join_timeout=5.0)

    assert first.stopped == 1 and second.stopped == 1
    assert log_events(writer.log, "runner_start")[0]["listeners"] == ["first", "second"]
    assert log_events(writer.log, "runner_stop")[0]["status"]["listeners"]["first"]["restarts"] == 0


def test_a_listener_that_exits_is_restarted_with_exponential_backoff(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = FakeListener("exiter", exits_immediately=True)
    runner = Runner(
        [listener],
        log=writer.log,
        restart_backoff_seconds=0.01,
        max_restart_backoff_seconds=0.04,
    )

    runner.start()
    assert wait_for(lambda: len(log_events(writer.log, "listener_restart")) >= 3)
    runner.stop(join_timeout=5.0)

    restarts = log_events(writer.log, "listener_restart")
    assert [record["attempt"] for record in restarts[:3]] == [1, 2, 3]
    assert [record["backoff_seconds"] for record in restarts[:3]] == [0.01, 0.02, 0.04]
    assert restarts[0]["reason"] == "listener loop returned while the runner was running"
    assert listener.stats.restarts >= 3
    assert runner.status()["restarts_total"] >= 3


def test_restart_backoff_is_capped_and_a_crash_does_not_kill_the_runner(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A raising listener is restarted like any other; its healthy sibling keeps running."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    crashing = FakeListener("crasher", raises=RuntimeError("listener exploded"))
    healthy = FakeListener("healthy")
    runner = Runner(
        [crashing, healthy],
        log=writer.log,
        restart_backoff_seconds=0.05,
        max_restart_backoff_seconds=0.05,
    )

    runner.start()
    assert wait_for(lambda: len(log_events(writer.log, "listener_restart")) >= 3)
    runner.stop(join_timeout=5.0)

    restarts = log_events(writer.log, "listener_restart")
    assert {record["listener"] for record in restarts} == {"crasher"}
    assert "RuntimeError: listener exploded" in restarts[0]["reason"]
    assert [record["backoff_seconds"] for record in restarts[:3]] == [0.05, 0.05, 0.05]
    assert crashing.stats.restarts >= 3
    assert healthy.started == 1, "the healthy listener was never restarted"
    assert healthy.stopped == 1
    assert MAX_RESTART_BACKOFF_SECONDS >= RESTART_BACKOFF_SECONDS > 0


def test_the_runner_close_releases_every_listener(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    first = FakeListener("first")
    second = FakeListener("second")
    Runner([first, second]).close()
    assert first.closed is True and second.closed is True


def test_a_listener_with_no_close_is_fine(tmp_path: Any, monkeypatch: Any) -> None:
    class Bare:
        name = "bare"
        stats = ListenerStats()

    Runner([Bare()]).close()  # no close attribute: the runner must not raise


# ----------------------------------------------------------------------------------------------
# status and the heartbeat
# ----------------------------------------------------------------------------------------------


def test_status_reports_per_listener_counters_and_totals(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    clock = FakeClock()
    counted = ListenerStats(
        polls=3, events=5, written=4, duplicates=1, failures=1, consecutive_failures=0, restarts=2
    )
    listener = FakeListener("counted", stats=counted)
    runner = Runner([listener], log=writer.log, clock=clock)

    runner.start()
    assert wait_for(lambda: listener.started == 1)
    clock.advance(90.0)
    status = runner.status()
    runner.stop(join_timeout=5.0)

    assert status["started_at"] == OBSERVED_AT.isoformat()
    assert status["now"] == (OBSERVED_AT + timedelta(seconds=90)).isoformat()
    assert status["uptime_seconds"] == 90
    assert status["listeners"]["counted"] == counted.snapshot()
    assert status["listeners"]["counted"]["backoff_seconds"] == 0.0
    assert status["restarts_total"] == 2
    assert status["events_total"] == 5
    assert status["failures_total"] == 1


def test_the_heartbeat_line_carries_the_status(tmp_path: Any, monkeypatch: Any) -> None:
    """One week of uptime is then one file to read, not a claim."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = FakeListener("heartbeat_probe")
    runner = Runner([listener], log=writer.log)

    runner.run(duration_seconds=0.6, heartbeat_seconds=0.1)

    beats = log_events(writer.log, "heartbeat")
    assert len(beats) >= 2
    assert beats[0]["status"]["listeners"]["heartbeat_probe"]["polls"] == 0
    assert beats[0]["status"]["started_at"] is not None
    assert log_events(writer.log, "runner_stop"), "the runner must stop itself at the deadline"
    assert listener.stopped == 1


# ----------------------------------------------------------------------------------------------
# Fixtures and the replay harness
# ----------------------------------------------------------------------------------------------


def kraken_fixture() -> dict[str, Any]:
    """A replay fixture in the documented shape: a real body plus the real first-article time."""
    return {
        "name": "kraken-tread-2026-09-16",
        "kind": "kraken_rss",
        "observed_at": "2026-09-19T03:00:00Z",
        "body": KRAKEN_RSS,
        "announcement_ref": "https://blog.kraken.com/tread-is-available-for-trading",
        "first_article": {
            "pub_date": "Wed, 16 Sep 2026 23:44:11 GMT",
            "title": "Tread.fi Exchanges TREAD Markets - CryptoRank",
            "source": "google_news_rss",
        },
    }


def test_load_fixtures_reads_a_directory_and_a_single_file(tmp_path: Any) -> None:
    (tmp_path / "a.json").write_text(json.dumps(kraken_fixture()), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps([kraken_fixture(), kraken_fixture()]), encoding="utf-8")

    from_directory = load_fixtures(tmp_path)
    assert len(from_directory) == 3
    assert load_fixtures(tmp_path / "a.json") == [kraken_fixture()]


def test_replay_fixture_measures_the_lead_and_writes_the_row(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)

    result = replay_fixture(kraken_fixture(), writer=writer, log=writer.log)

    assert result.lead_seconds == LEAD_SECONDS
    assert result.lead_seconds > 0, "the listing signal must lead the first article"
    assert result.announcement_at == ANNOUNCEMENT_AT
    assert result.first_article_at == FIRST_ARTICLE_AT
    assert result.source_class is SourceClass.EXCHANGE_ANNOUNCEMENT
    assert result.record_id is not None
    assert result.note == ""

    row = signal_rows(store)[0]
    assert row["latency"] == LEAD_SECONDS
    assert row["source_class"] == SourceClass.EXCHANGE_ANNOUNCEMENT.value
    assert row["ticker"] == "TREAD"
    assert row["cycle_id"] == listener_cycle_id(OBSERVED_AT)
    assert log_events(writer.log, "replay")[0]["lead_seconds"] == LEAD_SECONDS

    again = replay_fixture(kraken_fixture(), writer=writer)
    assert again.record_id is None
    assert again.note == "already in the ledger (duplicate skipped)"
    assert len(signal_rows(store)) == 1


def test_replay_reports_an_empty_fixture_rather_than_inventing_a_lead(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    fixture = kraken_fixture()
    fixture["body"] = KRAKEN_RSS_NO_LISTING
    fixture["name"] = "no-listing-that-day"

    result = replay_fixture(fixture, writer=writer)

    assert result.record_id is None
    assert result.lead_seconds is None
    assert result.announcement_at is None
    assert result.note == "no events parsed from the kraken_rss fixture"
    assert signal_rows(store) == []


def test_run_replay_handles_a_list_of_fixtures(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    fixtures = [kraken_fixture(), kraken_fixture()]
    fixtures[1]["name"] = "kraken-tread-second-case"

    results = run_replay(fixtures, writer=writer, log=writer.log)

    assert [result.name for result in results] == ["kraken-tread-2026-09-16", "kraken-tread-second-case"]
    assert results[0].record_id is not None
    assert results[1].record_id is None  # the same event, so the second case is a duplicate


def test_parse_replay_body_dispatches_every_fixture_kind() -> None:
    """The replay path must use the same parser the live listener uses, for every kind."""
    cases: list[tuple[str, Any, str]] = [
        ("bybit_announcements", BYBIT_BODY, SourceClass.EXCHANGE_ANNOUNCEMENT.value),
        ("okx_announcements", OKX_BODY, SourceClass.EXCHANGE_ANNOUNCEMENT.value),
        ("kraken_rss", KRAKEN_RSS, SourceClass.EXCHANGE_ANNOUNCEMENT.value),
        ("kraken_assetpairs", KRAKEN_PAIRS, SourceClass.EXCHANGE_ANNOUNCEMENT.value),
        ("github_releases", GITHUB_RELEASES, SourceClass.GITHUB_RELEASE.value),
        ("google_news_rss", GOOGLE_NEWS, SourceClass.HEADLINE.value),
        ("telegram_preview", TELEGRAM_PAGE, SourceClass.TELEGRAM_PREVIEW.value),
    ]
    for kind, body, expected_class in cases:
        events = parse_replay_body(kind, body, observed_at=OBSERVED_AT)
        assert events, kind
        assert {event.source_class.value for event in events} == {expected_class}, kind
        assert all(event.event_type for event in events), kind

    with pytest.raises(ValueError, match="unknown replay fixture kind"):
        parse_replay_body("not_a_kind", BYBIT_BODY)


def test_parse_replay_body_accepts_json_encoded_bodies() -> None:
    """A fixture file may hold the body as a string; the replay path must not care."""
    assert parse_replay_body("github_releases", json.dumps(GITHUB_RELEASES))[0].ticker is None
    assert parse_replay_body("kraken_assetpairs", json.dumps(KRAKEN_PAIRS))[0].ticker == "TREAD"


def test_format_replay_report_prints_the_measured_lead(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    result = replay_fixture(kraken_fixture(), writer=writer_for(store, tmp_path))

    report = format_replay_report([result])

    assert "kraken-tread-2026-09-16" in report
    assert "exchange_announcement" in report
    assert "+4h55m13s" in report
    assert "1 case(s) replayed, 1 with a real article timestamp, 1 of those with the signal ahead" in report
    assert "median lead: +4h55m13s" in report


# ----------------------------------------------------------------------------------------------
# The CLI
# ----------------------------------------------------------------------------------------------


def test_the_replay_cli_writes_the_ledger_and_prints_the_report(
    tmp_path: Any, monkeypatch: Any, capsys: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "kraken.json").write_text(json.dumps(kraken_fixture()), encoding="utf-8")
    log_dir = tmp_path / "logs"

    code = main(
        ["replay", "--fixtures", str(fixtures), "--db", str(store.db_path), "--log-dir", str(log_dir)]
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert "kraken-tread-2026-09-16" in printed
    assert "+4h55m13s" in printed
    assert signal_rows(store)[0]["latency"] == LEAD_SECONDS
    assert (log_dir / LOG_FILENAME).exists()

    code = main(
        [
            "replay",
            "--fixtures",
            str(fixtures),
            "--db",
            str(store.db_path),
            "--log-dir",
            str(log_dir),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "kraken-tread-2026-09-16"
    assert payload[0]["lead_seconds"] == LEAD_SECONDS
    assert payload[0]["record_id"] is None  # already replayed above
    assert payload[0]["note"] == "already in the ledger (duplicate skipped)"


def test_the_cli_reports_when_no_listener_could_be_started(
    tmp_path: Any, monkeypatch: Any, capsys: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    code = main(
        [
            "--only",
            "nothing_like_this",
            "--no-onchain",
            "--db",
            str(store.db_path),
            "--log-dir",
            str(tmp_path / "logs"),
        ]
    )
    assert code == 2
    assert "no listeners could be started" in capsys.readouterr().out


def test_the_cli_runs_a_short_offline_duration(tmp_path: Any, monkeypatch: Any, capsys: Any) -> None:
    """The uptime path: listeners start, a failing source is counted, and the status is printed."""
    store = scratch_ledger(tmp_path, monkeypatch)
    offline_http(monkeypatch)
    log_dir = tmp_path / "logs"

    code = main(
        [
            "--duration",
            "0.4",
            "--only",
            "okx_listings",
            "--no-onchain",
            "--interval-scale",
            "1000",
            "--db",
            str(store.db_path),
            "--log-dir",
            str(log_dir),
        ]
    )

    assert code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["listeners"]["okx_listings"]["polls"] >= 1
    assert status["listeners"]["okx_listings"]["failures"] >= 1
    assert signal_rows(store) == [], "a failed fetch writes nothing"
    log = JsonlLog(log_dir / LOG_FILENAME)
    assert log_events(log, "listener_start")[0]["listener"] == "okx_listings"
    assert log_events(log, "poll_failed")[0]["listener"] == "okx_listings"
    assert log_events(log, "listener_stop")


# ----------------------------------------------------------------------------------------------
# Acceptance criterion 3: every early signal carries a source class
# ----------------------------------------------------------------------------------------------


def test_every_early_signal_written_to_the_ledger_carries_a_source_class(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Acceptance criterion 3, verified across every listener that can write an early signal.

    Each listener is polled once into one ledger with canned bodies, and every row is checked: the
    ``source_class`` column is non-null, is a member of the frozen enum, and is the class the writing
    listener declares. A regression that wrote a bare string, or omitted the class, fails here.
    """
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)

    bybit = BybitListingsListener(writer, fetcher=StubFetcher(jsons={"/v5/announcements/index": BYBIT_BODY}))
    okx = OkxListingsListener(writer, fetcher=StubFetcher(jsons={"/api/v5/support/announcements": OKX_BODY}))
    kraken = KrakenListingsListener(
        writer,
        state_path=tmp_path / "kraken_pairs.json",
        pairs_fetcher=StubFetcher(jsons={"/0/public/AssetPairs": KRAKEN_PAIRS}),
        rss_fetcher=StubFetcher(texts={"/feed/": KRAKEN_RSS}),
    )
    github = GithubReleasesListener(
        writer, fetcher=StubFetcher(jsons={"/repos/RaoFoundation/bittensor/releases": GITHUB_RELEASES})
    )
    news = GoogleNewsRssListener(
        writer, tickers=("TAO",), fetcher=StubFetcher(texts={"TAO crypto": GOOGLE_NEWS})
    )
    telegram = TelegramPreviewsListener(
        writer, fetcher=StubFetcher(texts={"/s/binance_announcements": TELEGRAM_PAGE})
    )
    receiver = WebhookReceiver(
        writer,
        provider="alchemy",
        secret=SECRET,
        registry=TokenRegistry.from_mapping({WATCHED_ADDRESS: "VVV"}),
    )

    pollers: list[tuple[Any, str]] = [
        (bybit, SourceClass.EXCHANGE_ANNOUNCEMENT.value),
        (okx, SourceClass.EXCHANGE_ANNOUNCEMENT.value),
        (kraken, SourceClass.EXCHANGE_ANNOUNCEMENT.value),
        (github, SourceClass.GITHUB_RELEASE.value),
        (news, SourceClass.HEADLINE.value),
        (telegram, SourceClass.TELEGRAM_PREVIEW.value),
    ]
    written = 0
    for listener, _expected in pollers:
        written += len(listener.poll_once())
        listener.close()

    body = json.dumps(ALCHEMY_BODY).encode("utf-8")
    accepted = receiver.handle(body, {ALCHEMY_SIGNATURE_HEADER: alchemy_signature(body, SECRET)})
    assert accepted.status == 200
    written += accepted.body["written"]

    rows = signal_rows(store)
    assert written == len(rows) >= 7
    allowed = {member.value for member in SourceClass}
    for row in rows:
        assert row["source_class"] in allowed, row
        assert row["source_class"], row
        assert row["producer_role"].startswith("listener_"), row
    by_role = {row["producer_role"]: row["source_class"] for row in rows}
    assert by_role == {
        "listener_bybit_listings": SourceClass.EXCHANGE_ANNOUNCEMENT.value,
        "listener_okx_listings": SourceClass.EXCHANGE_ANNOUNCEMENT.value,
        "listener_kraken_listings": SourceClass.EXCHANGE_ANNOUNCEMENT.value,
        "listener_github_releases": SourceClass.GITHUB_RELEASE.value,
        "listener_google_news_rss": SourceClass.HEADLINE.value,
        "listener_telegram_previews": SourceClass.TELEGRAM_PREVIEW.value,
        "listener_onchain_webhooks": SourceClass.ONCHAIN.value,
    }
    # GAP: no Task 4 listener produces OFFICIAL_HANDLE (an X post is Task 3's connector, ranked by
    # Task 11's Verifier), and no listener produces CORROBORATED_TWO_SOURCE (that is the Verifier's).
    assert SourceClass.OFFICIAL_HANDLE.value not in set(by_role.values())
    assert SourceClass.CORROBORATED_TWO_SOURCE.value not in set(by_role.values())
    assert len(by_role) == 7


def test_the_source_class_is_required_by_the_event_type_itself() -> None:
    """The class cannot be defaulted away: constructing an event without one is a TypeError."""
    with pytest.raises(TypeError):
        SignalEvent(event_type="headline", raw_text_or_ref="x", detected_at=OBSERVED_AT)
    with pytest.raises(TypeError):
        SignalEvent(
            source_class="headline", event_type="headline", raw_text_or_ref="x", detected_at=OBSERVED_AT
        )
