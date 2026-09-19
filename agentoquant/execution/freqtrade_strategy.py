"""The thin bridge between the pipeline and freqtrade. Owned by Task 6, dry-run only.

Two halves, deliberately kept apart:

``freqtrade_user_data/strategies/AgentBridgeStrategy.py``
    Runs inside freqtrade. It imports the standard library and freqtrade only: it never imports
    ``agentoquant``, never calls a model and never polls Telegram. It reads the signal document the
    pipeline publishes and writes an ack.

this module
    The Python-side helpers the loop and the tests use: the placeholder decision source for Phase 0,
    the strategy's file paths, the dry-run assertion on the freqtrade config, and the same
    read/ack helpers the strategy implements in-process, so the bridge can be exercised offline.

The placeholder source is Phase 0 scaffolding. It is replaced by the real decision cascade in Phase 2
and is only reachable through ``agentoquant paper --placeholder``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentoquant.config_loader import load_fee_tiers, load_sleeves, repo_root
from agentoquant.enums import Action, ConfidenceBand, Sleeve
from agentoquant.execution.signal_store import (
    DOCUMENT_SCHEMA_VERSION,
    SignalStore,
    is_fresh,
    pair_for,
)
from agentoquant.ledger.schema import DecisionCard

#: freqtrade strategy interface version this bridge targets (freqtrade 2026.8 reports 3).
BRIDGE_INTERFACE_VERSION = 3

STRATEGY_FILENAME = "AgentBridgeStrategy.py"
STRATEGY_DIRNAME = "strategies"
USER_DATA_DIRNAME = "freqtrade_user_data"
CONFIG_FILENAME = "config.json"

#: How long a published card stays live for the bridge. One hourly cadence plus a margin.
SIGNAL_MAX_AGE_S = 75 * 60

#: The default placeholder universe, used when the sleeve config cannot be read.
DEFAULT_PLACEHOLDER_UNIVERSE: tuple[str, ...] = ("BTC", "ETH", "SOL")

#: Tokens that must never appear in the strategy file: the bridge stays dependency-free, offline and
#: free of any inbound Telegram path (the Hermes gateway owns inbound).
FORBIDDEN_STRATEGY_TOKENS: tuple[str, ...] = (
    "import agentoquant",
    "from agentoquant",
    "getUpdates",
    "webhook",
    "openai",
    "anthropic",
    "httpx",
    "requests.post",
)


class BridgeConfigError(RuntimeError):
    """The freqtrade config cannot be traded against in dry-run. Names the key, never the value."""


# ----------------------------------------------------------------------------------------------
# Paths and the dry-run assertion
# ----------------------------------------------------------------------------------------------


def user_data_dir() -> Path:
    return repo_root() / USER_DATA_DIRNAME


def strategy_dir() -> Path:
    return user_data_dir() / STRATEGY_DIRNAME


def strategy_path() -> Path:
    """The bridge strategy file freqtrade loads."""
    return strategy_dir() / STRATEGY_FILENAME


def freqtrade_config_path() -> Path:
    return user_data_dir() / CONFIG_FILENAME


def load_freqtrade_config(path: Path | str | None = None) -> dict[str, Any]:
    """Read the freqtrade config. Returns the parsed mapping; raises if it is unreadable."""
    config_path = Path(path) if path is not None else freqtrade_config_path()
    if not config_path.exists():
        raise BridgeConfigError(f"missing freqtrade config: {config_path}")
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeConfigError(f"{config_path}: not valid JSON ({type(exc).__name__})") from exc
    if not isinstance(payload, dict):
        raise BridgeConfigError(f"{config_path}: expected a JSON object")
    return payload


def assert_dry_run_config(path: Path | str | None = None) -> dict[str, Any]:
    """Fail closed unless the freqtrade config is a dry-run config with no real key.

    Three checks: ``dry_run`` is true, the exchange is Kraken, and neither ``exchange.key`` nor
    ``exchange.secret`` carries a value. Messages name the key only; a value is never echoed.
    """
    config = load_freqtrade_config(path)
    if config.get("dry_run") is not True:
        raise BridgeConfigError("dry_run is not true: refusing to trade against this config")
    exchange = config.get("exchange")
    if not isinstance(exchange, dict):
        raise BridgeConfigError("exchange section is missing")
    if exchange.get("name") != "kraken":
        raise BridgeConfigError("exchange.name is not kraken")
    for key in ("key", "secret"):
        value = exchange.get(key)
        if value:
            raise BridgeConfigError(
                f"exchange.{key} carries a value: keys belong in the environment, never in the repo"
            )
    return config


def check_strategy_hygiene(source: str | None = None) -> list[str]:
    """Return the isolation violations in the bridge strategy's source (empty means isolated)."""
    text = source if source is not None else strategy_path().read_text(encoding="utf-8")
    return [token for token in FORBIDDEN_STRATEGY_TOKENS if token in text]


