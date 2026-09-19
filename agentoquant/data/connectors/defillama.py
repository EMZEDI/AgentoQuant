"""DefiLlama connector: protocol usage (fees, revenue, TVL) for the sleeve-B tokens.

Standing prior 3 is "usage beats narrative": only trade hype that has real product usage, on-chain
activity or revenue behind it. DefiLlama's free API is where that evidence comes from for the
sleeve-B AI-infrastructure names -- the plan asks specifically for a usage reading on **VVV (Venice)**
and **AKT (Akash)**, whose fee adapters exist, and notes that **Bittensor (TAO)** and **Render** are
thin, so a slug with no adapter is a legitimate ``missing`` reading rather than a failure.

This is a **usage layer, not a trigger**: it sizes conviction, it never starts a trade. It is keyless
and cheap (about 500 requests a minute upstream, capped at 30 by ``config/sources.yaml``, with a
3600 s cache TTL), so the snapshot reads it every cycle.

Endpoints used, all from the free API: ``/overview/fees`` (one call covers every protocol's fees, so
it is cached and shared across slugs), ``/protocol/{slug}`` and ``/tvl/{slug}``.
**``/emissions*`` is paid** -- verified live it answers HTTP 402 -- and is never called from here;
:data:`PAID_PATH_PREFIXES` refuses it structurally.

Verified-live notes: ``/overview/fees`` returns ``{"protocols": [{"slug", "name", "category",
"chains", "total24h", "total7d", "total30d", "total1y", "totalAllTime", "change_1d", "change_7d",
"mcap", "listedAt", ...}]}``; ``/tvl/{slug}`` returns a **bare JSON number**, not an object.
Revenue is not a field on a fees row, so it is read from the same endpoint with
``dataType=dailyRevenue`` -- and if that view is unavailable the revenue field is recorded as ``None``
with the reason, never estimated.
"""

from __future__ import annotations

from typing import Any

from agentoquant.data import ConnectorResult, SourceError, Transport
from agentoquant.data.quota_manager import QuotaExceeded

#: The key this connector's budget and cache are filed under in ``config/sources.yaml``.
SOURCE = "defillama"

BASE_URL = "https://api.llama.fi"

#: The ledger series name for the whole-market fees overview.
FEES_SERIES = "defillama_fees_overview"

#: Path prefixes that are behind DefiLlama's paid tier (HTTP 402). Never requested.
PAID_PATH_PREFIXES = ("/emissions",)

#: How many protocol rows the ledger keeps from ``/overview/fees`` (the upstream list is thousands of
#: rows long; the reading keeps the busiest ones plus the full counts).
MAX_OVERVIEW_ROWS = 100

#: The sleeve-B slugs the plan names, in the order the snapshot reads them.
SLEEVE_B_SLUGS = ("venice", "akash-network", "bittensor", "render-token")


