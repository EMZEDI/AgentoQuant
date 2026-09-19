"""CoinGecko connector: market-wide context and a per-coin gap filler.

The plan uses CoinGecko for market-wide data and as a gap filler for altcoin history (the Kraken OHLC
endpoint returns only the last 720 candles). In the hourly snapshot its job is the **market-wide
context**: total market cap, 24-hour volume, BTC/ETH dominance, and a per-coin cross-check of price
and 24-hour change against Kraken.

The demo key is verified live (header ``x-cg-demo-api-key``); the free tier is about 10 calls a
minute, which is what ``config/sources.yaml`` declares. One call per cycle covers the whole universe,
because ``/coins/markets`` takes a comma-separated ``ids`` list.

Verified live 2026-09-19: ``/coins/markets?ids=venice-token,akash-network,bittensor,render-token,
solana`` returns all five, so the whole universe is reachable in a single call.
"""

from __future__ import annotations

from typing import Any

from agentoquant.config_loader import credentials
from agentoquant.data import ConnectorResult, SourceError, Transport
from agentoquant.data.quota_manager import QuotaExceeded

#: The key this connector's budget and cache are filed under in ``config/sources.yaml``.
SOURCE = "coingecko"

BASE_URL = "https://api.coingecko.com/api/v3"

#: Coin ticker -> CoinGecko id. Verified live 2026-09-19 for every entry.
COIN_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "VVV": "venice-token",
    "TAO": "bittensor",
    "RENDER": "render-token",
    "AKT": "akash-network",
}

#: The market-wide series name written to the ledger.
MARKET_SERIES = "market_global"


class CoinGeckoConnector:
    """Market-wide context plus per-coin cross-checks."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        """The demo key header. The value is read per call and never logged."""
        key = credentials().get("COINGECKO_API_KEY")
        return {"x-cg-demo-api-key": key} if key else {}

    def _ttl(self) -> int:
        return self.transport.quota.ttl_seconds(SOURCE, 300)

    def markets(self, symbols: list[str], *, force: bool = False) -> list[dict]:
        """``/coins/markets`` for the requested coins, one call for the whole list."""
        ids = [COIN_IDS[symbol] for symbol in symbols if symbol in COIN_IDS]
        if not ids:
            return []
        response = self.transport.get_json(
            f"{BASE_URL}/coins/markets",
            source=SOURCE,
            params={
                "vs_currency": "usd",
                "ids": ",".join(ids),
                "per_page": len(ids),
                "page": 1,
                "price_change_percentage": "24h",
            },
            headers=self._headers(),
            ttl_seconds=self._ttl(),
            force=force,
        )
        payload = response.payload
        if not isinstance(payload, list):
            raise SourceError(SOURCE, "/coins/markets did not return a list")
        return payload

    def global_market(self, *, force: bool = False) -> dict:
        """``/global``: total market cap, 24-hour volume and BTC/ETH dominance."""
        response = self.transport.get_json(
            f"{BASE_URL}/global",
            source=SOURCE,
            headers=self._headers(),
            ttl_seconds=self._ttl(),
            force=force,
        )
        payload = response.payload
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise SourceError(SOURCE, "/global carried no data block")
        return {
            "total_market_cap_usd": _usd(data.get("total_market_cap")),
            "total_volume_24h_usd": _usd(data.get("total_volume")),
            "btc_dominance_pct": data.get("market_cap_percentage", {}).get("btc"),
            "eth_dominance_pct": data.get("market_cap_percentage", {}).get("eth"),
            "active_cryptocurrencies": data.get("active_cryptocurrencies"),
            "market_cap_change_24h_pct": data.get("market_cap_change_percentage_24h_usd"),
            "updated_at": data.get("updated_at"),
        }

    def fetch_market_global(self, *, force: bool = False) -> ConnectorResult:
        """The market-wide context as one ledger-bound reading."""
        try:
            fields = self.global_market(force=force)
        except (SourceError, QuotaExceeded) as exc:
            return self.missing(MARKET_SERIES, str(exc))
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=MARKET_SERIES,
            fields=fields,
            quota_remaining=self.transport.quota.remaining(SOURCE),
            calls=1,
        )

    def fetch_markets(self, symbols: list[str], *, force: bool = False) -> list[ConnectorResult]:
        """One record per coin: price, market cap, 24-hour volume and change."""
        try:
            rows = self.markets(symbols, force=force)
        except (SourceError, QuotaExceeded) as exc:
            return [self.missing(symbol, str(exc)) for symbol in symbols]
        by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
        results: list[ConnectorResult] = []
        for symbol in symbols:
            row = by_id.get(COIN_IDS.get(symbol, ""))
            if row is None:
                results.append(self.missing(symbol, "coin id not returned by /coins/markets"))
                continue
            results.append(
                ConnectorResult(
                    source=SOURCE,
                    coin_or_series=symbol,
                    fields={
                        "coingecko_id": row.get("id"),
                        "price": row.get("current_price"),
                        "market_cap_usd": row.get("market_cap"),
                        "market_cap_rank": row.get("market_cap_rank"),
                        "volume_24h_usd": row.get("total_volume"),
                        "high_24h": row.get("high_24h"),
                        "low_24h": row.get("low_24h"),
                        "change_24h_pct": row.get("price_change_percentage_24h"),
                        "circulating_supply": row.get("circulating_supply"),
                        "ath_change_pct": row.get("ath_change_percentage"),
                        "last_updated": row.get("last_updated"),
                    },
                    quota_remaining=self.transport.quota.remaining(SOURCE),
                    calls=1,
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


def _usd(block: Any) -> float | None:
    """CoinGecko wraps money in ``{"usd": 123.0}``."""
    if isinstance(block, dict):
        return block.get("usd")
    return None


__all__ = ["BASE_URL", "COIN_IDS", "MARKET_SERIES", "SOURCE", "CoinGeckoConnector"]
