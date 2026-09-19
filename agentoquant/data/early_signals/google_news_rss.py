"""Google News RSS per ticker: the confirmation/headline layer of the early-signal set.

``config/sources.yaml`` -> ``google_news_rss``: ``GET https://news.google.com/rss/search?q=<query>``,
keyless, 6 calls/minute. Items are ``SourceClass.HEADLINE``: a headline is *not* evidence on its own
(the Verifier needs two independent sources or a price and volume reaction), which is exactly why the
class is carried on every record.

Two jobs:

1. :class:`GoogleNewsRssListener` writes per-ticker headline events.
2. :func:`first_article_time` answers "when did the first article about this appear?" for the replay
   harness, so the lead time of an announcement can be measured against real article timestamps rather
   than asserted. ``exclude_title_contains`` drops a publisher's own blog (Kraken's own post appearing
   in Google News is not an independent article).

The default ticker list comes from ``config/sleeves.yaml`` (sleeves A and B, the enabled universe), so
the listener follows the configured universe instead of a list hard-coded here.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

from agentoquant.data.early_signals import (
    FetchError,
    HttpFetcher,
    JsonlLog,
    Listener,
    RateLimiter,
    SignalEvent,
    SignalWriter,
    as_utc,
    utcnow,
)
from agentoquant.data.quota_manager import QuotaManager
from agentoquant.enums import SourceClass

BASE_URL = "https://news.google.com"
SEARCH_PATH = "/rss/search"

#: Quota from config/sources.yaml: 6 calls/minute.
CALLS_PER_MINUTE = 6

#: The ``config/sources.yaml`` key every call from this listener is charged to (the shared budget).
SOURCE = "google_news_rss"
DEFAULT_INTERVAL_SECONDS = 300.0
#: Query suffix per ticker. "SOL crypto" beats "SOL" for signal-to-noise; override per ticker if needed.
DEFAULT_QUERY_SUFFIX = "crypto"

#: Publishers whose own post about its own event is not an independent article.
DEFAULT_EXCLUDE_TITLE_CONTAINS: tuple[str, ...] = (
    " - Kraken Blog",
    " - Bybit",
    " - OKX",
    " - Binance",
)


def default_tickers() -> tuple[str, ...]:
    """Sleeve A and sleeve B coins from ``config/sleeves.yaml`` (the enabled universe)."""
    from agentoquant.config_loader import load_sleeves

    sleeves = load_sleeves()
    tickers: list[str] = []
    for _sleeve, spec in sleeves.sleeves.items():
        if not spec.enabled:
            continue
        for coin in spec.coins:
            if coin not in tickers:
                tickers.append(coin)
    return tuple(tickers)


def build_query(ticker: str, suffix: str = DEFAULT_QUERY_SUFFIX) -> str:
    """The Google News query for one ticker."""
    return f"{ticker} {suffix}".strip()


def parse_rss_pubdate(value: str | None) -> datetime | None:
    """RFC-822 ``pubDate`` to aware UTC."""
    if not value:
        return None
    try:
        return as_utc(parsedate_to_datetime(value.strip()))
    except (TypeError, ValueError):
        return None


def _split_title(title: str) -> tuple[str, str]:
    """Google News titles look like ``Headline text - Publisher``; split them."""
    if " - " in title:
        headline, _, publisher = title.rpartition(" - ")
        return headline.strip(), publisher.strip()
    return title.strip(), ""


def parse_google_news_rss(
    xml_text: str,
    *,
    ticker: str | None = None,
    query: str = "",
    observed_at: datetime | None = None,
    source: str = "google_news_rss",
) -> list[SignalEvent]:
    """Parse a Google News RSS document into headline events.

    ``raw_text_or_ref`` is the article link (stable, and what the Verifier cites); ``detected_at`` is
    the article's own ``pubDate``.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise FetchError(f"google news RSS is not valid XML: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        return []
    observed_at = observed_at or utcnow()

    events: list[SignalEvent] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not link and not title:
            continue
        published = parse_rss_pubdate(item.findtext("pubDate"))
        headline, publisher = _split_title(title)
        events.append(
            SignalEvent(
                source_class=SourceClass.HEADLINE,
                event_type="headline",
                raw_text_or_ref=link or f"google-news:{query}:{headline}",
                detected_at=published or observed_at,
                ticker=ticker,
                source=source,
                observed_at=observed_at,
                published_at=published,
            )
        )
    return events


