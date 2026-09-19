"""gold-api.com connector: the keyless spot gold price.

Gold is one of the plan's standing regime signals (prior 5: "wars, gold and the dollar set the
regime"). It is keyless, fast and cheap (60 calls an hour), so it is a **critical** series for the
snapshot: if the gold price is missing, the cycle has no regime reading and the ingest layer degrades
to hold-only rather than deciding on a half-blind macro picture.

Verified live 2026-09-19: ``GET https://api.gold-api.com/price/XAU`` returns
``{"name": "Gold", "price": 4379.0, "symbol": "XAU", "updatedAt": "2026-09-19T03:08:40Z"}``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agentoquant.data import ConnectorResult, SourceError, Transport
from agentoquant.data.quota_manager import QuotaExceeded

#: The key this connector's budget and cache are filed under in ``config/sources.yaml``.
SOURCE = "gold_api"

BASE_URL = "https://api.gold-api.com"

#: The series name written to the ledger for the gold spot price.
SERIES = "gold_spot_xau"


class GoldApiConnector:
    """The keyless gold spot reading."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def price(self, symbol: str = "XAU", *, force: bool = False) -> dict:
        """The current spot price for ``symbol`` (gold by default)."""
        response = self.transport.get_json(
            f"{BASE_URL}/price/{symbol}",
            source=SOURCE,
            ttl_seconds=self.transport.quota.ttl_seconds(SOURCE, 300),
            force=force,
        )
        payload = response.payload
        if not isinstance(payload, dict) or "price" not in payload:
            raise SourceError(SOURCE, "response carried no price field")
        try:
            price = float(payload["price"])
        except (TypeError, ValueError) as exc:
            raise SourceError(SOURCE, "price was not a number") from exc
        return {
            "price": price,
            "currency": payload.get("currency"),
            "symbol": payload.get("symbol", symbol),
            "updated_at": payload.get("updatedAt"),
            "source_updated_at_readable": payload.get("updatedAtReadable"),
            "from_cache": response.from_cache,
        }

    def fetch(self, *, force: bool = False) -> ConnectorResult:
        """The gold series as one ledger-bound reading."""
        try:
            fields = self.price(force=force)
        except (SourceError, QuotaExceeded) as exc:
            return ConnectorResult(
                source=SOURCE,
                coin_or_series=SERIES,
                fields={"missing": True, "checked_at": datetime.now(UTC).isoformat()},
                quota_remaining=self.transport.quota.remaining(SOURCE),
                is_stale=True,
                error=str(exc),
            )
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=SERIES,
            fields=fields,
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=False,
            calls=0 if fields.get("from_cache") else 1,
        )


__all__ = ["BASE_URL", "SERIES", "SOURCE", "GoldApiConnector"]
