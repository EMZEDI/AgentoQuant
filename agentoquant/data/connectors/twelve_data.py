"""Twelve Data connector: gold (XAU/USD) and the dollar proxy.

Two of the plan's standing regime signals come from here: the gold price (cross-checked against
gold-api.com, which is keyless) and the **dollar proxy**. Prior 5 reads escalation with gold and the
dollar rising together as risk-off for crypto beta, so both series are critical to the snapshot.

Dollar proxy: Twelve Data's free tier has no ``DXY`` symbol (verified: HTTP 404 "symbol invalid"),
so the proxy is ``USDX`` -- the ICE US Dollar Index feed -- which returns 200 on the free tier.
``EUR/USD`` also works and is the textbook inverse proxy, but it costs another credit; ``USDX`` is the
single call the snapshot makes. The choice is recorded in the ledger as ``dollar_proxy_symbol`` so a
later reader can see exactly what was measured.

Free-tier budgeting: Twelve Data spends **credits per endpoint, not per request** -- 8 credits a
minute, measured live when a burst of probes hit HTTP 429 "You have run out of API credits for the
current minute ... limit being 8". ``config/sources.yaml`` declares 800 calls a day, which a single
hourly snapshot never approaches; the per-minute ceiling is enforced by
:data:`~agentoquant.data.quota_manager.MIN_INTERVAL_SECONDS` (8 s between calls), which only ever
removes calls.
"""

from __future__ import annotations

from typing import Any

from agentoquant.config_loader import credentials
from agentoquant.data import ConnectorResult, SourceError, Transport
from agentoquant.data.quota_manager import QuotaExceeded

#: The key this connector's budget and cache are filed under in ``config/sources.yaml``.
SOURCE = "twelve_data"

BASE_URL = "https://api.twelvedata.com"

#: The two macro symbols the snapshot reads. Verified live 2026-09-19 (both HTTP 200).
GOLD_SYMBOL = "XAU/USD"
DOLLAR_PROXY_SYMBOL = "USDX"

#: Ledger series names.
GOLD_SERIES = "gold_usd_xau"
DOLLAR_SERIES = "dollar_proxy_usdx"


class TwelveDataConnector:
    """Gold and the dollar proxy, one credit-priced call each."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def _api_key(self) -> str:
        key = credentials().get("TWELVE_DATA_API_KEY")
        if not key:
            raise SourceError(SOURCE, "TWELVE_DATA_API_KEY is not configured")
        return key

    def _ttl(self) -> int:
        return self.transport.quota.ttl_seconds(SOURCE, 300)

    def price(self, symbol: str, *, force: bool = False) -> dict:
        """``/price`` for one symbol. Raises on an error body (Twelve Data answers 200 with one)."""
        response = self.transport.get_json(
            f"{BASE_URL}/price",
            source=SOURCE,
            params={"symbol": symbol, "apikey": self._api_key()},
            ttl_seconds=self._ttl(),
            force=force,
        )
        payload = response.payload
        if not isinstance(payload, dict):
            raise SourceError(SOURCE, f"{symbol}: unexpected response shape")
        if payload.get("status") == "error" or "price" not in payload:
            # The message names the symbol and the API's own reason; never the key.
            reason = payload.get("message") or payload.get("code") or "no price field"
            raise SourceError(SOURCE, f"{symbol}: {str(reason)[:160]}")
        try:
            price = float(payload["price"])
        except (TypeError, ValueError) as exc:
            raise SourceError(SOURCE, f"{symbol}: price was not a number") from exc
        return {"symbol": symbol, "price": price, "from_cache": response.from_cache}

    def quote(self, symbol: str, *, force: bool = False) -> dict:
        """``/quote`` for one symbol: the richer reading (open/high/low/close/change)."""
        response = self.transport.get_json(
            f"{BASE_URL}/quote",
            source=SOURCE,
            params={"symbol": symbol, "apikey": self._api_key()},
            ttl_seconds=self._ttl(),
            force=force,
        )
        payload = response.payload
        if not isinstance(payload, dict) or payload.get("status") == "error":
            reason = (payload or {}).get("message") if isinstance(payload, dict) else "bad shape"
            raise SourceError(SOURCE, f"{symbol}: {str(reason)[:160]}")
        return {
            "symbol": payload.get("symbol", symbol),
            "name": payload.get("name"),
            "exchange": payload.get("exchange"),
            "open": _float(payload.get("open")),
            "high": _float(payload.get("high")),
            "low": _float(payload.get("low")),
            "close": _float(payload.get("close")),
            "previous_close": _float(payload.get("previous_close")),
            "change_pct": _float(payload.get("percent_change")),
            "datetime": payload.get("datetime"),
        }

    def fetch_macro(self, *, force: bool = False) -> list[ConnectorResult]:
        """Gold and the dollar proxy, each as its own ledger record.

        A failure in one symbol does not lose the other: they are separate records, so the ingest
        layer can report exactly which half of the regime reading is missing.
        """
        results: list[ConnectorResult] = []
        for series, symbol in ((GOLD_SERIES, GOLD_SYMBOL), (DOLLAR_SERIES, DOLLAR_PROXY_SYMBOL)):
            try:
                fields = self.price(symbol, force=force)
            except (SourceError, QuotaExceeded) as exc:
                results.append(self.missing(series, str(exc)))
                continue
            fields["dollar_proxy_symbol"] = DOLLAR_PROXY_SYMBOL
            fields["kind"] = "gold" if series == GOLD_SERIES else "dollar_proxy"
            results.append(
                ConnectorResult(
                    source=SOURCE,
                    coin_or_series=series,
                    fields=fields,
                    quota_remaining=self.transport.quota.remaining(SOURCE),
                    calls=0 if fields.get("from_cache") else 1,
                )
            )
        return results

    def missing(self, coin_or_series: str, error: str) -> ConnectorResult:
        """A missing reading for this source."""
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=coin_or_series,
            fields={"missing": True},
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=True,
            error=error,
        )


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BASE_URL",
    "DOLLAR_PROXY_SYMBOL",
    "DOLLAR_SERIES",
    "GOLD_SERIES",
    "GOLD_SYMBOL",
    "SOURCE",
    "TwelveDataConnector",
]
