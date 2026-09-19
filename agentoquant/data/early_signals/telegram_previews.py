"""Telegram announcement channels through the public ``t.me/s/<channel>`` web preview.

**No MTProto, no Telegram account, no ban risk.** That is a recorded rule in ``plan.md`` (MTProto would
need a dedicated account and carries a ban risk the plan declines) and ``config/sources.yaml`` says the
same under ``telegram_previews``. This module only ever GETs the public web preview page.

Page shape (verified 2026-09-19 against ``https://t.me/s/binance_announcements``)::

    <div class="tgme_widget_message ... js-widget_message" data-post="binance_announcements/8948" ...>
      ...
      <a class="tgme_widget_message_date" href="https://t.me/binance_announcements/8948">
        <time datetime="2026-09-16T05:01:58+00:00" class="time">05:01</time></a>
      ...
      <div class="tgme_widget_message_text js-message_text" dir="auto"><b>Updates on ...</b>...</div>

Parsed with the standard library's ``html.parser``: no new dependency, and the message text is collected
with correct nesting rather than a greedy regex.

A channel without a public preview (``krakenfx``, ``okx_announcements`` both 302-redirect to
``https://t.me/<channel>``) yields no messages; that is logged as ``no_messages`` and is not an error.

Events are ``SourceClass.TELEGRAM_PREVIEW``: a preview of an announcement channel is weaker evidence
than the channel owner's own API, which is why it is its own class rather than
``EXCHANGE_ANNOUNCEMENT``.
"""

from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
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
    single_ticker,
    utcnow,
)
from agentoquant.enums import SourceClass

BASE_URL = "https://t.me"
PREVIEW_PATH = "/s/{channel}"

#: Quota from config/sources.yaml: 6 calls/minute.
CALLS_PER_MINUTE = 6
DEFAULT_INTERVAL_SECONDS = 60.0

#: Channels with a working public preview, verified 2026-09-19. Kraken's and OKX's announcement
#: channels have no public preview (302 to the join page), so they are not in the default list.
DEFAULT_CHANNELS: tuple[str, ...] = ("binance_announcements", "bybit_announcements")

#: Keyword map used to classify a message into the ledger's ``event_type`` vocabulary.
_EVENT_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("unlock", ("unlock", "vesting", "cliff", "token release schedule")),
    ("large_transfer", ("whale", "large transfer", "moved to exchange")),
    ("policy", ("delist", "regulat", "lawsuit", "sec charges", "ban ", "policy change")),
    ("listing", ("will list", "to list", "lists ", "listing", "new pair", "spot trading pair")),
    ("release", ("release", "mainnet", "upgrade", "v2.", "v3.", "launch of")),
)

_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{2,}")


def classify_event_type(text: str) -> str:
    """The ledger ``event_type`` for a message body. Defaults to ``headline`` (a preview, not evidence).

    Order matters: a delisting is checked before a listing so "will delist X" is not read as a listing.
    """
    lowered = text.lower()
    for event_type, keywords in _EVENT_TYPE_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return event_type
    return "headline"


