"""Kraken connector: public market data plus read-only private calls, including the live fee tier.

Transport decision: the project talks to Kraken with **httpx** rather than by shelling out to
``kraken-cli``. The CLI is installed (0.4.1, ``~/.local/bin/kraken``) and its MCP server is registered
in Hermes, but it needs its own credential store, returns a 54 MB binary's worth of surface, and its
private subcommands read keys from the environment rather than from
``~/.config/agentoquant/credentials.env``. Signing directly is 20 lines, is unit-testable with a mock
transport, and keeps one credential path (``config_loader.credentials()``) for the whole project. The
CLI stays available for interactive read-only checks.

Verified Kraken quirks this module encodes (all measured live 2026-09-18/19, see ``.hermes.md``):

* the ledger endpoint is ``/0/private/Ledgers``, not ``/Ledger`` (that 404s);
* ``TradeVolume`` returns the **maker** fee under ``fees_maker``, not under ``fees`` (``fees`` is the
  taker fee); the account sits on Tier 1, maker 0.4000 percent, next tier 0.3000 percent at $2,500 of
  30-day volume;
* ``OpenOrders``/``ClosedOrders`` nest their rows under ``open``/``closed``;
* private auth is ``API-Key`` + ``API-Sign``, where the signature is HMAC-SHA512 over
  ``path + SHA256(nonce + postdata)`` with the base64-decoded secret.

Safety: :data:`READ_ONLY_PRIVATE_PATHS` is an allow-list and :data:`FORBIDDEN_PATH_FRAGMENTS` is a
deny-list. A private path outside the allow-list raises :class:`~agentoquant.data.SafetyError` before
a request is built, so this connector structurally cannot place, amend, cancel or withdraw anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from agentoquant.config_loader import credentials, load_fee_tiers
from agentoquant.data import ConnectorResult, RetryPolicy, SafetyError, SourceError, Transport
from agentoquant.data.quota_manager import QuotaExceeded

#: The key this connector's budget and cache are filed under in ``config/sources.yaml``.
SOURCE = "kraken_rest"

BASE_URL = "https://api.kraken.com"

#: Every private endpoint this project may call. Read-only by construction; adding a path here is a
#: reviewed change, and the plan's rule is that no write endpoint is ever added.
READ_ONLY_PRIVATE_PATHS: frozenset[str] = frozenset(
    {
        "/0/private/Balance",
        "/0/private/TradeBalance",
        "/0/private/TradeVolume",
        "/0/private/OpenOrders",
        "/0/private/ClosedOrders",
        "/0/private/Ledgers",
        "/0/private/DepositMethods",
        "/0/private/DepositStatus",
    }
)

#: Anything matching one of these never reaches a request. Belt and braces on top of the allow-list.
FORBIDDEN_PATH_FRAGMENTS: tuple[str, ...] = (
    "AddOrder",
    "CancelOrder",
    "CancelAll",
    "AmendOrder",
    "EditOrder",
    "Withdraw",
    "WithdrawCancel",
    "WithdrawInfo",
    "Transfer",
    "WalletTransfer",
    "Allocate",
    "Staking",
)

#: Kraken spells bitcoin ``XBT``. Everything else uses its own ticker against USD.
PAIR_OVERRIDES: dict[str, str] = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}

#: The pair the fee tier is read from. ``TradeVolume`` is quoted per pair; BTC/USD is the reference.
FEE_TIER_PAIR = "XXBTZUSD"


class KrakenConnector:
    """Public market data and read-only private reads. Never a write endpoint."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self._pair_index: dict[str, str] | None = None

    # -- auth ---------------------------------------------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        """The API key pair, read fresh from the credentials file. Values are never logged."""
        values = credentials()
        key = values.get("KRAKEN_API_KEY")
        secret = values.get("KRAKEN_API_SECRET")
        if not key or not secret:
            raise SourceError(SOURCE, "KRAKEN_API_KEY / KRAKEN_API_SECRET are not configured")
        return key, secret

    @staticmethod
    def _guard(path: str) -> None:
        """Refuse any private path that is not on the read-only allow-list."""
        if any(fragment in path for fragment in FORBIDDEN_PATH_FRAGMENTS):
            raise SafetyError(
                f"refusing to call {path!r}: Kraken write/transfer endpoints are banned by "
                f".hermes.md (paper-only until Task 26)"
            )
        if path not in READ_ONLY_PRIVATE_PATHS:
            raise SafetyError(
                f"refusing to call {path!r}: not on the read-only private allow-list "
                f"({', '.join(sorted(READ_ONLY_PRIVATE_PATHS))})"
            )

    def sign(self, path: str, data: dict[str, Any]) -> tuple[dict[str, str], str]:
        """Build the headers **and** the exact POST body for one private call.

        ``API-Sign`` = base64(HMAC-SHA512(base64decode(secret), path + SHA256(nonce + postdata))),
        where ``postdata`` is the urlencoded body that is actually sent. Returning the body alongside
        the headers is deliberate: a nonce that differs between the signature and the request is the
        classic way to get ``EAPI:Invalid nonce``.
        """
        key, secret = self._credentials()
        nonce = str(int(time.time() * 1000))
        payload = {"nonce": nonce, **{k: str(v) for k, v in data.items()}}
        postdata = urllib.parse.urlencode(payload)
        digest = hashlib.sha256((nonce + postdata).encode()).digest()
        mac = hmac.new(base64.b64decode(secret), path.encode() + digest, hashlib.sha512)
        headers = {
            "API-Key": key,
            "API-Sign": base64.b64encode(mac.digest()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        return headers, postdata

    def _private(self, path: str, data: dict[str, Any] | None = None, *, force: bool = False) -> Any:
        """POST a read-only private endpoint and return Kraken's ``result`` object.

        No retry: a Kraken nonce must strictly increase, and a retry would resend the signed body
        verbatim. A failed private call is recorded as a missing reading and the cycle continues.
        """
        self._guard(path)
        headers, postdata = self.sign(path, data or {})
        response = self.transport.post_json(
            f"{BASE_URL}{path}",
            source=SOURCE,
            headers=headers,
            content=postdata,
            force=force,
            ttl_seconds=0,
            retry=RetryPolicy(attempts=1),
        )
        return self._unwrap(response.payload, path)

    def _public(
        self, path: str, params: dict[str, Any] | None = None, *, ttl_seconds: int = 60
    ) -> Any:
        """GET a public endpoint and return Kraken's ``result`` object."""
        response = self.transport.get_json(
            f"{BASE_URL}{path}",
            source=SOURCE,
            params=params,
            ttl_seconds=ttl_seconds,
        )
        return self._unwrap(response.payload, path)

    @staticmethod
    def _unwrap(payload: Any, path: str) -> Any:
        """Kraken returns ``{"error": [...], "result": {...}}``; an error list is a failure."""
        if not isinstance(payload, dict):
            raise SourceError(SOURCE, f"{path}: unexpected response shape")
        errors = payload.get("error") or []
        if errors:
            raise SourceError(SOURCE, f"{path}: {'; '.join(str(e) for e in errors)}")
        return payload.get("result")

    # -- public market data -------------------------------------------------------------------

    def server_time(self) -> dict:
        result = self._public("/0/public/Time", ttl_seconds=0)
        return {
            "unixtime": int((result or {}).get("unixtime") or 0),
            "rfc1123": (result or {}).get("rfc1123"),
        }

    def pair_index(self, *, force: bool = False) -> dict[str, str]:
        """``{altname -> result key}`` for every tradable pair, e.g. ``XBTUSD -> XXBTZUSD``."""
        if self._pair_index is not None and not force:
            return self._pair_index
        result = self._public("/0/public/AssetPairs", ttl_seconds=3600)
        index: dict[str, str] = {}
        for key, spec in (result or {}).items():
            altname = (spec or {}).get("altname") or key
            index[altname] = key
        self._pair_index = index
        return index

    def pair_for(self, symbol: str) -> str:
        """The Kraken altname for a coin ticker (``BTC`` -> ``XBTUSD``)."""
        wanted = PAIR_OVERRIDES.get(symbol.upper(), f"{symbol.upper()}USD")
        index = self.pair_index()
        if wanted in index:
            return wanted
        # Fall back to the override's bare form for assets Kraken lists without a USD pair.
        if symbol.upper() in index:
            return symbol.upper()
        return wanted

    def tickers(self, symbols: list[str], *, ttl_seconds: int = 60) -> dict[str, dict]:
        """Ticker rows keyed by the coin ticker the caller asked for."""
        pairs = [self.pair_for(symbol) for symbol in symbols]
        result = self._public(
            "/0/public/Ticker", {"pair": ",".join(pairs)}, ttl_seconds=ttl_seconds
        )
        index = self.pair_index()
        by_altname = {
            index.get(pair, pair): symbol for symbol, pair in zip(symbols, pairs, strict=True)
        }
        rows: dict[str, dict] = {}
        for key, row in (result or {}).items():
            if key == "last":
                continue
            symbol = by_altname.get(key)
            if symbol is None:
                symbol = next((s for s, p in by_altname.items() if p == key), key)
            rows[symbol] = row
        return rows

    @staticmethod
    def _ticker_fields(row: dict) -> dict:
        """Kraken ticker arrays -> a flat, named dict (a/b/c/v/p/h/l/o, all arrays)."""

        def first(name: str) -> float | None:
            values = row.get(name) or []
            try:
                return float(values[0])
            except (TypeError, ValueError, IndexError):
                return None

        def last_of(name: str) -> float | None:
            values = row.get(name) or []
            try:
                return float(values[-1])
            except (TypeError, ValueError, IndexError):
                return None

        bid, ask, close = first("b"), first("a"), first("c")
        spread_bps = None
        if bid and ask and bid > 0:
            spread_bps = round((ask - bid) / bid * 10_000, 4)
        return {
            "last": close,
            "bid": bid,
            "ask": ask,
            "vwap_24h": first("p"),
            "volume_24h": last_of("v"),
            "volume_24h_base": last_of("v"),
            "high_24h": first("h"),
            "low_24h": first("l"),
            "open_24h": first("o"),
            "trades_24h": last_of("t"),
            "spread_bps": spread_bps,
        }

    def ohlc(self, symbol: str, *, interval: int = 60, since: int | None = None) -> dict:
        """The last candles for a coin. Kraken returns at most 720 of them per call."""
        params: dict[str, Any] = {"pair": self.pair_for(symbol), "interval": interval}
        if since is not None:
            params["since"] = since
        result = self._public("/0/public/OHLC", params, ttl_seconds=60)
        candles: list[list] = []
        last: int | None = None
        for key, value in (result or {}).items():
            if key == "last":
                last = int(value)
                continue
            candles = value or []
        return {
            "pair": self.pair_for(symbol),
            "interval_minutes": interval,
            "candle_count": len(candles),
            "last_candle": candles[-1] if candles else None,
            "last": last,
        }

    # -- read-only private --------------------------------------------------------------------

    def balances(self) -> dict[str, float]:
        """Non-zero balances, keyed by Kraken's asset code."""
        result = self._private("/0/private/Balance") or {}
        out: dict[str, float] = {}
        for asset, amount in result.items():
            try:
                value = float(amount)
            except (TypeError, ValueError):
                continue
            if value:
                out[asset] = value
        return out

    def trade_volume(self, pair: str = FEE_TIER_PAIR) -> dict:
        """``TradeVolume`` for one pair. The maker fee lives under ``fees_maker``, not ``fees``."""
        return self._private("/0/private/TradeVolume", {"pair": pair}) or {}

    def open_orders(self) -> dict:
        """Open orders. Kraken nests the rows under ``open``."""
        result = self._private("/0/private/OpenOrders") or {}
        return result.get("open") or {}

    def closed_orders(self, *, count: int = 10) -> dict:
        """Recently closed orders. Kraken nests the rows under ``closed``."""
        result = self._private("/0/private/ClosedOrders", {"count": count}) or {}
        return result.get("closed") or {}

    def ledgers(self, *, count: int = 10) -> dict:
        """Recent ledger entries. The endpoint is ``/0/private/Ledgers`` (``/Ledger`` 404s)."""
        result = self._private("/0/private/Ledgers", {"count": count}) or {}
        return result.get("ledger") or {}

    # -- derived readings ---------------------------------------------------------------------

    def fee_tier(self, *, force: bool = False) -> dict:
        """The account's current fee tier: live from ``TradeVolume``, else the config pointer.

        ``TradeVolume`` is the source of truth (the plan: "the live fee tier read from TradeVolume");
        ``config/fee_tiers.yaml``'s ``current_tier`` is the offline fallback, and
        ``prefer_live_tier`` decides whether a live read wins. The maker fee comes from
        ``fees_maker``; ``fees`` is the taker side and is read for completeness only.
        """
        table = load_fee_tiers()
        fallback = table.current
        fields: dict[str, Any] = {
            "tier_id": fallback.id,
            "tier_name": fallback.name,
            "maker_pct": fallback.maker_pct,
            "taker_pct": fallback.taker_pct,
            "source": "config",
        }
        try:
            volume = self.trade_volume()
        except (SourceError, QuotaExceeded) as exc:
            fields["error"] = str(exc)
            return fields

        maker_row = ((volume.get("fees_maker") or {}).get(FEE_TIER_PAIR) or {})
        taker_row = ((volume.get("fees") or {}).get(FEE_TIER_PAIR) or {})
        maker = maker_row.get("fee")
        taker = taker_row.get("fee")
        fields["volume_30d_usd"] = _to_float(volume.get("volume"))
        fields["assets_on_platform_usd"] = _to_float(
            (volume.get("inputs") or {}).get("domain_assets_on_platform")
        )
        fields["next_volume_usd"] = _to_float(maker_row.get("nextvolume"))
        if maker is None:
            fields["error"] = "TradeVolume carried no fees_maker row for the reference pair"
            return fields
        maker_pct = _to_float(maker)
        taker_pct = _to_float(taker)
        fields.update(
            {
                "maker_pct": maker_pct,
                "taker_pct": taker_pct,
                "raw_fee_maker": maker,
                "raw_fee_taker": taker,
                "next_fee_maker": maker_row.get("nextfee"),
                "next_fee_taker": taker_row.get("nextfee"),
                "source": "live" if table.prefer_live_tier else "config",
            }
        )
        if table.prefer_live_tier:
            matched = _tier_for_maker(table, maker_pct)
            if matched is not None:
                fields["tier_id"] = matched.id
                fields["tier_name"] = matched.name
                fields["maker_pct"] = matched.maker_pct
                fields["tier_matched_from_config"] = True
            else:
                fields["tier_name"] = f"live maker {maker_pct}%"
                fields["tier_matched_from_config"] = False
        return fields

    # -- connector surface --------------------------------------------------------------------

    def fetch_market(self, symbols: list[str], *, force: bool = False) -> list[ConnectorResult]:
        """One record per coin: ticker fields, the pair used and the live fee tier on each row."""
        fee = self.fee_tier(force=force)
        results: list[ConnectorResult] = []
        try:
            rows = self.tickers(symbols)
        except (SourceError, QuotaExceeded) as exc:
            return [
                self.missing(symbol, str(exc)) for symbol in symbols
            ]
        for symbol in symbols:
            row = rows.get(symbol)
            if row is None:
                results.append(self.missing(symbol, "no ticker row returned for this pair"))
                continue
            fields = self._ticker_fields(row)
            fields["pair"] = self.pair_for(symbol)
            fields["fee_tier"] = {
                "tier_id": fee.get("tier_id"),
                "maker_pct": fee.get("maker_pct"),
                "taker_pct": fee.get("taker_pct"),
                "source": fee.get("source"),
            }
            results.append(
                ConnectorResult(
                    source=SOURCE,
                    coin_or_series=symbol,
                    fields=fields,
                    quota_remaining=self.transport.quota.remaining(SOURCE),
                    calls=1,
                )
            )
        return results

    def fetch_fee_tier(self, *, force: bool = False) -> ConnectorResult:
        """The live fee tier as its own ledger record."""
        fields = self.fee_tier(force=force)
        missing = bool(fields.get("error"))
        return ConnectorResult(
            source=SOURCE,
            coin_or_series="fee_tier",
            fields=fields,
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=missing,
            error=fields.get("error"),
            calls=1,
        )

    def fetch_account(self) -> ConnectorResult:
        """Free cash and open-order count. Read-only; no amounts are ever printed as a secret."""
        try:
            balances = self.balances()
            orders = self.open_orders()
        except (SourceError, QuotaExceeded) as exc:
            return self.missing("account", str(exc))
        fields = {
            "balance_count": len(balances),
            "free_cash_usd": balances.get("ZUSD", 0.0),
            "free_cash_cad": balances.get("ZCAD", 0.0),
            "free_cash_usdc": balances.get("USDC", 0.0),
            "open_orders": len(orders),
            "balances": {asset: amount for asset, amount in sorted(balances.items())},
        }
        return ConnectorResult(
            source=SOURCE,
            coin_or_series="account",
            fields=fields,
            quota_remaining=self.transport.quota.remaining(SOURCE),
            calls=2,
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


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tier_for_maker(table: Any, maker_pct: float | None) -> Any:
    """The config tier whose maker fee equals the live one, or ``None`` when nothing matches."""
    if maker_pct is None:
        return None
    for tier in table.tiers:
        if abs(tier.maker_pct - maker_pct) < 0.005:
            return tier
    return None


def now_utc() -> datetime:
    """A UTC timestamp helper shared by the connectors' ``updated_at`` fields."""
    return datetime.now(UTC)


__all__ = [
    "BASE_URL",
    "FEE_TIER_PAIR",
    "FORBIDDEN_PATH_FRAGMENTS",
    "PAIR_OVERRIDES",
    "READ_ONLY_PRIVATE_PATHS",
    "SOURCE",
    "KrakenConnector",
    "now_utc",
]
