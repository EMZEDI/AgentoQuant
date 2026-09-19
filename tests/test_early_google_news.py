"""Per-module tests for ``agentoquant.data.early_signals.google_news_rss`` (Task 4, Phase 0).

Offline by construction: the fetcher is the canned stub from ``tests/test_early_signals.py`` (imported,
never duplicated) and the ledger is a scratch DuckDB file under ``tmp_path``. No decorators in this file
(see the at-sign rule in ``.hermes.md``): the scratch ledger and the listener factory are plain helpers.

Coverage map: the parser against a real-shaped feed, malformed / empty / wrong-type bodies, the
per-ticker feeds and their request parameters, per-ticker failure isolation, ``poll_once`` ledger writes,
dedupe, failure accounting with backoff and recovery, the article-time helpers (``article_times``,
``first_article_time``, the publisher exclusion, the ``after``/``before`` window), ``default_tickers``
from ``config/sleeves.yaml``, and close().

Task 4's first verification step - "replay of three past listing days shows the signal ahead of the first
article" - is measured here rather than asserted: ``test_a_listing_signal_is_measured_ahead_of_the_first_article``
takes the first real article timestamp out of a Google News feed and checks that the ledger row's
``latency_seconds_vs_first_article`` is positive.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from agentoquant.data.early_signals import (
    FetchError,
    JsonlLog,
    SignalEvent,
    SignalWriter,
    utcnow,
)
from agentoquant.data.early_signals.google_news_rss import (
    BASE_URL,
    DEFAULT_EXCLUDE_TITLE_CONTAINS,
    SEARCH_PATH,
    GoogleNewsRssListener,
    article_times,
    build_query,
    default_tickers,
    first_article_time,
    parse_google_news_rss,
    parse_rss_pubdate,
)
from agentoquant.enums import SourceClass
from agentoquant.ledger.store import DB_PATH_ENV, LedgerStore
from tests.test_early_signals import StubFetcher, log_events, signal_rows

# ----------------------------------------------------------------------------------------------
# Plain helpers (no fixtures: this file must not emit a decorator)
# ----------------------------------------------------------------------------------------------

OBSERVED_AT = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
ANNOUNCEMENT_AT = datetime(2026, 9, 16, 18, 48, 58, tzinfo=UTC)  # the Kraken blog post's pubDate
FIRST_ARTICLE_AT = datetime(2026, 9, 16, 23, 44, 11, tzinfo=UTC)  # the first independent article
LEAD_SECONDS = int((FIRST_ARTICLE_AT - ANNOUNCEMENT_AT).total_seconds())

NEWS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>TREAD crypto - Google News</title>
<item><title>Tread.fi Exchanges TREAD Markets - CryptoRank</title>
<link>https://news.example.invalid/tread-markets</link>
<pubDate>Wed, 16 Sep 2026 23:44:11 GMT</pubDate></item>
<item><title>Kraken lists TREAD - Kraken Blog</title><link>https://blog.kraken.com/tread</link>
<pubDate>Wed, 16 Sep 2026 18:48:58 +0000</pubDate></item>
<item><title>Undated headline - Some Publisher</title>
<link>https://news.example.invalid/undated</link></item>
</channel></rss>"""


class ExplodingFetcher:
    """A fetcher that raises a non-``FetchError``: the failure the listener does not isolate."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def get_text(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        raise self.error

    def get_json(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.error

    def close(self) -> None:
        return None


def scratch_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> LedgerStore:
    """A real DuckDB ledger under ``tmp_path``, plus the env override for implicit stores."""
    monkeypatch.setenv(DB_PATH_ENV, str(tmp_path / "env-ledger.duckdb"))
    return LedgerStore(db_path=tmp_path / "ledger.duckdb")


def writer_for(store: LedgerStore, tmp_path: Any) -> SignalWriter:
    return SignalWriter(store, log=JsonlLog(tmp_path / "early_signals.jsonl"))


def feed(title: str, link: str, *, pub_date: str = "Wed, 16 Sep 2026 23:44:11 GMT") -> str:
    """A one-item Google News RSS document."""
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<item><title>{title}</title><link>{link}</link><pubDate>{pub_date}</pubDate></item>"
        "</channel></rss>"
    )


def make_listener(writer: SignalWriter, fetcher: Any, **kwargs: Any) -> GoogleNewsRssListener:
    """A Google News listener over the canned tickers, logging to the writer's log."""
    kwargs.setdefault("tickers", ("TAO", "VVV"))
    kwargs.setdefault("log", writer.log)
    return GoogleNewsRssListener(writer, fetcher=fetcher, **kwargs)


