"""OKX public support-announcements listener (keyless, verified reachable 2026-09-19).

Endpoint: ``GET https://www.okx.com/api/v5/support/announcements?annType=<type>&limit=N``
(``config/sources.yaml`` -> ``okx_announcements``, 10 calls/minute, keyless). Response shape::

    {"code": "0", "msg": "",
     "data": [{"details": [{"annType": "announcements-new-listings",
                            "title": "OKX to list SLX/USDT (Solstice) for spot trading",
                            "url": "https://www.okx.com/en-eu/help/okx-to-list-slx-usdt-...",
                            "pTime": "1783648816124",
                            "businessPTime": "1783648800000"}]}]}

``pTime`` is the publication time and is what ``detected_at`` uses; ``businessPTime`` is the scheduled
business time and is deliberately not used (it can be in the future). Every event carries
``SourceClass.EXCHANGE_ANNOUNCEMENT``.

``event_type`` mapping matches the Bybit listener: a new-listing announcement is ``listing``, a
delisting announcement is ``policy`` (the frozen vocabulary has no ``delisting`` member).
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
from agentoquant.enums import SourceClass

BASE_URL = "https://www.okx.com"
ANNOUNCEMENTS_PATH = "/api/v5/support/announcements"

#: Announcement types polled each pass, mapped to the ledger's ``event_type``.
ANNOUNCEMENT_TYPES: dict[str, str] = {
    "announcements-new-listings": "listing",
    "announcements-delistings": "policy",
}

#: Quota from config/sources.yaml: 10 calls/minute. One pass costs one call per announcement type.
CALLS_PER_MINUTE = 10


def _ms_string_to_datetime(value: Any) -> datetime | None:
    """OKX sends epoch milliseconds as a string; anything unusable returns ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        millis = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def parse_okx_announcements(
    payload: Any,
    *,
    observed_at: datetime | None = None,
    source: str = "okx_listings",
) -> list[SignalEvent]:
    """Parse one ``/api/v5/support/announcements`` response into :class:`SignalEvent` objects."""
    if not isinstance(payload, dict) or str(payload.get("code", "0")) != "0":
        return []
    groups = payload.get("data")
    if not isinstance(groups, list):
        return []
    observed_at = observed_at or utcnow()

    events: list[SignalEvent] = []
    for group in groups:
        details = group.get("details") if isinstance(group, dict) else None
        if not isinstance(details, list):
            continue
        for item in details:
            if not isinstance(item, dict):
                continue
            ann_type = str(item.get("annType") or "").strip()
            event_type = ANNOUNCEMENT_TYPES.get(ann_type)
            if event_type is None:
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title and not url:
                continue
            published = _ms_string_to_datetime(item.get("pTime"))
            events.append(
                SignalEvent(
                    source_class=SourceClass.EXCHANGE_ANNOUNCEMENT,
                    event_type=event_type,
                    raw_text_or_ref=url or title,
                    detected_at=published or observed_at,
                    ticker=single_ticker(title),
                    source=source,
                    observed_at=observed_at,
                    published_at=published,
                )
            )
    return events


class OkxListingsListener(Listener):
    """Polls OKX's new-listing and delisting announcement types every 30 s by default."""

    name: ClassVar[str] = "okx_listings"
    source_class: ClassVar[SourceClass] = SourceClass.EXCHANGE_ANNOUNCEMENT
    interval_seconds: ClassVar[float] = 30.0

    def __init__(
        self,
        writer: SignalWriter,
        *,
        limit: int = 20,
        announcement_types: dict[str, str] | None = None,
        fetcher: HttpFetcher | None = None,
        interval_seconds: float | None = None,
        log: JsonlLog | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(writer, interval_seconds=interval_seconds, log=log, **kwargs)
        self.limit = limit
        self.announcement_types = dict(announcement_types or ANNOUNCEMENT_TYPES)
        self.fetcher = fetcher or HttpFetcher(
            base_url=BASE_URL,
            rate_limiter=RateLimiter(60.0 / CALLS_PER_MINUTE),
            max_retries=3,
        )

    def poll(self) -> list[SignalEvent]:
        """One poll across the configured announcement types."""
        observed_at = self._clock()
        events: list[SignalEvent] = []
        for ann_type in self.announcement_types:
            payload = self.fetcher.get_json(
                ANNOUNCEMENTS_PATH, params={"annType": ann_type, "limit": self.limit}
            )
            if isinstance(payload, dict) and str(payload.get("code", "0")) != "0":
                raise RuntimeError(
                    f"okx announcements returned code={payload.get('code')!r} "
                    f"({payload.get('msg')!r}) for {ann_type}"
                )
            events.extend(
                parse_okx_announcements(payload, observed_at=observed_at, source=self.name)
            )
        return events

    def close(self) -> None:
        self.fetcher.close()


__all__ = [
    "ANNOUNCEMENTS_PATH",
    "ANNOUNCEMENT_TYPES",
    "BASE_URL",
    "CALLS_PER_MINUTE",
    "OkxListingsListener",
    "parse_okx_announcements",
]
