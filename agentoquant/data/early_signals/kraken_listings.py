"""Kraken early-signal listener: the asset-listings RSS feed plus a fast ``AssetPairs`` poll.

Two feeds, one listener (``config/sources.yaml`` -> ``kraken_listings_rss`` and ``kraken_rest``):

* **RSS** - ``https://blog.kraken.com/feed/`` (2 calls/minute). Kraken announces a listing there with
  the category ``Asset Listings`` and a title like ``TREAD is available for trading!``. The feed
  redirects ``/feed/`` to ``/feed``; httpx follows it.
* **AssetPairs** - ``https://api.kraken.com/0/public/AssetPairs``, polled every 10 to 30 seconds as the
  plan requires (default 20 s). A pair that was not in the previous snapshot **is** a listing signal,
  and it is visible there before the blog post exists. The baseline snapshot is kept in a state file
  (``data/early_signals/kraken_pairs.json``, gitignored) so a restart does not re-emit the whole
  catalogue as new listings. On the very first run with no state file the catalogue is recorded as the
  baseline and no events are emitted, which is logged as ``kraken_baseline``; that is deliberate and
  is stated in the Task 4 report.

Everything here is public and read-only. No Kraken private endpoint, no write endpoint, ever.
"""

from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ElementTree
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, ClassVar