class _PreviewParser(HTMLParser):
    """Collects ``(data-post, datetime, text)`` for every message on a ``t.me/s/`` page.

    The ``<time datetime>`` element sits in the message's *footer*, after the text block, so a message
    is only finalized when its own ``js-widget_message`` div closes - finalizing on the text div's close
    would lose every timestamp.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._div_depth = 0
        self._message_div_depth: int | None = None
        self._text_div_depth: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: (value or "") for name, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "div":
            self._div_depth += 1
            if "js-widget_message" in classes:
                self._current = {
                    "post": attributes.get("data-post", ""),
                    "datetime": "",
                    "text": "",
                }
                self._message_div_depth = self._div_depth
            elif self._current is not None and "js-message_text" in classes:
                self._text_div_depth = self._div_depth
        if self._current is None:
            return
        if tag == "time" and attributes.get("datetime") and not self._current["datetime"]:
            self._current["datetime"] = attributes["datetime"]
        if tag == "br" and self._text_div_depth is not None:
            self._current["text"] += "\n"

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._text_div_depth is not None:
            self._current["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag != "div":
            return
        if self._text_div_depth is not None and self._div_depth == self._text_div_depth:
            self._text_div_depth = None
        if self._message_div_depth is not None and self._div_depth == self._message_div_depth:
            if self._current is not None and self._current["post"]:
                self.messages.append(self._current)
            self._current = None
            self._message_div_depth = None
        self._div_depth -= 1


def parse_telegram_preview(
    html_text: str,
    *,
    channel: str = "",
    observed_at: datetime | None = None,
    source: str = "telegram_previews",
) -> list[SignalEvent]:
    """Parse a ``t.me/s/<channel>`` page into preview events.

    ``detected_at`` is the message's own ``<time datetime>`` (UTC); ``raw_text_or_ref`` is the message
    permalink, which is stable and therefore the de-duplication key.
    """
    parser = _PreviewParser()
    parser.feed(html_text)
    observed_at = observed_at or utcnow()

    events: list[SignalEvent] = []
    for message in parser.messages:
        text = _BLANK_LINES.sub("\n", _WHITESPACE.sub(" ", message["text"])).strip()
        post = message["post"]
        if not text:
            continue  # a photo-only message carries nothing to classify
        published: datetime | None = None
        if message["datetime"]:
            try:
                published = as_utc(datetime.fromisoformat(message["datetime"]))
            except ValueError:
                published = None
        events.append(
            SignalEvent(
                source_class=SourceClass.TELEGRAM_PREVIEW,
                event_type=classify_event_type(text),
                raw_text_or_ref=f"{BASE_URL}/{post}" if post else f"telegram:{channel}:{text[:80]}",
                detected_at=published or observed_at,
                ticker=single_ticker(text[:300]),
                source=source,
                observed_at=observed_at,
                published_at=published,
            )
        )
    return events


class TelegramPreviewsListener(Listener):
    """Polls the configured announcement channels' public previews every 60 s."""

    name: ClassVar[str] = "telegram_previews"
    source_class: ClassVar[SourceClass] = SourceClass.TELEGRAM_PREVIEW
    interval_seconds: ClassVar[float] = DEFAULT_INTERVAL_SECONDS

    def __init__(
        self,
        writer: SignalWriter,
        *,
        channels: tuple[str, ...] | None = None,
        fetcher: HttpFetcher | None = None,
        interval_seconds: float | None = None,
        log: JsonlLog | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(writer, interval_seconds=interval_seconds, log=log, **kwargs)
        self.channels = tuple(channels) if channels is not None else DEFAULT_CHANNELS
        self.fetcher = fetcher or HttpFetcher(
            base_url=BASE_URL,
            rate_limiter=RateLimiter(60.0 / CALLS_PER_MINUTE),
            max_retries=3,
        )

    def poll(self) -> list[SignalEvent]:
        """One pass over the configured channels."""
        observed_at = self._clock()
        events: list[SignalEvent] = []
        for channel in self.channels:
            try:
                page = self.fetcher.get_text(PREVIEW_PATH.format(channel=channel))
            except FetchError as exc:
                if self.log is not None:
                    self.log.write(
                        "channel_failed", listener=self.name, channel=channel, error=str(exc)[:200]
                    )
                continue
            parsed = parse_telegram_preview(page, channel=channel, observed_at=observed_at, source=self.name)
            if not parsed and self.log is not None:
                # A channel without a public preview redirects to its join page: no messages, no error.
                self.log.write("no_messages", listener=self.name, channel=channel)
            events.extend(parsed)
        return events

    def close(self) -> None:
        self.fetcher.close()


__all__ = [
    "BASE_URL",
    "CALLS_PER_MINUTE",
    "DEFAULT_CHANNELS",
    "DEFAULT_INTERVAL_SECONDS",
    "PREVIEW_PATH",
    "TelegramPreviewsListener",
    "classify_event_type",
    "parse_telegram_preview",
]
