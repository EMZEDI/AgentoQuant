"""Per-module tests for ``agentoquant.data.early_signals.kraken_listings`` (Task 4, Phase 0).

Offline by construction: the AssetPairs and RSS fetchers are the canned stubs from
``tests/test_early_signals.py`` (imported, never duplicated), the baseline state file lives under
``tmp_path``, and the ledger is a scratch DuckDB file. No decorators in this file (see the at-sign rule
in ``.hermes.md``), so the scratch ledger and the listener factory are plain helpers.

Acceptance criterion 1 (``tasks/todo.md`` Task 4) - "A new Kraken or Bybit listing appears in the ledger
within one minute of the announcement" - is verified here by
``test_kraken_listing_reaches_the_ledger_within_one_poll_interval``: a pair that was not in the baseline
is written on the same poll that sees it, with the AssetPairs interval inside the plan's 10 to 30 s
window.

The blog RSS feed returns HTTP 403 from this box, so the AssetPairs diff is the path that matters; the
RSS parser and its failure handling are covered anyway because a working feed must not be able to lose
an AssetPairs listing.

Coverage map: the RSS parser (real-shaped, malformed, empty, wrong-type), ``is_listing_item``,
``parse_rss_pubdate``, ``parse_asset_pairs`` and ``pair_ticker``, the state file (round trip, corruption,
atomic write), baseline-on-first-run, diffing, the quote filter, RSS failure isolation, the RSS poll
gate, ``poll_once`` ledger writes, dedupe, failure accounting with backoff and recovery, and close().
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentoquant.data.early_signals import FetchError, JsonlLog, SignalWriter, utcnow
from agentoquant.data.early_signals.kraken_listings import (
    ASSET_PAIRS_PATH,
    BASE_URL,
    DEFAULT_ASSETPAIRS_INTERVAL_SECONDS,
    DEFAULT_QUOTE_FILTER,
    RSS_BASE_URL,
    RSS_PATH,
    KrakenListingsListener,
    KrakenPairsState,
    is_listing_item,
    pair_ticker,
    parse_asset_pairs,
    parse_kraken_rss,
    parse_rss_pubdate,
)
from agentoquant.enums import SourceClass
from agentoquant.ledger.store import DB_PATH_ENV, LedgerStore
from tests.test_early_signals import StubFetcher, log_events, signal_rows

# ----------------------------------------------------------------------------------------------
# Plain helpers (no fixtures: this file must not emit a decorator)
# ----------------------------------------------------------------------------------------------

OBSERVED_AT = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
PUBLISHED_AT = datetime(2026, 9, 16, 18, 48, 58, tzinfo=UTC)

RSS_LISTING = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Kraken Blog</title>
<item><title>TREAD is available for trading!</title>
<link>https://blog.kraken.com/tread-is-available-for-trading</link>
<category>Asset Listings</category>
<pubDate>Wed, 16 Sep 2026 18:48:58 +0000</pubDate></item>
<item><title>Kraken monthly market review</title><link>https://blog.kraken.com/review</link>
<category>Market Update</category>
<pubDate>Tue, 15 Sep 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""

RSS_OTHER = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Kraken Blog</title>
<item><title>Kraken monthly market review</title><link>https://blog.kraken.com/review</link>
<category>Market Update</category>
<pubDate>Tue, 15 Sep 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""

_PAIR_TEMPLATES: dict[str, dict[str, str]] = {
    "TREADUSD": {"altname": "TREADUSD", "wsname": "TREAD/USD", "base": "TREAD", "quote": "ZUSD"},
    "XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT", "quote": "ZUSD"},
    "PENGUUSD": {"altname": "PENGUUSD", "wsname": "PENGU/USD", "base": "PENGU", "quote": "ZUSD"},
    "NEWJPY": {"altname": "NEWJPY", "wsname": "NEW/JPY", "base": "NEW", "quote": "ZJPY"},
}
BASE_CATALOGUE: tuple[str, ...] = ("TREADUSD", "XXBTZUSD")


def scratch_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> LedgerStore:
    """A real DuckDB ledger under ``tmp_path``, plus the env override for implicit stores."""
    monkeypatch.setenv(DB_PATH_ENV, str(tmp_path / "env-ledger.duckdb"))
    return LedgerStore(db_path=tmp_path / "ledger.duckdb")


def writer_for(store: LedgerStore, tmp_path: Any) -> SignalWriter:
    return SignalWriter(store, log=JsonlLog(tmp_path / "early_signals.jsonl"))


def pairs_payload(*extra_keys: str) -> dict[str, Any]:
    """A ``/0/public/AssetPairs`` response: the baseline catalogue plus the named extra pairs."""
    result: dict[str, Any] = {
        key: dict(_PAIR_TEMPLATES[key]) for key in BASE_CATALOGUE
    }
    for key in extra_keys:
        result[key] = dict(_PAIR_TEMPLATES[key])
    result["last"] = 1_700_000_000  # Kraken adds a non-pair key; the parser must skip it
    return {"error": [], "result": result}


def make_listener(writer: SignalWriter, tmp_path: Any, **kwargs: Any) -> KrakenListingsListener:
    """A Kraken listener with canned fetchers, a scratch state file and the writer's log."""
    kwargs.setdefault("state_path", tmp_path / "kraken_pairs.json")
    kwargs.setdefault("pairs_fetcher", StubFetcher(jsons={ASSET_PAIRS_PATH: pairs_payload()}))
    kwargs.setdefault("rss_fetcher", StubFetcher(texts={RSS_PATH: RSS_OTHER}))
    kwargs.setdefault("log", writer.log)
    return KrakenListingsListener(writer, **kwargs)


