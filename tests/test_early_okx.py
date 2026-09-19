"""Per-module tests for ``agentoquant.data.early_signals.okx_listings`` (Task 4, Phase 0).

Offline by construction: every request goes through the canned transports and stub fetchers defined in
``tests/test_early_signals.py`` (imported, never duplicated) and the ledger is a DuckDB file under
``tmp_path`` - the repo's ``data/ledger.duckdb`` is never opened. This file deliberately carries no
pytest decorators (see the at-sign rule in ``.hermes.md``): the scratch ledger and the listener
factories are plain helpers that each test calls explicitly, and the case tables are plain loops.

Acceptance criterion 1 (``tasks/todo.md`` Task 4) - "A new Kraken or Bybit listing appears in the ledger
within one minute of the announcement" - is verified here for the OKX announcement endpoint by
``test_okx_listing_reaches_the_ledger_within_one_poll_interval``: the announcement time comes from the
source's own ``pTime``, and the ledger row's ``ts - detected_at`` is bounded by the listener's poll
interval.

Coverage map: parser against a real-shaped body, malformed / empty / wrong-type bodies, the
``pTime`` fallback, one call per announcement type, ``poll_once`` ledger writes with source class and
producer role, dedupe on a repeated poll, failure accounting with a failing fetch (failures,
consecutive_failures, backoff, recovery), a non-zero API code counted as a failure, the run loop, and
close().
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agentoquant.data.early_signals import (
    JsonlLog,
    SignalWriter,
    utcnow,
)
from agentoquant.data.early_signals.okx_listings import (
    ANNOUNCEMENTS_PATH,
    ANNOUNCEMENT_TYPES,
    OkxListingsListener,
    parse_okx_announcements,
)
from agentoquant.enums import SourceClass
from agentoquant.ledger.store import DB_PATH_ENV, LedgerStore
from tests.test_early_signals import (
    RecordingTransport,
    StopAfterSleeps,
    StubFetcher,
    log_events,
    make_fetcher,
    signal_rows,
)

# ----------------------------------------------------------------------------------------------
# Plain helpers (no fixtures: this file must not emit a decorator)
# ----------------------------------------------------------------------------------------------

ANNOUNCED_AT = datetime(2026, 7, 10, 2, 0, 16, 124000, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)


def scratch_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> LedgerStore:
    """A real DuckDB ledger under ``tmp_path``, plus the env override for implicit stores.

    Autouse would be tidier, but an autouse fixture is a decorator; every test calls this instead, so a
    ``SignalWriter()`` built without an explicit store still resolves to a scratch file.
    """
    monkeypatch.setenv(DB_PATH_ENV, str(tmp_path / "env-ledger.duckdb"))
    return LedgerStore(db_path=tmp_path / "ledger.duckdb")


def writer_for(store: LedgerStore, tmp_path: Any) -> SignalWriter:
    """A writer whose JSON-lines log also lives under ``tmp_path``."""
    return SignalWriter(store, log=JsonlLog(tmp_path / "early_signals.jsonl"))


def announcement(
    *,
    ann_type: str = "announcements-new-listings",
    title: str = "OKX to list SLX/USDT (Solstice) for spot trading",
    url: str = "https://www.okx.com/en-eu/help/okx-to-list-slx-usdt",
    ptime: Any = "1783648816124",
) -> dict[str, Any]:
    """One ``details`` entry in the shape the OKX endpoint returns."""
    return {"annType": ann_type, "title": title, "url": url, "pTime": ptime}


def body(*details: dict[str, Any], code: str = "0", msg: str = "") -> dict[str, Any]:
    """A full ``/api/v5/support/announcements`` response envelope."""
    return {"code": code, "msg": msg, "data": [{"details": list(details)}]}


def make_listener(writer: SignalWriter, fetcher: Any, **kwargs: Any) -> OkxListingsListener:
    """An OKX listener wired to a canned fetcher, with a single announcement type by default."""
    kwargs.setdefault("announcement_types", {"announcements-new-listings": "listing"})
    return OkxListingsListener(writer, fetcher=fetcher, **kwargs)


# ----------------------------------------------------------------------------------------------
# Parser: real-shaped, malformed, empty, wrong-type
# ----------------------------------------------------------------------------------------------


def test_parse_okx_announcements_reads_the_documented_shape() -> None:
    """A real listing and a real delisting body parse to one event each, with the stated times."""
    payload = body(
        announcement(),
        announcement(
            ann_type="announcements-delistings",
            title="OKX to delist XYZ/USDT",
            url="https://www.okx.com/en-eu/help/okx-to-delist-xyz-usdt",
        ),
    )
    events = parse_okx_announcements(payload, observed_at=OBSERVED_AT)
    assert [event.event_type for event in events] == ["listing", "policy"]
    assert [event.source_class for event in events] == [SourceClass.EXCHANGE_ANNOUNCEMENT] * 2
    assert [event.ticker for event in events] == ["SLX", "XYZ"]
    assert events[0].raw_text_or_ref == "https://www.okx.com/en-eu/help/okx-to-list-slx-usdt"
    assert events[0].detected_at == ANNOUNCED_AT
    assert events[0].published_at == ANNOUNCED_AT
    assert events[0].observed_at == OBSERVED_AT
    assert events[0].source == "okx_listings"
    assert events[0].payload().source_class is SourceClass.EXCHANGE_ANNOUNCEMENT


def test_parse_okx_announcements_skips_types_outside_the_event_vocabulary() -> None:
    """Only the two configured announcement types become events; anything else is ignored."""
    payload = body(
        announcement(ann_type="announcements-new-listings"),
        announcement(ann_type="announcements-maintenance", title="OKX maintenance window"),
        announcement(ann_type="", title="OKX to list FOO/USDT"),
    )
    events = parse_okx_announcements(payload, observed_at=OBSERVED_AT)
    assert len(events) == 1
    assert events[0].ticker == "SLX"


def test_parse_okx_announcements_handles_malformed_empty_and_wrong_type_bodies() -> None:
    """Nothing raises: a wrong-type or empty body yields no events, never a half-built one."""
    cases: list[tuple[str, Any]] = [
        ("none", None),
        ("empty string", ""),
        ("string", "not json at all"),
        ("bytes", b"{}"),
        ("list", [announcement()]),
        ("int", 42),
        ("empty dict", {}),
        ("no data key", {"code": "0", "msg": ""}),
        ("data is not a list", {"code": "0", "data": {"details": [announcement()]}}),
        ("empty data", {"code": "0", "data": []}),
        ("group is not a dict", {"code": "0", "data": ["nope"]}),
        ("details is not a list", {"code": "0", "data": [{"details": "nope"}]}),
        ("empty details", {"code": "0", "data": [{"details": []}]}),
        ("detail is not a dict", {"code": "0", "data": [{"details": [None, 7]}]}),
        ("blank title and url", body(announcement(title="  ", url=""))),
        ("non-zero code", body(announcement(), code="51000", msg="bad request")),
    ]
    for label, payload in cases:
        assert parse_okx_announcements(payload, observed_at=OBSERVED_AT) == [], label


def test_parse_okx_announcements_falls_back_to_observed_at_for_an_unusable_ptime() -> None:
    """``pTime`` is epoch milliseconds as a string; anything unusable falls back, never guesses."""
    unusable: list[Any] = [None, "", "   ", "not-a-number", "0", "-5", True, False, [], {}]
    for value in unusable:
        events = parse_okx_announcements(body(announcement(ptime=value)), observed_at=OBSERVED_AT)
        assert len(events) == 1
        assert events[0].detected_at == OBSERVED_AT, value
        assert events[0].published_at is None, value
    usable = parse_okx_announcements(body(announcement(ptime=1783648816124)), observed_at=OBSERVED_AT)
    assert usable[0].detected_at == ANNOUNCED_AT


def test_parse_okx_announcements_uses_the_url_as_the_dedupe_reference() -> None:
    """The URL is stable across polls; a body with only a title still yields a usable reference."""
    titled = parse_okx_announcements(
        body(announcement(url="", title="OKX to list SLX/USDT")), observed_at=OBSERVED_AT
    )
    assert titled[0].raw_text_or_ref == "OKX to list SLX/USDT"
    assert titled[0].dedupe_key == ("exchange_announcement", "OKX to list SLX/USDT")


# ----------------------------------------------------------------------------------------------
# The listener: one call per announcement type
# ----------------------------------------------------------------------------------------------


def test_poll_calls_the_endpoint_once_per_announcement_type(tmp_path: Any, monkeypatch: Any) -> None:
    """One pass costs one call per configured type, with the configured limit."""
    store = scratch_ledger(tmp_path, monkeypatch)
    transport = RecordingTransport().add(
        ANNOUNCEMENTS_PATH, 200, body(announcement())
    )
    listener = OkxListingsListener(
        writer_for(store, tmp_path),
        fetcher=make_fetcher(transport, base_url="https://www.okx.com"),
        announcement_types=dict(ANNOUNCEMENT_TYPES),
        limit=7,
    )
    listener.poll()
    assert transport.paths == [ANNOUNCEMENTS_PATH, ANNOUNCEMENTS_PATH]
    assert [params["annType"] for params in transport.query_params] == list(ANNOUNCEMENT_TYPES)
    assert [params["limit"] for params in transport.query_params] == ["7", "7"]


# ----------------------------------------------------------------------------------------------
# The ledger write path, dedupe and the acceptance criterion
# ----------------------------------------------------------------------------------------------


def test_poll_once_writes_the_ledger_row_with_class_and_producer_role(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(writer, StubFetcher(jsons={ANNOUNCEMENTS_PATH: body(announcement())}))

    written = listener.poll_once()

    assert len(written) == 1
    rows = signal_rows(store)
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "early_signal"
    assert row["source_class"] == SourceClass.EXCHANGE_ANNOUNCEMENT.value
    assert row["ticker"] == "SLX"
    assert row["event_type"] == "listing"
    assert row["producer_role"] == "listener_okx_listings"
    assert row["family"] == "none"
    assert row["harness"] == "none"
    assert row["cycle_id"].endswith("-listen")
    assert row["latency"] is None
    assert listener.stats.polls == 1
    assert listener.stats.events == 1
    assert listener.stats.written == 1
    assert listener.stats.failures == 0
    assert log_events(writer.log, "poll_ok")[0]["written"] == 1


def test_poll_once_deduplicates_a_repeated_poll(tmp_path: Any, monkeypatch: Any) -> None:
    """Pollers re-see the same announcement every pass; the ledger must not gain a second row."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(writer, StubFetcher(jsons={ANNOUNCEMENTS_PATH: body(announcement())}))

    assert len(listener.poll_once()) == 1
    assert listener.poll_once() == []

    assert len(signal_rows(store)) == 1
    assert listener.stats.events == 2
    assert listener.stats.written == 1
    assert listener.stats.duplicates == 1
    assert log_events(writer.log, "poll_ok")[1]["duplicates"] == 1