from agentoquant.config_loader import repo_root
from agentoquant.data.early_signals import (
    LOGGER,
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

BASE_URL = "https://api.kraken.com"
ASSET_PAIRS_PATH = "/0/public/AssetPairs"
RSS_BASE_URL = "https://blog.kraken.com"
RSS_PATH = "/feed/"

#: Quotas from config/sources.yaml.
ASSETPAIRS_CALLS_PER_MINUTE = 15
RSS_CALLS_PER_MINUTE = 2

#: Default AssetPairs poll interval: inside the plan's 10 to 30 second window.
DEFAULT_ASSETPAIRS_INTERVAL_SECONDS = 20.0
#: The RSS feed is 2 calls/minute, so it is polled far less often than AssetPairs.
DEFAULT_RSS_INTERVAL_SECONDS = 60.0

#: Where the AssetPairs baseline lives between runs. Under ``/data/``, which is gitignored.
DEFAULT_STATE_PATH = "data/early_signals/kraken_pairs.json"

#: Quote currencies worth a signal for this system (Kraken spot, USD-quoted universe). Kraken reports
#: fiat quotes with a legacy ``Z`` prefix (``ZUSD``). Pass ``quote_filter=None`` for every pair.
DEFAULT_QUOTE_FILTER: frozenset[str] = frozenset(
    {"ZUSD", "USD", "USDT", "USDC", "ZEUR", "EUR", "ZCAD", "CAD"}
)

#: Kraken's legacy asset codes, normalized to the ticker the rest of the system uses.
ASSET_ALIASES: dict[str, str] = {"XBT": "BTC", "XDG": "DOGE"}

#: RSS categories that mark a listing announcement.
LISTING_CATEGORIES: frozenset[str] = frozenset({"asset listings", "asset listing"})

#: RSS title shapes that mark a listing announcement even when the category is missing.
LISTING_TITLE_MARKERS: tuple[str, ...] = (
    "available for trading",
    "will be available for trading",
    "is now live for trading",
    "listing on kraken",
    "now tradable",
)


def parse_rss_pubdate(value: str | None) -> datetime | None:
    """RFC-822 ``pubDate`` (``Wed, 16 Sep 2026 18:48:58 +0000``) to aware UTC."""
    if not value:
        return None
    try:
        return as_utc(parsedate_to_datetime(value.strip()))
    except (TypeError, ValueError):
        return None


def is_listing_item(title: str, categories: list[str]) -> bool:
    """True when an RSS item is a Kraken asset-listing announcement."""
    lowered = {category.strip().lower() for category in categories}
    if lowered & LISTING_CATEGORIES:
        return True
    title_lower = title.lower()
    return any(marker in title_lower for marker in LISTING_TITLE_MARKERS)


def parse_kraken_rss(
    xml_text: str,
    *,
    observed_at: datetime | None = None,
    source: str = "kraken_listings",
) -> list[SignalEvent]:
    """Parse the Kraken blog RSS feed into listing :class:`SignalEvent` objects.

    Uses the standard library's XML parser: ``feedparser`` is not on the addendum's pinned list, so
    adding it would be an unpinned dependency.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise FetchError(f"kraken RSS feed is not valid XML: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        return []
    observed_at = observed_at or utcnow()

    events: list[SignalEvent] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        categories = [(node.text or "").strip() for node in item.findall("category")]
        if not is_listing_item(title, categories):
            continue
        if not title and not link:
            continue
        published = parse_rss_pubdate(item.findtext("pubDate"))
        events.append(
            SignalEvent(
                source_class=SourceClass.EXCHANGE_ANNOUNCEMENT,
                event_type="listing",
                raw_text_or_ref=link or title,
                detected_at=published or observed_at,
                ticker=_kraken_rss_ticker(title),
                source=source,
                observed_at=observed_at,
                published_at=published,
            )
        )
    return events


def _kraken_rss_ticker(title: str) -> str | None:
    """Ticker from a Kraken listing title, e.g. ``TREAD is available for trading!`` -> ``TREAD``."""
    ticker = single_ticker(title)
    if ticker is None:
        return None
    return ASSET_ALIASES.get(ticker, ticker)


def parse_asset_pairs(payload: Any) -> dict[str, dict[str, str]]:
    """``/0/public/AssetPairs`` -> ``{pair_key: {base, quote, altname, wsname}}``.

    Pagination is ignored on purpose: with no ``pair`` parameter Kraken returns the full catalogue in
    one response (1450 pairs, 674 KB, verified 2026-09-19).
    """
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}
    pairs: dict[str, dict[str, str]] = {}
    for key, info in result.items():
        if not isinstance(info, dict):
            continue  # the response can carry a non-pair "last" key
        pairs[str(key)] = {
            "base": str(info.get("base") or ""),
            "quote": str(info.get("quote") or ""),
            "altname": str(info.get("altname") or ""),
            "wsname": str(info.get("wsname") or ""),
        }
    return pairs


def pair_ticker(info: dict[str, str]) -> str | None:
    """The base asset of a pair, normalized (``XBT`` -> ``BTC``)."""
    wsname = info.get("wsname") or ""
    base = wsname.split("/")[0].strip() if "/" in wsname else str(info.get("base") or "").strip()
    if not base:
        return None
    base = base.upper()
    return ASSET_ALIASES.get(base, base)


class KrakenPairsState:
    """The AssetPairs baseline, persisted between runs (atomic write: temp file + rename)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else repo_root() / DEFAULT_STATE_PATH

    def load(self) -> set[str] | None:
        """The recorded pair keys, or ``None`` when there is no usable state file yet."""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        pairs = data.get("pairs") if isinstance(data, dict) else None
        if not isinstance(pairs, list):
            return None
        return {str(pair) for pair in pairs}

    def save(self, pairs: set[str], *, updated_at: datetime | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": as_utc(updated_at or utcnow()).isoformat(),
            "pairs": sorted(pairs),
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, self.path)


class KrakenListingsListener(Listener):
    """Kraken listing signals: ``AssetPairs`` every 20 s, the listings RSS every 60 s."""

    name: ClassVar[str] = "kraken_listings"
    source_class: ClassVar[SourceClass] = SourceClass.EXCHANGE_ANNOUNCEMENT
    interval_seconds: ClassVar[float] = DEFAULT_ASSETPAIRS_INTERVAL_SECONDS

    def __init__(
        self,
        writer: SignalWriter,
        *,
        state_path: Path | str | None = None,
        quote_filter: frozenset[str] | None = DEFAULT_QUOTE_FILTER,
        rss_interval_seconds: float = DEFAULT_RSS_INTERVAL_SECONDS,
        pairs_fetcher: HttpFetcher | None = None,
        rss_fetcher: HttpFetcher | None = None,
        interval_seconds: float | None = None,
        log: JsonlLog | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(writer, interval_seconds=interval_seconds, log=log, **kwargs)
        self.state = KrakenPairsState(state_path)
        self.quote_filter = quote_filter
        self.rss_interval_seconds = float(rss_interval_seconds)
        self.pairs_fetcher = pairs_fetcher or HttpFetcher(
            base_url=BASE_URL,
            rate_limiter=RateLimiter(60.0 / ASSETPAIRS_CALLS_PER_MINUTE),
            max_retries=3,
        )
        self.rss_fetcher = rss_fetcher or HttpFetcher(
            base_url=RSS_BASE_URL,
            rate_limiter=RateLimiter(60.0 / RSS_CALLS_PER_MINUTE),
            max_retries=3,
        )
        self._known_pairs: set[str] | None = None
        self._last_rss_poll: float | None = None

    # -- AssetPairs ---------------------------------------------------------------------------

    def poll_asset_pairs(self, observed_at: datetime) -> list[SignalEvent]:
        """Compare the live catalogue against the baseline and emit the new pairs."""
        payload = self.pairs_fetcher.get_json(ASSET_PAIRS_PATH)
        pairs = parse_asset_pairs(payload)
        if not pairs:
            raise FetchError("kraken AssetPairs returned no pairs")
        current = set(pairs)
        baseline = self._known_pairs
        if baseline is None:
            baseline = self.state.load()
            if baseline is None:
                # First ever run: record the catalogue as the baseline, emit nothing, say so.
                self._known_pairs = current
                self.state.save(current, updated_at=observed_at)
                if self.log is not None:
                    self.log.write(
                        "kraken_baseline",
                        listener=self.name,
                        pairs=len(current),
                        note="first run: catalogue recorded as baseline, no listing events emitted",
                    )
                return []
            self._known_pairs = baseline
        new_keys = sorted(current - baseline)
        self._known_pairs = current
        self.state.save(current, updated_at=observed_at)

        events: list[SignalEvent] = []
        for key in new_keys:
            info = pairs[key]
            if self.quote_filter is not None and info["quote"] not in self.quote_filter:
                continue
            wsname = info["wsname"] or info["altname"] or key
            events.append(
                SignalEvent(
                    source_class=SourceClass.EXCHANGE_ANNOUNCEMENT,
                    event_type="listing",
                    raw_text_or_ref=f"kraken:assetpairs:{key}:{wsname}",
                    detected_at=observed_at,
                    ticker=pair_ticker(info),
                    source=self.name,
                    observed_at=observed_at,
                )
            )
        return events

    # -- RSS ----------------------------------------------------------------------------------

    def poll_rss(self, observed_at: datetime) -> list[SignalEvent]:
        """The Kraken blog feed. ``pubDate`` is the announcement time, so it is ``detected_at``."""
        xml_text = self.rss_fetcher.get_text(RSS_PATH)
        return parse_kraken_rss(xml_text, observed_at=observed_at, source=self.name)

    # -- the poll loop ------------------------------------------------------------------------

    def poll(self) -> list[SignalEvent]:
        """One pass: always AssetPairs, the RSS only every ``rss_interval_seconds``."""
        observed_at = self._clock()
        events = self.poll_asset_pairs(observed_at)
        now = time.monotonic()
        if self._last_rss_poll is None or now - self._last_rss_poll >= self.rss_interval_seconds:
            self._last_rss_poll = now
            try:
                events.extend(self.poll_rss(observed_at))
            except Exception as exc:  # noqa: BLE001 - a blog-feed outage must not hide a listing
                # AssetPairs just showed a new pair; an RSS failure must not throw that away.
                if self.log is not None:
                    self.log.write(
                        "rss_failed", listener=self.name, error=f"{type(exc).__name__}: {exc}"[:300]
                    )
                LOGGER.warning("%s: RSS poll failed: %s", self.name, exc)
        return events

    def close(self) -> None:
        self.pairs_fetcher.close()
        self.rss_fetcher.close()


__all__ = [
    "ASSETPAIRS_CALLS_PER_MINUTE",
    "ASSET_ALIASES",
    "ASSET_PAIRS_PATH",
    "BASE_URL",
    "DEFAULT_ASSETPAIRS_INTERVAL_SECONDS",
    "DEFAULT_QUOTE_FILTER",
    "DEFAULT_RSS_INTERVAL_SECONDS",
    "DEFAULT_STATE_PATH",
    "LISTING_CATEGORIES",
    "LISTING_TITLE_MARKERS",
    "RSS_CALLS_PER_MINUTE",
    "RSS_PATH",
    "KrakenListingsListener",
    "KrakenPairsState",
    "is_listing_item",
    "pair_ticker",
    "parse_asset_pairs",
    "parse_kraken_rss",
    "parse_rss_pubdate",
]