def state_path(tmp_path: Any) -> Path:
    return Path(tmp_path) / "kraken_pairs.json"


# ----------------------------------------------------------------------------------------------
# The RSS parser: real-shaped, malformed, empty, wrong-type
# ----------------------------------------------------------------------------------------------


def test_parse_rss_pubdate_reads_rfc822_and_rejects_the_rest() -> None:
    assert parse_rss_pubdate("Wed, 16 Sep 2026 18:48:58 +0000") == PUBLISHED_AT
    assert parse_rss_pubdate("Wed, 16 Sep 2026 20:48:58 +0200") == PUBLISHED_AT
    # The parameter is typed str | None (it comes from an XML text node); anything unusable is None.
    for unusable in [None, "", "   ", "not a date", "32 Foo 2026 99:99:99 +0000"]:
        assert parse_rss_pubdate(unusable) is None, unusable


def test_is_listing_item_uses_the_category_and_the_title_markers() -> None:
    assert is_listing_item("TREAD is available for trading!", ["Asset Listings"]) is True
    assert is_listing_item("TREAD is available for trading!", ["asset listings"]) is True
    assert is_listing_item("TREAD is available for trading!", []) is True
    assert is_listing_item("FOO will be available for trading", ["Market Update"]) is True
    assert is_listing_item("BAR is now live for trading", []) is True
    assert is_listing_item("Kraken monthly market review", ["Market Update"]) is False
    assert is_listing_item("", []) is False
    assert is_listing_item("Kraken lists a new asset", []) is False


def test_parse_kraken_rss_reads_listing_items_only() -> None:
    events = parse_kraken_rss(RSS_LISTING, observed_at=OBSERVED_AT)

    assert len(events) == 1
    event = events[0]
    assert event.source_class is SourceClass.EXCHANGE_ANNOUNCEMENT
    assert event.event_type == "listing"
    assert event.ticker == "TREAD"
    assert event.detected_at == PUBLISHED_AT
    assert event.published_at == PUBLISHED_AT
    assert event.observed_at == OBSERVED_AT
    assert event.source == "kraken_listings"
    assert event.raw_text_or_ref == "https://blog.kraken.com/tread-is-available-for-trading"