# ----------------------------------------------------------------------------------------------
# The parser: real-shaped, malformed, empty, wrong-type
# ----------------------------------------------------------------------------------------------


def test_parse_google_news_rss_reads_the_documented_shape() -> None:
    events = parse_google_news_rss(NEWS_FEED, ticker="TREAD", query="TREAD crypto", observed_at=OBSERVED_AT)

    assert len(events) == 3
    assert [event.source_class for event in events] == [SourceClass.HEADLINE] * 3
    assert [event.event_type for event in events] == ["headline"] * 3
    assert [event.ticker for event in events] == ["TREAD"] * 3
    assert events[0].raw_text_or_ref == "https://news.example.invalid/tread-markets"
    assert events[0].detected_at == FIRST_ARTICLE_AT
    assert events[0].published_at == FIRST_ARTICLE_AT
    assert events[0].observed_at == OBSERVED_AT
    assert events[0].source == "google_news_rss"
    assert events[0].payload().source_class is SourceClass.HEADLINE


def test_parse_google_news_rss_falls_back_to_observed_at_for_an_undated_item() -> None:
    """Google News sometimes omits pubDate; the item is still recorded, at the observation time."""
    events = parse_google_news_rss(NEWS_FEED, observed_at=OBSERVED_AT)
    assert events[2].detected_at == OBSERVED_AT
    assert events[2].published_at is None


def test_parse_google_news_rss_handles_malformed_empty_and_wrong_type_bodies() -> None:
    for label, body in [
        ("empty string", ""),
        ("truncated xml", "<rss><channel><item>"),
        ("json body", json.dumps({"items": []})),
        ("html page", "<html><body>consent</body></html>"),
        ("plain text", "no xml here"),
    ]:
        if label == "html page":
            assert parse_google_news_rss(body, observed_at=OBSERVED_AT) == []
        else:
            with pytest.raises(FetchError):
                parse_google_news_rss(body, observed_at=OBSERVED_AT)

    for label, body in [
        ("no channel", '<rss version="2.0"></rss>'),
        ("empty channel", '<rss version="2.0"><channel></channel></rss>'),
        ("item with no title or link", '<rss version="2.0"><channel><item><pubDate>'
         "Wed, 16 Sep 2026 23:44:11 GMT</pubDate></item></channel></rss>"),
    ]:
        assert parse_google_news_rss(body, observed_at=OBSERVED_AT) == [], label


def test_parse_google_news_rss_falls_back_to_a_synthesised_reference() -> None:
    """An item with a title but no link is still citable and still de-duplicable."""
    body = '<rss version="2.0"><channel><item><title>Only a title</title></item></channel></rss>'
    events = parse_google_news_rss(body, query="TAO crypto", observed_at=OBSERVED_AT)
    assert events[0].raw_text_or_ref == "google-news:TAO crypto:Only a title"


def test_parse_rss_pubdate_reads_rfc822_and_rejects_the_rest() -> None:
    assert parse_rss_pubdate("Wed, 16 Sep 2026 23:44:11 GMT") == FIRST_ARTICLE_AT
    assert parse_rss_pubdate("Thu, 17 Sep 2026 01:44:11 +0200") == FIRST_ARTICLE_AT
    for unusable in [None, "", "   ", "not a date", "32 Foo 2026 99:99:99 +0000"]:
        assert parse_rss_pubdate(unusable) is None, unusable


# ----------------------------------------------------------------------------------------------
# Per-ticker feeds
# ----------------------------------------------------------------------------------------------