class DefiLlamaConnector:
    """Fees, revenue and TVL. Usage evidence for sleeve B; never a trigger."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def _ttl(self) -> int:
        return self.transport.quota.ttl_seconds(SOURCE, 3600)

    @staticmethod
    def _guard(path: str) -> None:
        """Refuse a paid endpoint before a request is built (``/emissions*`` is HTTP 402)."""
        if path.startswith(PAID_PATH_PREFIXES):
            raise SourceError(
                SOURCE, f"{path} is a paid DefiLlama endpoint (HTTP 402) and is not called"
            )

    def _get(self, path: str, *, force: bool) -> Any:
        self._guard(path)
        return self.transport.get_json(
            f"{BASE_URL}{path}",
            source=SOURCE,
            ttl_seconds=self._ttl(),
            force=force,
        )

    def _overview(self, data_type: str, *, force: bool) -> tuple[list[dict], bool]:
        """The protocol rows from ``/overview/fees`` for one ``dataType`` and whether it was cached.

        One cached call serves every slug: the snapshot asks for fees once and revenue once per
        cycle, not once per protocol.
        """
        response = self.transport.get_json(
            f"{BASE_URL}/overview/fees",
            source=SOURCE,
            params={"dataType": data_type},
            ttl_seconds=self._ttl(),
            force=force,
        )
        payload = response.payload
        if not isinstance(payload, dict) or not isinstance(payload.get("protocols"), list):
            raise SourceError(SOURCE, f"/overview/fees ({data_type}) carried no protocols list")
        rows = [row for row in payload["protocols"] if isinstance(row, dict)]
        if payload["protocols"] and not rows:
            raise SourceError(SOURCE, f"/overview/fees ({data_type}) carried no usable rows")
        return rows, response.from_cache

    def fees_overview(self, *, force: bool = False) -> dict:
        """``/overview/fees``: the market-wide fees picture, trimmed to the busiest protocols."""
        rows, from_cache = self._overview("dailyFees", force=force)
        top = sorted(rows, key=lambda row: _num(row.get("total24h")) or 0.0, reverse=True)
        return {
            "data_type": "dailyFees",
            "protocol_count": len(rows),
            "protocols_returned": min(len(top), MAX_OVERVIEW_ROWS),
            "protocols": [_trim(row) for row in top[:MAX_OVERVIEW_ROWS]],
            "total_24h_fees_usd": round(
                sum(_num(row.get("total24h")) or 0.0 for row in rows), 2
            ),
            "from_cache": from_cache,
        }

    def protocol(self, slug: str, *, force: bool = False) -> dict:
        """``/protocol/{slug}``: the protocol's own record (TVL, market cap, chains, fees)."""
        response = self._get(f"/protocol/{slug}", force=force)
        payload = response.payload
        if not isinstance(payload, dict) or not payload:
            raise SourceError(SOURCE, f"/protocol/{slug} did not return an object")
        return {
            "slug": payload.get("slug", slug),
            "name": payload.get("name"),
            "category": payload.get("category"),
            "chains": payload.get("chains"),
            "tvl_usd": _num(payload.get("tvl")),
            "mcap_usd": _num(payload.get("mcap")),
            "fees_24h_usd": _first_num(payload, ("total24h", "fees24h")),
            "revenue_24h_usd": _first_num(payload, ("revenue24h", "revenue_24h")),
            "change_1d_pct": _num(payload.get("change_1d")),
            "listed_at": payload.get("listedAt"),
            "from_cache": response.from_cache,
        }

    def _tvl(self, slug: str, *, force: bool) -> tuple[float, bool]:
        """``/tvl/{slug}`` -- a bare JSON number -- plus whether the answer came from the cache."""
        response = self._get(f"/tvl/{slug}", force=force)
        payload = response.payload
        if isinstance(payload, dict):
            payload = payload.get("tvl")
        value = _num(payload)
        if value is None:
            raise SourceError(SOURCE, f"/tvl/{slug} did not return a number")
        return value, response.from_cache

    def tvl(self, slug: str, *, force: bool = False) -> float:
        """The current TVL for one slug, in USD."""
        return self._tvl(slug, force=force)[0]

    def usage_for(self, slug: str, *, force: bool = False) -> ConnectorResult:
        """Fees, revenue and TVL for one slug as a ledger-bound reading.

        A slug DefiLlama has no fees adapter for is a legitimate ``missing`` reading
        (``is_stale=True``, ``fee_adapter=False``), not an exception: Venice and Akash have adapters,
        Bittensor and Render are thin, and the plan expects exactly that. Revenue is ``None`` with a
        reason when the revenue view is unavailable -- never estimated. One live fees call and one
        live revenue call serve every slug in the cycle; only the per-slug TVL is its own request.
        """
        try:
            rows, fees_cached = self._overview("dailyFees", force=force)
        except (SourceError, QuotaExceeded) as exc:
            return self.missing(slug, str(exc))
        live = not fees_cached
        row = _row_for(rows, slug)
        if row is None:
            return ConnectorResult(
                source=SOURCE,
                coin_or_series=slug,
                fields={
                    "missing": True,
                    "slug": slug,
                    "fee_adapter": False,
                    "reason": "DefiLlama has no fees adapter for this slug",
                },
                quota_remaining=self.transport.quota.remaining(SOURCE),
                is_stale=True,
                error=f"{SOURCE}: no fees adapter for {slug!r} in /overview/fees",
            )
        revenue: float | None = None
        revenue_error: str | None = None
        try:
            revenue_rows, revenue_cached = self._overview("dailyRevenue", force=force)
            live = live or not revenue_cached
            revenue_row = _row_for(revenue_rows, slug)
            if revenue_row is not None:
                revenue = _num(revenue_row.get("total24h"))
        except (SourceError, QuotaExceeded) as exc:
            revenue_error = str(exc)
        tvl_usd: float | None = None
        tvl_error: str | None = None
        try:
            tvl_usd, tvl_cached = self._tvl(slug, force=force)
            live = live or not tvl_cached
        except (SourceError, QuotaExceeded) as exc:
            tvl_error = str(exc)
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=slug,
            fields={
                "slug": slug,
                "name": row.get("name"),
                "category": row.get("category"),
                "fee_adapter": True,
                "fees_24h_usd": _num(row.get("total24h")),
                "fees_7d_usd": _num(row.get("total7d")),
                "fees_30d_usd": _num(row.get("total30d")),
                "fees_1y_usd": _num(row.get("total1y")),
                "fees_all_time_usd": _num(row.get("totalAllTime")),
                "change_1d_pct": _num(row.get("change_1d")),
                "change_7d_pct": _num(row.get("change_7d")),
                "revenue_24h_usd": revenue,
                "revenue_error": revenue_error,
                "tvl_usd": tvl_usd,
                "tvl_error": tvl_error,
                "chains": row.get("chains"),
                "mcap_usd": _num(row.get("mcap")),
                "listed_at": row.get("listedAt"),
                "from_cache": not live,
            },
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=False,
            calls=1 if live else 0,
        )

    def fetch(self, *, force: bool = False) -> ConnectorResult:
        """The market-wide fees overview as one ledger-bound reading. Never raises."""
        try:
            fields = self.fees_overview(force=force)
        except (SourceError, QuotaExceeded) as exc:
            return self.missing(FEES_SERIES, str(exc))
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=FEES_SERIES,
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


