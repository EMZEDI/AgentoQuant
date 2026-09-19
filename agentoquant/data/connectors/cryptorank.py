"""CryptoRank connector: market-wide context, and the unlock data that mostly is not there.

CryptoRank is the unlock layer: sleeve C's entry rules read an unlock calendar, and the plan's
Adversary is given "the unlock calendar (CryptoRank)" as a tool. What actually works on the free
Sandbox plan is market data. Verified live 2026-09-19 quirks this module encodes:

* the **v2 base is the only one that works** -- ``/v0/*`` hits a Cloudflare challenge and ``/v1/*``
  answers 401 -- so :data:`BASE_URL` is ``https://api.cryptorank.io/v2``;
* the credential goes in the **``X-Api-Key`` header, not a query parameter**;
* ``limit`` must be exactly one of **100, 500 or 1000**; anything else is a ``ValueError`` raised
  before a request is built, because the API's own error for a bad limit is unhelpful;
* **vesting/unlock endpoints are not in the free plan**: ``/currencies/{id}/vesting`` answers
  ``403 "Endpoint is not available in your tariff plan"`` (the Pro tier is $4,750 a year). That
  absence is a fact about this source, so :meth:`CryptoRankConnector.unlock_schedule` **records** it
  (``unlock_data_available=False`` plus the reason) instead of pretending the coin has no unlocks.
  Absence fails closed for sleeve C downstream: "we could not read the calendar" is not "there is
  nothing to unlock".

Because the paid path cannot be read, the unlock event rows are stored verbatim rather than mapped
into invented field names; if the plan is ever upgraded, the shape recorded here is the evidence.

Response shape (v2): ``{"status": {...}, "data": ...}`` for every endpoint -- a dict for ``/global``
and a list for ``/currencies``.
"""

from __future__ import annotations

from typing import Any

from agentoquant.config_loader import credentials
from agentoquant.data import ConnectorResult, SourceError, Transport, redact
from agentoquant.data.quota_manager import QuotaExceeded

#: The key this connector's budget and cache are filed under in ``config/sources.yaml``.
SOURCE = "cryptorank"

BASE_URL = "https://api.cryptorank.io/v2"

#: The ledger series name for the market-wide reading.
GLOBAL_SERIES = "cryptorank_global"

#: The only ``limit`` values the API accepts. Validated locally, before any request.
ALLOWED_LIMITS = (100, 500, 1000)

#: How many unlock rows are kept when the vesting endpoint is reachable (Pro plans only).
MAX_UNLOCK_EVENTS = 200


