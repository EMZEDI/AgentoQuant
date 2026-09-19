"""Alpha Vantage connector: the news-sentiment confirmation layer.

Alpha Vantage supplies per-ticker news sentiment (``NEWS_SENTIMENT``) plus, on the same key, a daily
gold/dollar series. It is a **confirmation layer only**: standing prior 4 says policy news moves
direction and prior 5 reads the regime from wars, gold and the dollar, but neither is a trigger. The
free tier is **25 calls a day in total** (``config/sources.yaml``), which is the whole budget, so this
source is never on the critical path -- a missing reading degrades the cycle rather than failing it,
and the hourly snapshot makes at most one call (the rest are cache hits on a 3600 s TTL).

Verified live 2026-09-19 quirk this module encodes: with a bad, absent or exhausted key Alpha Vantage
answers **HTTP 200** with a body carrying an ``Information``, ``Note`` or ``Error Message`` key
instead of ``feed``. That is a failure, not an empty feed, and the message is scrubbed of the key
before it reaches an error string (the key value is never echoed anywhere).

Response shape (verified): ``{"items": "50", "sentiment_score_definition": "...",
"feed": [{"title": ..., "url": ..., "time_published": "20260919T031500", "authors": [...],
"summary": ..., "overall_sentiment_score": 0.21, "overall_sentiment_label": "Somewhat-Bullish",
"ticker_sentiment": [{"ticker": "CRYPTO:BTC", "relevance_score": "0.9",
"sentiment_score": "0.31", "sentiment_label": "Bullish"}]}]}``.
"""

from __future__ import annotations

from typing import Any

from agentoquant.config_loader import credentials
from agentoquant.data import ConnectorResult, SourceError, Transport, redact
from agentoquant.data.quota_manager import QuotaExceeded

#: The key this connector's budget and cache are filed under in ``config/sources.yaml``.
SOURCE = "alpha_vantage"

BASE_URL = "https://www.alphavantage.co"

#: ``/query`` is the single endpoint; the function is a parameter.
ENDPOINT = f"{BASE_URL}/query"

#: The ledger series name for the news feed.
SERIES = "news_sentiment"

#: The universe the snapshot reads sentiment for. ``CRYPTO:`` is Alpha Vantage's crypto prefix.
DEFAULT_TICKERS = "CRYPTO:BTC"

#: The feed size the free tier returns (``limit`` is accepted up to 1000).
DEFAULT_LIMIT = 50

#: Keys Alpha Vantage uses to answer with prose instead of data. A 200 carrying one of these is an
#: error body (bad key, exhausted daily budget, malformed parameters).
ERROR_KEYS = ("Information", "Note", "Error Message")


class AlphaVantageConnector:
    """News sentiment. Confirmation layer only; 25 calls a day is the whole budget."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def _api_key(self) -> str:
        """The API key, read fresh from the credentials file. The value is never logged."""
        key = credentials().get("ALPHA_VANTAGE_API_KEY")
        if not key:
            raise SourceError(SOURCE, "ALPHA_VANTAGE_API_KEY is not configured")
        return key

    def _ttl(self) -> int:
        return self.transport.quota.ttl_seconds(SOURCE, 3600)

    def news_sentiment(
        self,
        tickers: str = DEFAULT_TICKERS,
        *,
        limit: int = DEFAULT_LIMIT,
        force: bool = False,
    ) -> dict:
        """``NEWS_SENTIMENT`` for one ticker expression, with a computed mean score.

        Raises :class:`~agentoquant.data.SourceError` on an error body (which arrives with HTTP 200),
        on a missing ``feed`` and on a feed whose rows are not objects.
        """
        key = self._api_key()
        response = self.transport.get_json(
            ENDPOINT,
            source=SOURCE,
            params={
                "function": "NEWS_SENTIMENT",
                "tickers": tickers,
                "limit": limit,
                "apikey": key,
            },
            ttl_seconds=self._ttl(),
            force=force,
        )
        payload = response.payload
        if not isinstance(payload, dict):
            raise SourceError(SOURCE, "NEWS_SENTIMENT did not return an object")
        for name in ERROR_KEYS:
            if name in payload:
                # The reason is the API's own words, scrubbed; the key is never included.
                raise SourceError(SOURCE, f"{name}: {_scrub(str(payload[name])[:160], key)}")
        feed = payload.get("feed")
        if not isinstance(feed, list):
            raise SourceError(SOURCE, "NEWS_SENTIMENT carried no feed list")
        items = [_item(row) for row in feed if isinstance(row, dict)]
        if feed and not items:
            raise SourceError(SOURCE, "NEWS_SENTIMENT feed carried no usable rows")
        scores = [
            row["overall_sentiment_score"]
            for row in items
            if row["overall_sentiment_score"] is not None
        ]
        return {
            "tickers": tickers,
            "item_count": len(items),
            "items": items,
            "items_with_score": len(scores),
            "mean_sentiment_score": round(sum(scores) / len(scores), 6) if scores else None,
            "sentiment_label_counts": _label_counts(items),
            "from_cache": response.from_cache,
        }

    def fetch(self, *, force: bool = False) -> ConnectorResult:
        """The news-sentiment feed as one ledger-bound reading.

        Never raises: an error body, a missing key, a quota breach or a transport failure all become a
        missing reading (``is_stale=True``) so the cycle degrades gracefully.
        """
        try:
            fields = self.news_sentiment(force=force)
        except (SourceError, QuotaExceeded) as exc:
            return self.missing(SERIES, str(exc))
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=SERIES,
            fields=fields,
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=False,
            calls=0 if fields.get("from_cache") else 1,
        )

    def missing(self, coin_or_series: str, error: str) -> ConnectorResult:
        """A missing reading for this source, shaped like a real one."""
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=coin_or_series,
            fields={"missing": True},
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=True,
            error=error,
        )


def _item(row: dict) -> dict:
    """One feed row, trimmed to the fields the ledger keeps (the summary prose is dropped)."""
    tickers = row.get("ticker_sentiment")
    return {
        "title": row.get("title"),
        "url": row.get("url"),
        "time_published": row.get("time_published"),
        "source": row.get("source"),
        "overall_sentiment_score": _float(row.get("overall_sentiment_score")),
        "overall_sentiment_label": row.get("overall_sentiment_label"),
        "ticker_sentiment": [
            {
                "ticker": entry.get("ticker"),
                "relevance_score": _float(entry.get("relevance_score")),
                "sentiment_score": _float(entry.get("sentiment_score")),
                "sentiment_label": entry.get("sentiment_label"),
            }
            for entry in (tickers if isinstance(tickers, list) else [])
            if isinstance(entry, dict)
        ],
    }


def _label_counts(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in items:
        label = row.get("overall_sentiment_label")
        if isinstance(label, str) and label:
            counts[label] = counts.get(label, 0) + 1
    return counts


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scrub(text: str, key: str) -> str:
    """Remove the credential value from a message, then apply the project's redaction rules."""
    if key and key in text:
        text = text.replace(key, "<redacted>")
    return redact(text)


__all__ = [
    "BASE_URL",
    "DEFAULT_LIMIT",
    "DEFAULT_TICKERS",
    "ENDPOINT",
    "ERROR_KEYS",
    "SERIES",
    "SOURCE",
    "AlphaVantageConnector",
]