def _row_for(rows: list[dict], slug: str) -> dict | None:
    """The row whose ``slug`` matches, case-insensitively, or ``None`` when there is no adapter."""
    wanted = slug.strip().lower()
    for row in rows:
        value = row.get("slug")
        if isinstance(value, str) and value.strip().lower() == wanted:
            return row
    return None


def _trim(row: dict) -> dict:
    """A protocol row trimmed to the fields the ledger keeps from the overview."""
    return {
        "slug": row.get("slug"),
        "name": row.get("name"),
        "category": row.get("category"),
        "fees_24h_usd": _num(row.get("total24h")),
        "fees_7d_usd": _num(row.get("total7d")),
        "fees_30d_usd": _num(row.get("total30d")),
        "change_1d_pct": _num(row.get("change_1d")),
    }


def _num(value: Any) -> float | None:
    """A number or ``None``. Never coerces a missing value into zero."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_num(mapping: dict, names: tuple[str, ...]) -> float | None:
    """The first of ``names`` that carries a number, or ``None``."""
    for name in names:
        if name in mapping:
            value = _num(mapping[name])
            if value is not None:
                return value
    return None


__all__ = [
    "BASE_URL",
    "FEES_SERIES",
    "MAX_OVERVIEW_ROWS",
    "PAID_PATH_PREFIXES",
    "SLEEVE_B_SLUGS",
    "SOURCE",
    "DefiLlamaConnector",
]