def test_okx_listing_reaches_the_ledger_within_one_poll_interval(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Acceptance criterion 1: a listing announced seconds ago is in the ledger after one poll.

    The body states the announcement time twice - ``pTime`` (publication) and ``businessPTime`` (the
    scheduled business time, deliberately a day in the future). ``detected_at`` must come from
    ``pTime``: a regression that preferred ``businessPTime`` would put ``detected_at`` in the future and
    the ``0 <= latency <= interval`` assertion below would fail.
    """
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    announced = utcnow() - timedelta(seconds=5)
    payload = body(
        announcement(
            ptime=str(int(announced.timestamp() * 1000)),
            url="https://www.okx.com/en-eu/help/okx-to-list-pengu-usdt",
            title="OKX to list PENGU/USDT (Pudgy Penguins) for spot trading",
        )
    )
    payload["data"][0]["details"][0]["businessPTime"] = str(
        int((announced + timedelta(days=1)).timestamp() * 1000)
    )
    listener = make_listener(
        writer, StubFetcher(jsons={ANNOUNCEMENTS_PATH: payload}), interval_seconds=30.0
    )

    written = listener.poll_once()

    assert len(written) == 1
    row = signal_rows(store)[0]
    assert abs((row["detected_at"] - announced).total_seconds()) < 0.001
    assert row["ticker"] == "PENGU"
    latency = writer.detection_latency_seconds(written[0])
    assert latency is not None
    assert 0 <= latency <= listener.interval_seconds
    assert latency < 60, "the announcement must reach the ledger inside the one-minute budget"


# ----------------------------------------------------------------------------------------------
# Failure accounting: failures, consecutive_failures, backoff, recovery
# ----------------------------------------------------------------------------------------------


def test_a_failing_fetch_is_counted_backed_off_and_recovered(tmp_path: Any, monkeypatch: Any) -> None:
    """A source outage never raises out of the listener; it is counted, backed off and recovered."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(writer, StubFetcher())

    assert listener.poll_once() == []
    first_backoff = listener.stats.backoff_seconds
    assert listener.stats.failures == 1
    assert listener.stats.consecutive_failures == 1
    assert first_backoff > 0
    assert listener.stats.last_error is not None
    assert "FetchError" in listener.stats.last_error
    assert listener.stats.last_error_at is not None
    assert listener.stats.last_success_at is None
    assert signal_rows(store) == []

    assert listener.poll_once() == []
    second_backoff = listener.stats.backoff_seconds
    assert listener.stats.failures == 2
    assert listener.stats.consecutive_failures == 2
    assert second_backoff > first_backoff
    assert second_backoff <= listener.max_backoff_seconds * 1.25

    failures = log_events(writer.log, "poll_failed")
    assert [record["consecutive_failures"] for record in failures] == [1, 2]
    assert failures[0]["listener"] == "okx_listings"

    # Recovery: a listener thread that is restarted (or whose source comes back) clears the counters.
    listener.fetcher = StubFetcher(jsons={ANNOUNCEMENTS_PATH: body(announcement())})
    assert len(listener.poll_once()) == 1
    assert listener.stats.consecutive_failures == 0
    assert listener.stats.backoff_seconds == 0.0
    assert listener.stats.last_error is None
    assert listener.stats.last_success_at is not None


def test_a_non_zero_api_code_is_a_failure_not_an_empty_poll(tmp_path: Any, monkeypatch: Any) -> None:
    """OKX answers HTTP 200 with ``code`` set on error; that is a failure, not 'nothing new'."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(
        writer, StubFetcher(jsons={ANNOUNCEMENTS_PATH: {"code": "51000", "msg": "bad request"}})
    )

    assert listener.poll_once() == []
    assert listener.stats.failures == 1
    assert listener.stats.last_error is not None
    assert "51000" in listener.stats.last_error
    assert signal_rows(store) == []


def test_close_releases_the_fetcher(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    stub = StubFetcher(jsons={ANNOUNCEMENTS_PATH: body(announcement())})
    listener = make_listener(writer_for(store, tmp_path), stub)
    listener.close()
    assert stub.closed is True


# ----------------------------------------------------------------------------------------------
# The run loop
# ----------------------------------------------------------------------------------------------


def test_run_logs_start_and_stop_without_polling_when_already_stopped(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """An already-set stop event starts and stops the loop without a poll."""
    import threading

    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(
        writer,
        StubFetcher(jsons={ANNOUNCEMENTS_PATH: body(announcement())}),
        interval_seconds=30.0,
        log=writer.log,
    )
    stop = threading.Event()
    stop.set()
    listener.run(stop_event=stop)

    assert [record["event"] for record in log_events(writer.log)] == [
        "listener_start",
        "listener_stop",
    ]
    assert listener.stats.polls == 0
    assert signal_rows(store) == []


def test_run_polls_once_then_stops_inside_the_interval(tmp_path: Any, monkeypatch: Any) -> None:
    """One poll, then the sleep window notices the stop event and the loop returns."""
    import threading

    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(
        writer,
        StubFetcher(jsons={ANNOUNCEMENTS_PATH: body(announcement())}),
        interval_seconds=30.0,
        log=writer.log,
    )
    stop = threading.Event()
    sleeper = StopAfterSleeps(stop, after=2)
    listener._sleep = sleeper
    listener.run(stop_event=stop)

    assert sleeper.delays == [0.5, 0.5]  # the 30 s interval, sliced, cut short by the stop event
    assert listener.stats.polls == 1
    assert listener.stats.written == 1
    assert [record["event"] for record in log_events(writer.log)] == [
        "listener_start",
        "poll_ok",
        "listener_stop",
    ]
    assert len(signal_rows(store)) == 1