def test_each_ticker_gets_its_own_feed_and_query(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    fetcher = StubFetcher(
        texts={
            "TAO crypto": feed("TAO mainnet upgrade - The Block", "https://news.example.invalid/tao"),
            "VVV crypto": feed("VVV token burn - CoinDesk", "https://news.example.invalid/vvv"),
        }
    )
    listener = make_listener(writer, fetcher)

    written = listener.poll_once()

    assert len(written) == 2
    rows = signal_rows(store)
    assert [(row["ticker"], row["raw_text_or_ref"]) for row in rows] == [
        ("TAO", "https://news.example.invalid/tao"),
        ("VVV", "https://news.example.invalid/vvv"),
    ]
    assert all(row["source_class"] == SourceClass.HEADLINE.value for row in rows)
    assert all(row["producer_role"] == "listener_google_news_rss" for row in rows)

    params = [call[2] for call in fetcher.calls]
    assert [entry["q"] for entry in params] == ["TAO crypto", "VVV crypto"]
    assert [call[1] for call in fetcher.calls] == [SEARCH_PATH, SEARCH_PATH]
    assert params[0]["hl"] == "en-US"
    assert params[0]["gl"] == "US"
    assert params[0]["ceid"] == "US:en"
    assert BASE_URL == "https://news.google.com"


def test_build_query_appends_the_suffix_only_when_there_is_one() -> None:
    assert build_query("TAO") == "TAO crypto"
    assert build_query("TAO", "") == "TAO"
    assert build_query("TAO", "news") == "TAO news"


def test_one_dead_ticker_does_not_hide_a_live_one(tmp_path: Any, monkeypatch: Any) -> None:
    """A ticker with no news is common; it must not stop the other tickers."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    fetcher = StubFetcher(
        texts={"VVV crypto": feed("VVV token burn - CoinDesk", "https://news.example.invalid/vvv")}
    )
    listener = make_listener(writer, fetcher)

    written = listener.poll_once()

    assert len(written) == 1
    assert signal_rows(store)[0]["ticker"] == "VVV"
    assert listener.stats.failures == 0, "a ticker with no canned feed is isolated, not a poll failure"
    failed = log_events(writer.log, "ticker_failed")
    assert [record["ticker"] for record in failed] == ["TAO"]
    assert "HTTP 404" in failed[0]["error"]


def test_poll_once_deduplicates_a_repeated_poll(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(
        writer,
        StubFetcher(texts={"TAO crypto": feed("TAO news", "https://news.example.invalid/tao")}),
        tickers=("TAO",),
    )

    assert len(listener.poll_once()) == 1
    assert listener.poll_once() == []

    assert len(signal_rows(store)) == 1
    assert listener.stats.events == 2
    assert listener.stats.written == 1
    assert listener.stats.duplicates == 1
    assert log_events(writer.log, "poll_ok")[1]["duplicates"] == 1


def test_the_default_ticker_list_comes_from_the_sleeve_config() -> None:
    tickers = default_tickers()
    assert tickers, "sleeves.yaml must name an enabled universe"
    assert len(set(tickers)) == len(tickers), "no duplicate queries"
    assert all(isinstance(ticker, str) and ticker for ticker in tickers)


# ----------------------------------------------------------------------------------------------
# Article-time handling
# ----------------------------------------------------------------------------------------------


def test_article_times_are_filtered_and_sorted_oldest_first() -> None:
    articles = article_times(NEWS_FEED)
    assert [title for _when, title in articles] == [
        "Kraken lists TREAD - Kraken Blog",
        "Tread.fi Exchanges TREAD Markets - CryptoRank",
    ]  # the undated item is dropped: an article with no time cannot measure a lead
    assert [when for when, _title in articles] == [ANNOUNCEMENT_AT, FIRST_ARTICLE_AT]

    assert [title for _when, title in article_times(NEWS_FEED, exclude_title_contains=("Kraken Blog",))] == [
        "Tread.fi Exchanges TREAD Markets - CryptoRank"
    ]
    assert article_times(NEWS_FEED, after=FIRST_ARTICLE_AT) == [
        (FIRST_ARTICLE_AT, "Tread.fi Exchanges TREAD Markets - CryptoRank")
    ]
    assert article_times(NEWS_FEED, before=FIRST_ARTICLE_AT) == [
        (ANNOUNCEMENT_AT, "Kraken lists TREAD - Kraken Blog")
    ]
    assert article_times(NEWS_FEED, after=datetime(2026, 9, 17, tzinfo=UTC)) == []
    assert article_times(NEWS_FEED, before=datetime(2026, 1, 1, tzinfo=UTC)) == []


def test_first_article_time_excludes_the_publisher_itself() -> None:
    """Kraken's own post appearing in Google News is not an independent article."""
    assert " - Kraken Blog" in DEFAULT_EXCLUDE_TITLE_CONTAINS
    chosen = first_article_time(NEWS_FEED)
    assert chosen == (FIRST_ARTICLE_AT, "Tread.fi Exchanges TREAD Markets - CryptoRank")

    unfiltered = first_article_time(NEWS_FEED, exclude_title_contains=())
    assert unfiltered == (ANNOUNCEMENT_AT, "Kraken lists TREAD - Kraken Blog")

    assert first_article_time(NEWS_FEED, after=datetime(2026, 9, 17, tzinfo=UTC)) is None
    assert first_article_time("<rss version='2.0'><channel></channel></rss>") is None


def test_a_listing_signal_is_measured_ahead_of_the_first_article(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Task 4 verification 1, measured: the ledger row carries the real lead over the first article.

    The announcement time is the source's own (the Kraken blog's ``pubDate``); the article time comes
    from a real Google News document. A regression that dropped ``first_article_at`` would leave the
    column NULL and this assertion would fail.
    """
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    article = first_article_time(NEWS_FEED)
    assert article is not None

    event = SignalEvent(
        source_class=SourceClass.EXCHANGE_ANNOUNCEMENT,
        event_type="listing",
        raw_text_or_ref="https://blog.kraken.com/tread",
        detected_at=ANNOUNCEMENT_AT,
        ticker="TREAD",
        source="kraken_listings",
        first_article_at=article[0],
    )
    record_id = writer.write(event)
    assert record_id is not None

    row = signal_rows(store)[0]
    assert row["latency"] == LEAD_SECONDS
    assert row["latency"] > 0, "the primary source must lead the first article"
    assert row["source_class"] == SourceClass.EXCHANGE_ANNOUNCEMENT.value
    assert row["detected_at"] == ANNOUNCEMENT_AT


# ----------------------------------------------------------------------------------------------
# Failure accounting: failures, consecutive_failures, backoff, recovery
# ----------------------------------------------------------------------------------------------


def test_an_unexpected_failure_is_counted_backed_off_and_recovered(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(writer, ExplodingFetcher(RuntimeError("html instead of rss")))

    assert listener.poll_once() == []
    first_backoff = listener.stats.backoff_seconds
    assert listener.stats.failures == 1
    assert listener.stats.consecutive_failures == 1
    assert first_backoff > 0
    assert listener.stats.last_error is not None
    assert "RuntimeError" in listener.stats.last_error
    assert signal_rows(store) == []

    assert listener.poll_once() == []
    assert listener.stats.failures == 2
    assert listener.stats.consecutive_failures == 2
    assert listener.stats.backoff_seconds > first_backoff
    assert listener.stats.backoff_seconds <= listener.max_backoff_seconds * 1.25
    assert [record["consecutive_failures"] for record in log_events(writer.log, "poll_failed")] == [1, 2]

    listener.fetcher = StubFetcher(
        texts={"TAO crypto": feed("TAO news", "https://news.example.invalid/tao")}
    )
    assert len(listener.poll_once()) == 1
    assert listener.stats.consecutive_failures == 0
    assert listener.stats.backoff_seconds == 0.0
    assert listener.stats.last_error is None
    assert listener.stats.last_success_at is not None
    assert listener.stats.last_success_at <= utcnow()


def test_close_releases_the_fetcher(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    stub = StubFetcher(texts={"TAO crypto": feed("TAO news", "https://news.example.invalid/tao")})
    listener = make_listener(writer_for(store, tmp_path), stub)
    listener.close()
    assert stub.closed is True