def test_parse_kraken_rss_normalises_a_legacy_asset_code() -> None:
    """Kraken writes ``XBT``; the rest of the system uses ``BTC``."""
    feed = RSS_LISTING.replace("TREAD is available for trading!", "XBT is available for trading!")
    events = parse_kraken_rss(feed, observed_at=OBSERVED_AT)
    assert [event.ticker for event in events] == ["BTC"]


def test_parse_kraken_rss_handles_malformed_empty_and_wrong_type_bodies() -> None:
    for label, body in [
        ("empty string", ""),
        ("truncated xml", "<rss><channel><item>"),
        ("json body", json.dumps({"error": [], "result": {}})),
        ("html page", "<html><body>403 Forbidden</body></html>"),
        ("plain text", "Forbidden"),
    ]:
        if label in {"html page"}:
            assert parse_kraken_rss(body, observed_at=OBSERVED_AT) == []
        else:
            with pytest.raises(FetchError):
                parse_kraken_rss(body, observed_at=OBSERVED_AT)

    for label, body in [
        ("no channel", "<rss version=\"2.0\"></rss>"),
        ("empty channel", "<rss version=\"2.0\"><channel></channel></rss>"),
        ("no items", RSS_OTHER.replace("Kraken monthly market review", "Nothing to see")),
    ]:
        assert parse_kraken_rss(body, observed_at=OBSERVED_AT) == [], label


def test_parse_kraken_rss_skips_a_listing_item_with_no_title_or_link() -> None:
    feed = """<rss version="2.0"><channel>
    <item><category>Asset Listings</category></item>
    <item><title>TREAD is available for trading!</title><category>Asset Listings</category></item>
    </channel></rss>"""
    events = parse_kraken_rss(feed, observed_at=OBSERVED_AT)
    assert [event.raw_text_or_ref for event in events] == ["TREAD is available for trading!"]


# ----------------------------------------------------------------------------------------------
# AssetPairs parsing
# ----------------------------------------------------------------------------------------------


def test_parse_asset_pairs_reads_the_documented_shape() -> None:
    pairs = parse_asset_pairs(pairs_payload("PENGUUSD"))

    assert sorted(pairs) == ["PENGUUSD", "TREADUSD", "XXBTZUSD"]  # the non-pair "last" key is skipped
    assert pairs["PENGUUSD"] == {
        "base": "PENGU",
        "quote": "ZUSD",
        "altname": "PENGUUSD",
        "wsname": "PENGU/USD",
    }


def test_parse_asset_pairs_handles_wrong_type_bodies() -> None:
    for payload in [None, [], "nope", 42, {}, {"error": []}, {"result": None}, {"result": []},
                    {"result": {"TREADUSD": "nope"}}]:
        assert parse_asset_pairs(payload) == {}, payload


def test_parse_asset_pairs_tolerates_missing_fields() -> None:
    pairs = parse_asset_pairs({"result": {"X": {"base": None, "quote": "ZUSD"}}})
    assert pairs == {"X": {"base": "", "quote": "ZUSD", "altname": "", "wsname": ""}}
    assert pair_ticker(pairs["X"]) is None


def test_pair_ticker_prefers_the_wsname_and_normalises_aliases() -> None:
    """Both spellings of the legacy BTC/DOGE codes normalise; a regression here writes a Kraken code."""
    assert pair_ticker({"wsname": "TREAD/USD", "base": "TREAD", "quote": "ZUSD"}) == "TREAD"
    assert pair_ticker({"wsname": "XBT/USD", "base": "XXBT", "quote": "ZUSD"}) == "BTC"
    assert pair_ticker({"wsname": "", "base": "XXBT", "quote": "ZUSD"}) == "BTC"
    assert pair_ticker({"wsname": "", "base": "XBT", "quote": "ZUSD"}) == "BTC"
    assert pair_ticker({"wsname": "XDG/USD", "base": "XXDG", "quote": "ZUSD"}) == "DOGE"
    assert pair_ticker({"wsname": "", "base": "XXDG", "quote": "ZUSD"}) == "DOGE"
    assert pair_ticker({"wsname": "", "base": "", "quote": "ZUSD"}) is None