def strategy_interface_version(source: str | None = None) -> int:
    """The ``INTERFACE_VERSION`` the strategy declares, or -1 when it declares none."""
    text = source if source is not None else strategy_path().read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("INTERFACE_VERSION"):
            _, _, value = stripped.partition("=")
            try:
                return int(value.strip())
            except ValueError:
                return -1
    return -1


# ----------------------------------------------------------------------------------------------
# The Phase 0 placeholder decision source (replaced by the cascade in Phase 2)
# ----------------------------------------------------------------------------------------------

#: One placeholder cycle in eight. Entries, adds and reductions all appear, so the soak exercises
#: the laddered entry, the trailing stop and the partial exit rather than only HOLD.
PLACEHOLDER_PATTERN: tuple[Action, ...] = (
    Action.ENTER_LADDERED,
    Action.TRAIL_STOP,
    Action.HOLD,
    Action.TRIM,
    Action.TAKE_PROFIT_LADDER,
    Action.HOLD,
    Action.ADD,
    Action.EXIT,
)

#: Size each placeholder action asks for, in percent of book value.
PLACEHOLDER_SIZE_PCT: dict[Action, float] = {
    Action.ENTER_LADDERED: 3.0,
    Action.ADD: 2.0,
    Action.TRIM: 1.5,
    Action.TAKE_PROFIT_LADDER: 3.0,
    Action.EXIT: 3.0,
}


def placeholder_universe() -> tuple[str, ...]:
    """Sleeve A's coins from ``config/sleeves.yaml``, with a fixed fallback."""
    try:
        sleeves = load_sleeves()
        coins = tuple(sleeves.sleeves[Sleeve.A].coins)
    except Exception:  # pragma: no cover - a broken config fails closed at CLI startup
        return DEFAULT_PLACEHOLDER_UNIVERSE
    return coins or DEFAULT_PLACEHOLDER_UNIVERSE


def placeholder_action(sequence: int) -> Action:
    """The action the placeholder source picks for cycle number ``sequence`` (0-based)."""
    return PLACEHOLDER_PATTERN[int(sequence) % len(PLACEHOLDER_PATTERN)]


