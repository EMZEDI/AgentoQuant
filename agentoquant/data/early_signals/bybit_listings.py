"""Bybit public announcement endpoint listener (keyless, verified reachable 2026-09-19).

Endpoint: ``GET https://api.bybit.com/v5/announcements/index?locale=en-US&limit=N&type=new_crypto``
(``config/sources.yaml`` -> ``bybit_announcements``, 10 calls/minute, keyless). Response shape::

    {"retCode": 0, "retMsg": "OK",
     "result": {"list": [{"title": "...", "description": "...",
                          "type": {"key": "new_crypto", "title": "New Cryptocurrency"},
                          "tags": ["Listing"], "url": "https://announcements.bybit.com/...",
                          "dateTimestamp": 1789728312000, "publishTime": 1789728312000}]}}

Every event is a ``SourceClass.EXCHANGE_ANNOUNCEMENT`` record: an exchange's own announcement is
primary evidence for the Verifier (Task 11), not a headline.

``event_type`` mapping. The ledger vocabulary is ``listing | unlock | large_transfer | release |
headline | policy``; a *delisting* is not in it and the schema is frozen, so a delisting announcement
is recorded as ``policy`` (an exchange policy action on an existing asset) while a new listing is
``listing``. Both carry the announcement title in ``raw_text_or_ref``'s sibling log line and in the
listener log, so nothing is lost. Flagged in the Task 4 report.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from agentoquant.data.early_signals import (
    HttpFetcher,
    JsonlLog,
    Listener,
    RateLimiter,
    SignalEvent,
    SignalWriter,
    single_ticker,
    utcnow,
)
from agentoquant.data.quota_manager import QuotaManager
from agentoquant.enums import SourceClass

BASE_URL = "https://api.bybit.com"
ANNOUNCEMENTS_PATH = "/v5/announcements/index"

#: Announcement ``type.key`` values this listener treats as an early signal.
#: ``new_crypto`` is Bybit's new listing/perpetual bucket; ``delistings`` is its delisting bucket.
LISTING_TYPE_KEYS: tuple[str, ...] = ("new_crypto",)
DELISTING_TYPE_KEYS: tuple[str, ...] = ("delistings",)
WATCHED_TYPE_KEYS: tuple[str, ...] = LISTING_TYPE_KEYS + DELISTING_TYPE_KEYS

#: Quota from config/sources.yaml: 10 calls/minute.
CALLS_PER_MINUTE = 10

#: The ``config/sources.yaml`` key every call from this listener is charged to (the shared budget).
SOURCE = "bybit_announcements"


def _ms_to_datetime(value: Any) -> datetime | None:
    """Bybit sends epoch milliseconds; anything unusable returns ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def parse_bybit_announcements(
    payload: Any,
    *,
    observed_at: datetime | None = None,
    source: str = "bybit_listings",
) -> list[SignalEvent]:
    """Parse one ``/v5/announcements/index`` response into :class:`SignalEvent` objects.

    A response with a non-zero ``retCode`` is not an error the caller can use, so it yields no events
    (the caller's fetch already succeeded); the listener logs the raw code when it is not ``0``.
    """
    if not isinstance(payload, dict) or payload.get("retCode") not in (0, None):
        return []
    result = payload.get("result")
    items = result.get("list") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return []
    observed_at = observed_at or utcnow()

    events: list[SignalEvent] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        type_block = item.get("type")
        type_key = type_block.get("key") if isinstance(type_block, dict) else None
        if type_key not in WATCHED_TYPE_KEYS:
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title and not url:
            continue
        published = _ms_to_datetime(item.get("publishTime")) or _ms_to_datetime(
            item.get("dateTimestamp")
        )
        events.append(
            SignalEvent(
                source_class=SourceClass.EXCHANGE_ANNOUNCEMENT,
                event_type="listing" if type_key in LISTING_TYPE_KEYS else "policy",
                raw_text_or_ref=url or title,
                detected_at=published or observed_at,
                ticker=single_ticker(title),
                source=source,
                observed_at=observed_at,
                published_at=published,
            )
        )
    return events


class BybitListingsListener(Listener):
    """Fast poll of Bybit's announcement index, 20 s by default (quota allows one call per 6 s)."""

    name: ClassVar[str] = "bybit_listings"
    source_class: ClassVar[SourceClass] = SourceClass.EXCHANGE_ANNOUNCEMENT
    interval_seconds: ClassVar[float] = 20.0

    def __init__(
        self,
        writer: SignalWriter,
        *,
        locale: str = "en-US",
        limit: int = 20,
        fetcher: HttpFetcher | None = None,
        quota: QuotaManager | None = None,
        interval_seconds: float | None = None,
        log: JsonlLog | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(writer, interval_seconds=interval_seconds, log=log, **kwargs)
        self.locale = locale
        self.limit = limit
        self.fetcher = fetcher or HttpFetcher(
            base_url=BASE_URL,
            rate_limiter=RateLimiter(60.0 / CALLS_PER_MINUTE),
            max_retries=3,
            quota=quota,
            source=SOURCE,
        )

    def poll(self) -> list[SignalEvent]:
        """One announcement poll. Raises :class:`FetchError` on a hard fetch failure."""
        payload = self.fetcher.get_json(
            ANNOUNCEMENTS_PATH,
            params={"locale": self.locale, "limit": self.limit},
        )
        if isinstance(payload, dict) and payload.get("retCode") not in (0, None):
            raise RuntimeError(
                f"bybit announcements returned retCode={payload.get('retCode')!r} "
                f"({payload.get('retMsg')!r})"
            )
        return parse_bybit_announcements(payload, observed_at=self._clock(), source=self.name)

    def close(self) -> None:
        self.fetcher.close()


__all__ = [
    "ANNOUNCEMENTS_PATH",
    "BASE_URL",
    "CALLS_PER_MINUTE",
    "DELISTING_TYPE_KEYS",
    "LISTING_TYPE_KEYS",
    "SOURCE",
    "WATCHED_TYPE_KEYS",
    "BybitListingsListener",
    "parse_bybit_announcements",
]