# ----------------------------------------------------------------------------------------------
# The baseline state file
# ----------------------------------------------------------------------------------------------


def test_pairs_state_round_trips_and_is_written_atomically(tmp_path: Any) -> None:
    state = KrakenPairsState(state_path(tmp_path))
    assert state.load() is None  # no file yet: the caller records a baseline instead of guessing

    state.save({"XXBTZUSD", "TREADUSD"}, updated_at=OBSERVED_AT)
    assert state.load() == {"XXBTZUSD", "TREADUSD"}
    stored = json.loads(state_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["pairs"] == ["TREADUSD", "XXBTZUSD"]  # sorted, so the file diffs cleanly in git
    assert stored["updated_at"] == OBSERVED_AT.isoformat()
    assert list(state_path(tmp_path).parent.glob("*.tmp")) == []


def test_pairs_state_returns_none_for_an_unusable_file(tmp_path: Any) -> None:
    path = state_path(tmp_path)
    for body in ["not json", "[]", '{"pairs": "nope"}', '{"pairs": 42}', ""]:
        path.write_text(body, encoding="utf-8")
        assert KrakenPairsState(path).load() is None, body


# ----------------------------------------------------------------------------------------------
# Baseline on first run, then diffing
# ----------------------------------------------------------------------------------------------


def test_first_run_records_the_catalogue_as_the_baseline_and_emits_nothing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The first ever poll must not announce 1,450 pairs as new listings."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(writer, tmp_path)

    assert listener.poll_once() == []
    assert signal_rows(store) == []
    assert KrakenPairsState(state_path(tmp_path)).load() == set(BASE_CATALOGUE)
    baseline_records = log_events(writer.log, "kraken_baseline")
    assert len(baseline_records) == 1
    assert baseline_records[0]["pairs"] == len(BASE_CATALOGUE)

    # The next poll sees the same catalogue and still emits nothing.
    assert listener.poll_once() == []
    assert signal_rows(store) == []
    assert len(log_events(writer.log, "kraken_baseline")) == 1


def test_a_pair_that_was_not_in_the_baseline_becomes_a_listing(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    KrakenPairsState(state_path(tmp_path)).save(set(BASE_CATALOGUE))
    listener = make_listener(
        writer, tmp_path, pairs_fetcher=StubFetcher(jsons={ASSET_PAIRS_PATH: pairs_payload("PENGUUSD")})
    )

    written = listener.poll_once()

    assert len(written) == 1
    row = signal_rows(store)[0]
    assert row["ticker"] == "PENGU"
    assert row["event_type"] == "listing"
    assert row["source_class"] == SourceClass.EXCHANGE_ANNOUNCEMENT.value
    assert row["raw_text_or_ref"] == "kraken:assetpairs:PENGUUSD:PENGU/USD"
    assert row["producer_role"] == "listener_kraken_listings"
    assert log_events(writer.log, "kraken_baseline") == []  # a file baseline, not a first run
    assert KrakenPairsState(state_path(tmp_path)).load() == {"TREADUSD", "XXBTZUSD", "PENGUUSD"}

    # Re-polling the same catalogue does not re-announce the pair: it is in the baseline now.
    assert listener.poll_once() == []
    assert len(signal_rows(store)) == 1


def test_a_new_pair_outside_the_quote_filter_is_recorded_but_not_announced(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A JPY-quoted pair is not part of the USD universe, but it must not be re-emitted later."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    KrakenPairsState(state_path(tmp_path)).save(set(BASE_CATALOGUE))
    listener = make_listener(
        writer, tmp_path, pairs_fetcher=StubFetcher(jsons={ASSET_PAIRS_PATH: pairs_payload("NEWJPY")})
    )

    assert listener.poll_once() == []
    assert signal_rows(store) == []
    assert "NEWJPY" in KrakenPairsState(state_path(tmp_path)).load()
    assert "ZJPY" not in DEFAULT_QUOTE_FILTER

    # With no filter, the same new pair is announced.
    other_state = tmp_path / "other_pairs.json"
    KrakenPairsState(other_state).save(set(BASE_CATALOGUE))
    unfiltered = make_listener(
        writer,
        tmp_path,
        quote_filter=None,
        state_path=other_state,
        pairs_fetcher=StubFetcher(jsons={ASSET_PAIRS_PATH: pairs_payload("NEWJPY")}),
    )
    assert len(unfiltered.poll_once()) == 1


def test_kraken_listing_reaches_the_ledger_within_one_poll_interval(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Acceptance criterion 1 for Kraken: a new AssetPairs entry is in the ledger after one poll.

    AssetPairs carries no publication time, so ``detected_at`` is the moment the poll saw it and the
    row's ``ts - detected_at`` is the write latency. The poll interval must also stay inside the plan's
    10 to 30 second window, or the one-minute budget cannot be met.
    """
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    KrakenPairsState(state_path(tmp_path)).save(set(BASE_CATALOGUE))
    listener = make_listener(
        writer, tmp_path, pairs_fetcher=StubFetcher(jsons={ASSET_PAIRS_PATH: pairs_payload("PENGUUSD")})
    )

    assert 10.0 <= listener.interval_seconds <= 30.0
    assert listener.interval_seconds == DEFAULT_ASSETPAIRS_INTERVAL_SECONDS

    written = listener.poll_once()

    assert len(written) == 1
    latency = writer.detection_latency_seconds(written[0])
    assert latency is not None
    assert 0 <= latency <= listener.interval_seconds
    assert latency < 60, "the new pair must reach the ledger inside the one-minute budget"
    assert signal_rows(store)[0]["detected_at"] <= utcnow()


# ----------------------------------------------------------------------------------------------
# RSS: failure isolation and the poll gate
# ----------------------------------------------------------------------------------------------


def test_an_rss_outage_does_not_lose_an_assetpairs_listing(tmp_path: Any, monkeypatch: Any) -> None:
    """The blog feed is 2 calls/minute and can 403; a new pair must survive it."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    KrakenPairsState(state_path(tmp_path)).save(set(BASE_CATALOGUE))
    listener = make_listener(
        writer,
        tmp_path,
        pairs_fetcher=StubFetcher(jsons={ASSET_PAIRS_PATH: pairs_payload("PENGUUSD")}),
        rss_fetcher=StubFetcher(),  # every RSS call raises FetchError, as the real 403 does
    )

    written = listener.poll_once()

    assert len(written) == 1
    assert signal_rows(store)[0]["ticker"] == "PENGU"
    assert listener.stats.failures == 0  # isolated: the listener itself is healthy
    assert listener.stats.consecutive_failures == 0
    failed = log_events(writer.log, "rss_failed")
    assert len(failed) == 1
    assert "FetchError" in failed[0]["error"]


def test_the_rss_feed_is_polled_on_its_own_slower_interval(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    rss = StubFetcher(texts={RSS_PATH: RSS_LISTING})
    listener = make_listener(writer, tmp_path, rss_fetcher=rss, rss_interval_seconds=3600.0)

    listener.poll_once()
    assert [call[0] for call in rss.calls] == ["text"]
    listener.poll_once()
    assert len(rss.calls) == 1, "the RSS feed is quota-limited and must not be fetched every poll"

    eager = StubFetcher(texts={RSS_PATH: RSS_LISTING})
    listener2 = make_listener(writer, tmp_path, rss_fetcher=eager, rss_interval_seconds=0.0)
    listener2.poll_once()
    listener2.poll_once()
    assert len(eager.calls) == 2


def test_poll_once_deduplicates_a_repeated_poll(tmp_path: Any, monkeypatch: Any) -> None:
    """The RSS feed repeats its items; only the first sighting may reach the ledger."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(
        writer, tmp_path, rss_fetcher=StubFetcher(texts={RSS_PATH: RSS_LISTING}), rss_interval_seconds=0.0
    )

    assert len(listener.poll_once()) == 1
    assert listener.poll_once() == []

    assert len(signal_rows(store)) == 1
    assert listener.stats.events == 2
    assert listener.stats.written == 1
    assert listener.stats.duplicates == 1
    assert log_events(writer.log, "poll_ok")[1]["duplicates"] == 1


# ----------------------------------------------------------------------------------------------
# Failure accounting: failures, consecutive_failures, backoff, recovery
# ----------------------------------------------------------------------------------------------


def test_a_failing_assetpairs_fetch_is_counted_backed_off_and_recovered(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(writer, tmp_path, pairs_fetcher=StubFetcher())

    assert listener.poll_once() == []
    first_backoff = listener.stats.backoff_seconds
    assert listener.stats.failures == 1
    assert listener.stats.consecutive_failures == 1
    assert first_backoff > 0
    assert listener.stats.last_error is not None and "FetchError" in listener.stats.last_error
    assert listener.stats.last_success_at is None
    assert signal_rows(store) == []

    assert listener.poll_once() == []
    assert listener.stats.failures == 2
    assert listener.stats.consecutive_failures == 2
    assert listener.stats.backoff_seconds > first_backoff
    assert listener.stats.backoff_seconds <= listener.max_backoff_seconds * 1.25
    assert [record["consecutive_failures"] for record in log_events(writer.log, "poll_failed")] == [1, 2]

    listener.pairs_fetcher = StubFetcher(jsons={ASSET_PAIRS_PATH: pairs_payload()})
    assert listener.poll_once() == []
    assert listener.stats.consecutive_failures == 0
    assert listener.stats.backoff_seconds == 0.0
    assert listener.stats.last_error is None
    assert listener.stats.last_success_at is not None
    assert KrakenPairsState(state_path(tmp_path)).load() == set(BASE_CATALOGUE)


def test_an_empty_assetpairs_catalogue_is_a_failure_not_a_full_delisting(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """An empty response must not be read as 'every pair vanished'; it is a failed poll."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    KrakenPairsState(state_path(tmp_path)).save(set(BASE_CATALOGUE))
    listener = make_listener(
        writer,
        tmp_path,
        pairs_fetcher=StubFetcher(jsons={ASSET_PAIRS_PATH: {"error": [], "result": {}}}),
    )

    assert listener.poll_once() == []
    assert listener.stats.failures == 1
    assert listener.stats.last_error is not None
    assert "no pairs" in listener.stats.last_error
    assert KrakenPairsState(state_path(tmp_path)).load() == set(BASE_CATALOGUE)


def test_close_releases_both_fetchers(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    pairs = StubFetcher(jsons={ASSET_PAIRS_PATH: pairs_payload()})
    rss = StubFetcher(texts={RSS_PATH: RSS_LISTING})
    listener = make_listener(
        writer_for(store, tmp_path), tmp_path, pairs_fetcher=pairs, rss_fetcher=rss
    )
    listener.close()
    assert pairs.closed is True and rss.closed is True


def test_the_default_fetchers_are_pointed_at_the_public_endpoints(tmp_path: Any, monkeypatch: Any) -> None:
    """No Kraken private endpoint and no write endpoint is ever reachable from this listener."""
    store = scratch_ledger(tmp_path, monkeypatch)
    listener = KrakenListingsListener(writer_for(store, tmp_path), state_path=state_path(tmp_path))
    assert listener.pairs_fetcher.base_url == BASE_URL
    assert listener.rss_fetcher.base_url == RSS_BASE_URL
    assert ASSET_PAIRS_PATH == "/0/public/AssetPairs"
    assert RSS_PATH == "/feed/"
    listener.close()