class CryptoRankConnector:
    """Market-wide context plus a best-effort unlock schedule that fails closed."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def _api_key(self) -> str:
        """The API key, read fresh from the credentials file. The value is never logged."""
        key = credentials().get("CRYPTORANK_API_KEY")
        if not key:
            raise SourceError(SOURCE, "CRYPTORANK_API_KEY is not configured")
        return key

    def _headers(self) -> dict[str, str]:
        """The credential header. CryptoRank takes ``X-Api-Key``, never a query parameter."""
        return {"X-Api-Key": self._api_key()}

    def _ttl(self) -> int:
        return self.transport.quota.ttl_seconds(SOURCE, 3600)

    def global_metrics(self, *, force: bool = False) -> dict:
        """``/global``: total market cap, 24-hour volume and dominance.

        Field names are looked up tolerantly and the response's own key list is recorded alongside
        them, so a reader can see exactly what the API returned rather than trusting a guess.
        """
        response = self.transport.get_json(
            f"{BASE_URL}/global",
            source=SOURCE,
            headers=self._headers(),
            ttl_seconds=self._ttl(),
            force=force,
        )
        payload = response.payload
        if not isinstance(payload, dict):
            raise SourceError(SOURCE, "/global did not return an object")
        _raise_for_error(payload, "/global")
        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            raise SourceError(SOURCE, "/global carried no data block")
        return {
            "total_market_cap_usd": _pick(data, ("totalMarketCap", "marketCap", "total_market_cap")),
            "total_volume_24h_usd": _pick(
                data, ("totalVolume24h", "volume24h", "total_volume_24h")
            ),
            "btc_dominance_pct": _pick(data, ("btcDominance", "btc_dominance")),
            "eth_dominance_pct": _pick(data, ("ethDominance", "eth_dominance")),
            "market_cap_change_24h_pct": _pick(data, ("marketCapChange24h", "marketCapChange24H")),
            "volume_change_24h_pct": _pick(data, ("volumeChange24h", "volumeChange24H")),
            "active_currencies": _pick(data, ("activeCurrencies", "currenciesCount")),
            "defi_market_cap_usd": _pick(data, ("defiMarketCap", "defi_market_cap")),
            "response_keys": sorted(str(key) for key in data)[:40],
            "from_cache": response.from_cache,
        }

    def currencies(self, *, limit: int = 100, force: bool = False) -> list[dict]:
        """``/currencies``: the top coins by market cap, ``limit`` rows.

        Raises :class:`ValueError` unless ``limit`` is one of :data:`ALLOWED_LIMITS`.
        """
        if limit not in ALLOWED_LIMITS:
            raise ValueError(
                f"limit must be one of {list(ALLOWED_LIMITS)} (CryptoRank rejects anything else), "
                f"got {limit!r}"
            )
        response = self.transport.get_json(
            f"{BASE_URL}/currencies",
            source=SOURCE,
            headers=self._headers(),
            params={"limit": limit},
            ttl_seconds=self._ttl(),
            force=force,
        )
        _raise_for_error(response.payload, "/currencies")
        rows = _rows(response.payload, "/currencies")
        return [_currency(row) for row in rows]

    def unlock_schedule(self, currency_id: str, *, force: bool = False) -> ConnectorResult:
        """``/currencies/{id}/vesting`` as a ledger-bound reading that records absence honestly.

        On the free Sandbox plan this answers HTTP 403 ("Endpoint is not available in your tariff
        plan"). That is not an exception and not an empty calendar: the reading comes back
        ``is_stale=True`` with ``unlock_data_available=False`` and the reason, so sleeve C fails
        closed rather than trading an unknown unlock schedule. A transport failure or quota breach is
        recorded the same way (``unlock_data_available=False``), because "unknown" must fail closed
        exactly like "paid-only".
        """
        url = f"{BASE_URL}/currencies/{currency_id}/vesting"
        try:
            response = self.transport.get_json(
                url,
                source=SOURCE,
                headers=self._headers(),
                ttl_seconds=self._ttl(),
                force=force,
                # A 403 here is a fact to record, not an error to raise.
                allow_statuses=(403,),
            )
        except (SourceError, QuotaExceeded) as exc:
            return self._unlock_missing(currency_id, str(exc), None)
        if response.status_code == 403:
            reason = _error_message(response.payload) or "not available in your tariff plan"
            return self._unlock_missing(
                currency_id,
                f"{SOURCE}: vesting is not in the free Sandbox plan (HTTP 403): {reason}",
                403,
            )
        payload = response.payload
        error = _error_message(payload)
        if error is not None:
            return self._unlock_missing(currency_id, f"{SOURCE}: {error}", response.status_code)
        data = payload.get("data") if isinstance(payload, dict) else payload
        try:
            rows = [data] if isinstance(data, dict) else _rows(payload, "vesting")
        except SourceError as exc:
            return self._unlock_missing(currency_id, str(exc), response.status_code)
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=f"{currency_id}_unlock_schedule",
            fields={
                "currency_id": currency_id,
                "unlock_data_available": True,
                "event_count": len(rows),
                "events": rows[:MAX_UNLOCK_EVENTS],
                "http_status": response.status_code,
                "from_cache": response.from_cache,
            },
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=False,
            calls=0 if response.from_cache else 1,
        )

    def fetch(self, *, force: bool = False) -> ConnectorResult:
        """The market-wide reading as one ledger-bound record. Never raises."""
        try:
            fields = self.global_metrics(force=force)
        except (SourceError, QuotaExceeded) as exc:
            return self.missing(GLOBAL_SERIES, str(exc))
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=GLOBAL_SERIES,
            fields=fields,
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=False,
            calls=0 if fields.get("from_cache") else 1,
        )

    def _unlock_missing(self, currency_id: str, error: str, status: int | None) -> ConnectorResult:
        """An unlock reading that records the absence, so downstream fails closed."""
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=f"{currency_id}_unlock_schedule",
            fields={
                "missing": True,
                "currency_id": currency_id,
                "unlock_data_available": False,
                "reason": error,
                "http_status": status,
            },
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=True,
            error=error,
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


def _rows(payload: Any, what: str) -> list[dict]:
    """The row list from a v2 body (``{"data": [...]}`` or a bare list). Raises when malformed."""
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise SourceError(SOURCE, f"{what} did not return a list of rows")
    usable = [row for row in rows if isinstance(row, dict)]
    if rows and not usable:
        raise SourceError(SOURCE, f"{what} carried no usable rows")
    return usable


def _currency(row: dict) -> dict:
    """One ``/currencies`` row trimmed to the fields the ledger keeps."""
    return {
        "id": row.get("id"),
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "rank": row.get("rank"),
        "price_usd": _pick(row, ("price", "priceUsd")),
        "market_cap_usd": _pick(row, ("marketCap", "market_cap")),
        "volume_24h_usd": _pick(row, ("volume24h", "volume")),
        "change_24h_pct": _pick(row, ("percentChange24h", "change24h")),
        "change_7d_pct": _pick(row, ("percentChange7d", "change7d")),
    }


def _error_message(payload: Any) -> str | None:
    """CryptoRank's own error text from a v2 body, or ``None`` when the body carries no error."""
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, dict):
        code = status.get("error_code") or status.get("errorCode")
        message = status.get("error_message") or status.get("errorMessage")
        if code or message:
            return redact(f"{code or ''} {message or ''}").strip()[:200]
    for name in ("message", "error", "errorMessage"):
        value = payload.get(name)
        if value:
            return redact(str(value))[:200]
    return None


def _raise_for_error(payload: Any, what: str) -> None:
    message = _error_message(payload)
    if message is not None:
        raise SourceError(SOURCE, f"{what}: {message}")


def _pick(mapping: dict, names: tuple[str, ...]) -> Any:
    """The first present key from ``names``, or ``None``. Never invents a value."""
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


__all__ = [
    "ALLOWED_LIMITS",
    "BASE_URL",
    "GLOBAL_SERIES",
    "MAX_UNLOCK_EVENTS",
    "SOURCE",
    "CryptoRankConnector",
]