def article_times(
    xml_text: str,
    *,
    exclude_title_contains: tuple[str, ...] = (),
    after: datetime | None = None,
    before: datetime | None = None,
) -> list[tuple[datetime, str]]:
    """Every ``(pubDate, title)`` in a Google News document, oldest first, after the filters.

    ``after``/``before`` matter for honesty: a ticker query returns unrelated articles from years back,
    and the lead time of an announcement is only meaningful against an article about that same event,
    which in practice means an article published around the announcement.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise FetchError(f"google news RSS is not valid XML: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        return []
    found: list[tuple[datetime, str]] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        if any(marker.lower() in title.lower() for marker in exclude_title_contains):
            continue
        published = parse_rss_pubdate(item.findtext("pubDate"))
        if published is None:
            continue
        if after is not None and published < as_utc(after):
            continue
        if before is not None and published > as_utc(before):
            continue
        found.append((published, title))
    found.sort(key=lambda pair: pair[0])
    return found


def first_article_time(
    xml_text: str,
    *,
    exclude_title_contains: tuple[str, ...] = DEFAULT_EXCLUDE_TITLE_CONTAINS,
    after: datetime | None = None,
    before: datetime | None = None,
) -> tuple[datetime, str] | None:
    """The earliest qualifying article time and title.

    A publisher's own post is excluded by default (Kraken's blog appearing in Google News is not an
    independent article), and ``after``/``before`` bound the window to articles about this event.
    """
    articles = article_times(
        xml_text,
        exclude_title_contains=exclude_title_contains,
        after=after,
        before=before,
    )
    return articles[0] if articles else None


class GoogleNewsRssListener(Listener):
    """Polls one Google News query per ticker every 5 minutes (quota allows 6 calls/minute)."""

    name: ClassVar[str] = "google_news_rss"
    source_class: ClassVar[SourceClass] = SourceClass.HEADLINE
    interval_seconds: ClassVar[float] = DEFAULT_INTERVAL_SECONDS

    def __init__(
        self,
        writer: SignalWriter,
        *,
        tickers: tuple[str, ...] | None = None,
        query_suffix: str = DEFAULT_QUERY_SUFFIX,
        locale: str = "en-US",
        fetcher: HttpFetcher | None = None,
        quota: QuotaManager | None = None,
        interval_seconds: float | None = None,
        log: JsonlLog | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(writer, interval_seconds=interval_seconds, log=log, **kwargs)
        self.tickers = tuple(tickers) if tickers is not None else default_tickers()
        self.query_suffix = query_suffix
        self.locale = locale
        self.fetcher = fetcher or HttpFetcher(
            base_url=BASE_URL,
            rate_limiter=RateLimiter(60.0 / CALLS_PER_MINUTE),
            max_retries=3,
            quota=quota,
            source=SOURCE,
        )

    def search(self, ticker: str) -> str:
        """The RSS document for one ticker."""
        return self.fetcher.get_text(
            SEARCH_PATH,
            params={
                "q": build_query(ticker, self.query_suffix),
                "hl": self.locale,
                "gl": "US",
                "ceid": "US:en",
            },
        )

    def poll(self) -> list[SignalEvent]:
        """One pass over the configured tickers."""
        observed_at = self._clock()
        events: list[SignalEvent] = []
        for ticker in self.tickers:
            query = build_query(ticker, self.query_suffix)
            try:
                xml_text = self.search(ticker)
            except FetchError as exc:
                if self.log is not None:
                    self.log.write(
                        "ticker_failed", listener=self.name, ticker=ticker, error=str(exc)[:200]
                    )
                continue
            events.extend(
                parse_google_news_rss(
                    xml_text, ticker=ticker, query=query, observed_at=observed_at, source=self.name
                )
            )
        return events

    def close(self) -> None:
        self.fetcher.close()


__all__ = [
    "BASE_URL",
    "CALLS_PER_MINUTE",
    "DEFAULT_EXCLUDE_TITLE_CONTAINS",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_QUERY_SUFFIX",
    "SEARCH_PATH",
    "SOURCE",
    "GoogleNewsRssListener",
    "article_times",
    "build_query",
    "default_tickers",
    "first_article_time",
    "parse_google_news_rss",
    "parse_rss_pubdate",
]