def placeholder_card(
    cycle_id: str,
    *,
    sequence: int = 0,
    universe: tuple[str, ...] | None = None,
    fee_tier: str | None = None,
) -> DecisionCard:
    """A deterministic Decision Card for one placeholder cycle.

    Every field the addendum's Decision Card shape requires is filled, so the card is a real card:
    it goes through the Risk Gate, is written to the ledger and is rendered to Telegram exactly like
    a Phase 2 card.
    """
    coins = universe or placeholder_universe()
    action = placeholder_action(sequence)
    holds = action in {Action.HOLD, Action.REBALANCE, Action.CANCEL_ORDER}
    management = action in {Action.TRAIL_STOP, Action.TAKE_PROFIT_LADDER}
    coin = coins[int(sequence) % len(coins)] if not holds else None
    if management and coin is None:
        coin = coins[0]
    size_pct = None if holds else PLACEHOLDER_SIZE_PCT.get(action, 2.0)
    confidence = 50 if holds else (68 if management else 72)
    if confidence >= 70:
        band = ConfidenceBand.STANDARD
    elif confidence >= 55:
        band = ConfidenceBand.SMALL
    else:
        band = ConfidenceBand.NO_TRADE
    if fee_tier is None:
        try:
            fee_tier = load_fee_tiers().current_tier
        except Exception:  # pragma: no cover - a broken config fails closed at CLI startup
            fee_tier = "tier_1"
    return DecisionCard(
        cycle_id=cycle_id,
        selected_proposal_id=None if holds else f"placeholder-{cycle_id}",
        action=action,
        coin=coin,
        sleeve=Sleeve.A if coin else None,
        size_pct=size_pct,
        confidence=confidence,
        confidence_band=band,
        p_up=0.5 if holds else 0.58,
        interval_low=0.45 if holds else 0.55,
        interval_high=0.55 if holds else 0.62,
        ev_after_fees=0.0 if holds else 0.004,
        fee_tier_assumed=fee_tier,
        evidence_split={"primary": 0 if holds else 1, "verified": 0 if holds else 2, "unverified": 0},
        strongest_objection=None
        if holds
        else {"severity": 2, "text": "placeholder source: the thesis is not a real forecast yet"},
        flip_condition="placeholder: no live thesis to flip",
    )


# ----------------------------------------------------------------------------------------------
# The bridge's read and ack helpers, mirrored by the strategy in-process
# ----------------------------------------------------------------------------------------------


def signal_age_seconds(document: dict[str, Any], *, now: datetime | None = None) -> float | None:
    """How old a published document is, or ``None`` when it carries no usable timestamp."""
    stamp = document.get("published_at")
    if not isinstance(stamp, str):
        return None
    try:
        published = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (moment - published).total_seconds()


def should_act(document: dict[str, Any]) -> bool:
    """Whether the bridge should place anything for this document.

    Fails closed: an unapproved document, a HOLD, or a size of zero places nothing.
    """
    execution = document.get("execution") or {}
    if not execution.get("approved"):
        return False
    if document.get("action") == Action.HOLD.value:
        return False
    return float(execution.get("size_pct") or 0.0) > 0.0


def poll_signal(
    store: SignalStore | None = None,
    *,
    max_age_s: float | None = SIGNAL_MAX_AGE_S,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The current signal, or ``None`` when nothing is published or it is stale."""
    signal_store = store or SignalStore()
    return signal_store.latest(max_age_s=max_age_s, now=now)


def ack_signal(
    store: SignalStore | None,
    *,
    cycle_id: str,
    result: dict[str, Any],
) -> None:
    """Record what the bridge did with a cycle, in the same store the strategy writes to."""
    (store or SignalStore()).ack(cycle_id, result)


def document_is_usable(document: dict[str, Any], *, max_age_s: float, now: datetime | None = None) -> bool:
    """Fresh and at the schema version this bridge understands."""
    if document.get("schema_version") != DOCUMENT_SCHEMA_VERSION:
        return False
    return is_fresh(document, max_age_s=max_age_s, now=now)


__all__ = [
    "BRIDGE_INTERFACE_VERSION",
    "BridgeConfigError",
    "DEFAULT_PLACEHOLDER_UNIVERSE",
    "FORBIDDEN_STRATEGY_TOKENS",
    "PLACEHOLDER_PATTERN",
    "SIGNAL_MAX_AGE_S",
    "ack_signal",
    "assert_dry_run_config",
    "check_strategy_hygiene",
    "document_is_usable",
    "freqtrade_config_path",
    "load_freqtrade_config",
    "pair_for",
    "placeholder_action",
    "placeholder_card",
    "placeholder_universe",
    "poll_signal",
    "should_act",
    "signal_age_seconds",
    "strategy_dir",
    "strategy_interface_version",
    "strategy_path",
    "user_data_dir",
]
