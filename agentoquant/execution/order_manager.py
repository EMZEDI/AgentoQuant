"""Order management: the action vocabulary mapped to its execution path and its freqtrade call.

Owned by Task 6. Paper and dry-run only: this module never places, modifies or cancels a real order
and never moves funds. It builds an :class:`ExecutionPlan` of :class:`OrderIntent` objects and hands
them to a transport; the production transport talks to a **dry-run** freqtrade instance, and the
tests hand it a fake that records calls.

One row per action in :data:`EXECUTION_PATHS`. The rules the plan fixes:

* Entries are post-only limit orders. A laddered entry is two or three slices
  (``adjust_trade_position`` with ``custom_entry_price``); ``add``/DCA is a positive stake capped by
  ``max_entry_position_adjustment``; ``trim`` and ``exit`` use a negative stake or a force exit.
* Stops live on the exchange (``stoploss_on_exchange``). ``trail_stop`` ratchets the stop and never
  loosens it; ``set_stop`` places it.
* A taker order is allowed only for the purposes listed in
  ``risk_limits.execution.market_orders_allowed_for`` (``stop_loss``, ``emergency_exit``); every other
  purpose is rejected by :func:`assert_order_type_allowed` with a named rule.
* The rejected vocabulary (``grid_trade``, ``scalp``, ``hourly_rebalance``, ``market_chase_breakout``)
  is refused up front with :data:`RULE_REJECTED_ACTION`, before any other check runs. Those four are
  deliberately absent from :class:`~agentoquant.enums.Action`.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agentoquant.config_loader import (
    ExecutionLimits,
    FeeTiers,
    RiskLimits,
    load_fee_tiers,
    load_risk_limits,
    load_settings,
)
from agentoquant.enums import REJECTED_ACTIONS, Action, Stage
from agentoquant.execution.signal_store import (
    DEFAULT_LADDER_SLICES,
    EVENT_TIME_STOP_MINUTES,
    pair_for,
)
from agentoquant.ledger.schema import DecisionCard, ExecutionPayload, RiskGateVerdict
from agentoquant.ledger.store import LedgerStore

# ----------------------------------------------------------------------------------------------
# Named rules. Every refusal carries the rule that fired.
# ----------------------------------------------------------------------------------------------

#: The rejection path for the vocabulary the plan bans outright (grid, scalp, hourly rebalance,
#: market chase). It runs before every other check.
RULE_REJECTED_ACTION = "rejected_action_vocabulary"

#: A taker order for a purpose that is not in ``market_orders_allowed_for``.
RULE_MARKET_ORDER_NOT_ALLOWED = "market_order_not_allowed"

#: More slices than ``max_entry_position_adjustment`` allows.
RULE_MAX_ENTRY_ADJUSTMENT = "max_entry_position_adjustment_exceeded"

#: A reprice beyond ``risk_limits.execution.max_reprices``.
RULE_MAX_REPRICES = "max_reprices_exceeded"

#: The rejected vocabulary, verbatim from ``agentoquant/enums.py``.
REJECTED_EXECUTION_ACTIONS: tuple[str, ...] = tuple(REJECTED_ACTIONS)

#: Order types the addendum allows in an ``execution`` record.
ORDER_TYPES: tuple[str, ...] = ("post_only_limit", "market", "stop_loss", "stop_loss_limit")

#: Execution record statuses, from the addendum's ``execution`` payload.
STATUS_FILLED = "filled"
STATUS_PARTIAL = "partial"
STATUS_UNFILLED_TIMEOUT = "unfilled_timeout"
STATUS_CANCELLED = "cancelled"
STATUSES: tuple[str, ...] = (STATUS_FILLED, STATUS_PARTIAL, STATUS_UNFILLED_TIMEOUT, STATUS_CANCELLED)

#: Purpose of an order. Only the purposes in ``market_orders_allowed_for`` may be taker orders.
PURPOSE_ENTRY = "entry"
PURPOSE_ADD = "add"
PURPOSE_TRIM = "trim"
PURPOSE_EXIT = "exit"
PURPOSE_STOP_LOSS = "stop_loss"
PURPOSE_EMERGENCY_EXIT = "emergency_exit"
PURPOSE_TAKE_PROFIT = "take_profit"
PURPOSE_REBALANCE = "rebalance"
PURPOSE_ROTATE = "rotate"
PURPOSE_CANCEL = "cancel"
PURPOSE_NONE = "none"

#: Ladder offsets below the reference price for a buy (above it for a sell), in percent. Post-only
#: orders sit on the maker side by construction, which is what keeps the entry at Tier 1 maker fees.
LADDER_OFFSETS_PCT: tuple[float, ...] = (0.10, 0.25, 0.40)

#: Time in minutes an unfilled entry or exit waits before it is repriced or cancelled.
UNFILLED_TIMEOUT_MINUTES = 10

#: The weekly rebalance runs on Sunday at 00:00 UTC, never hourly (the plan bans hourly rebalancing).
REBALANCE_WEEKDAY = 6
REBALANCE_HOUR = 0

#: The default take-profit ladder, in freqtrade's minimal-ROI form: minutes after entry -> ratio.
DEFAULT_MINIMAL_ROI: dict[str, float] = {"0": 0.04, "30": 0.02, "60": 0.01, "120": 0.0}

#: Fractions of the position a take-profit ladder takes off, largest first.
PARTIAL_EXIT_FRACTIONS: tuple[float, ...] = (0.5, 0.25)

#: The venue the addendum's ``execution`` payload records for Kraken spot.
VENUE_KRAKEN = "kraken"


class UnsupportedActionError(ValueError):
    """An action the execution layer refuses. Carries the named rule that fired."""

    def __init__(self, action: Any, *, rule_fired: str, message: str | None = None) -> None:
        self.action = action
        self.rule_fired = rule_fired
        self.action_name = getattr(action, "value", str(action))
        super().__init__(message or f"{self.action_name}: {rule_fired}")


def is_rejected_action(action: Any) -> bool:
    """Whether ``action`` belongs to the banned vocabulary, as a string or as a plain name."""
    name = getattr(action, "value", action)
    return str(name) in REJECTED_EXECUTION_ACTIONS


# ----------------------------------------------------------------------------------------------
# The plan: one order intent, and the plan that holds them
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderIntent:
    """One order the bridge should place, with the freqtrade call that places it.

    ``freqtrade_call`` names the strategy hook or REST endpoint (``forceenter``, ``forceexit``,
    ``adjust_trade_position``, ``stoploss_on_exchange``, ``cancel_open_order``, ``amend_order``);
    ``freqtrade_kwargs`` carries the arguments that call needs.
    """

    action: Action
    path: str
    pair: str | None
    purpose: str
    side: str  # buy | sell | none
    order_type: str
    freqtrade_call: str
    freqtrade_kwargs: dict[str, Any] = field(default_factory=dict)
    stake_pct: float = 0.0
    stake_amount: float | None = None
    amount: float | None = None
    price: float | None = None
    post_only: bool = True
    reprices_allowed: int = 0
    unfilled_timeout_minutes: int = UNFILLED_TIMEOUT_MINUTES
    keep_queue_position: bool = False
    time_stop_minutes: int | None = None
    slice_index: int = 0
    slice_count: int = 1
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "path": self.path,
            "pair": self.pair,
            "purpose": self.purpose,
            "side": self.side,
            "order_type": self.order_type,
            "freqtrade_call": self.freqtrade_call,
            "freqtrade_kwargs": dict(self.freqtrade_kwargs),
            "stake_pct": self.stake_pct,
            "stake_amount": self.stake_amount,
            "amount": self.amount,
            "price": self.price,
            "post_only": self.post_only,
            "reprices_allowed": self.reprices_allowed,
            "unfilled_timeout_minutes": self.unfilled_timeout_minutes,
            "keep_queue_position": self.keep_queue_position,
            "time_stop_minutes": self.time_stop_minutes,
            "slice_index": self.slice_index,
            "slice_count": self.slice_count,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ExecutionPlan:
    """What one card turns into: the named path, the intents, and why nothing runs when it does not."""

    cycle_id: str
    action: Action
    path: str
    approved: bool
    intents: tuple[OrderIntent, ...] = ()
    rule_fired: str | None = None
    decision_card_id: str | None = None
    detail: str = ""

    @property
    def executes(self) -> bool:
        return bool(self.intents)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "action": self.action.value,
            "path": self.path,
            "approved": self.approved,
            "rule_fired": self.rule_fired,
            "decision_card_id": self.decision_card_id,
            "intent_count": len(self.intents),
            "intents": [intent.as_dict() for intent in self.intents],
            "detail": self.detail,
        }


# ----------------------------------------------------------------------------------------------
# One execution path per action in the vocabulary
# ----------------------------------------------------------------------------------------------

#: The execution path for every :class:`Action`. The test suite asserts this covers the enum exactly.
EXECUTION_PATHS: dict[Action, str] = {
    Action.ENTER_LADDERED: "laddered_entry",
    Action.ADD: "dca_add",
    Action.TRIM: "partial_exit",
    Action.EXIT: "full_exit",
    Action.ROTATE: "rotate",
    Action.SET_STOP: "stop_on_exchange",
    Action.TRAIL_STOP: "trailing_stop_ratchet",
    Action.TAKE_PROFIT_LADDER: "take_profit_ladder",
    Action.EVENT_TRADE: "event_trade_with_time_stop",
    Action.REBALANCE: "scheduled_weekly_rebalance",
    Action.HOLD: "hold_no_op",
    Action.CANCEL_ORDER: "cancel_or_amend_resting",
}

#: The freqtrade call each path uses. ``forceenter``/``forceexit`` are the REST force-entry hooks,
#: the rest are interface version 3 strategy hooks.
FREQTRADE_CALLS: dict[str, str] = {
    "laddered_entry": "forceenter+adjust_trade_position",
    "dca_add": "adjust_trade_position",
    "partial_exit": "adjust_trade_position",
    "full_exit": "forceexit",
    "rotate": "forceexit+forceenter",
    "stop_on_exchange": "stoploss_on_exchange",
    "trailing_stop_ratchet": "custom_stoploss+stoploss_on_exchange",
    "take_profit_ladder": "custom_exit",
    "event_trade_with_time_stop": "forceenter+custom_exit",
    "scheduled_weekly_rebalance": "rebalance",
    "hold_no_op": "none",
    "cancel_or_amend_resting": "cancel_open_order+amend_order",
}

#: Paths that only manage an existing position and therefore carry no size.
SIZE_FREE_PATHS: frozenset[str] = frozenset(
    {
        "stop_on_exchange",
        "trailing_stop_ratchet",
        "take_profit_ladder",
        "scheduled_weekly_rebalance",
        "hold_no_op",
        "cancel_or_amend_resting",
    }
)

#: Purposes a taker order may use. Read from ``risk_limits.execution.market_orders_allowed_for``;
#: this is the fallback when the config cannot be read.
DEFAULT_MARKET_PURPOSES: tuple[str, ...] = (PURPOSE_STOP_LOSS, PURPOSE_EMERGENCY_EXIT)


def assert_order_type_allowed(
    order_type: str, purpose: str, *, limits: ExecutionLimits | None = None
) -> None:
    """Refuse a taker order for a purpose the plan does not allow.

    Stops and emergency exits are the only taker orders; everything else is post-only.
    """
    if order_type != "market":
        return
    allowed = tuple(limits.market_orders_allowed_for) if limits is not None else DEFAULT_MARKET_PURPOSES
    if purpose not in allowed:
        raise UnsupportedActionError(
            purpose,
            rule_fired=RULE_MARKET_ORDER_NOT_ALLOWED,
            message=(
                f"market order not allowed for purpose {purpose!r}; "
                f"allowed purposes are {', '.join(allowed) or 'none'}"
            ),
        )


RULE_UNKNOWN_ACTION = "unknown_action"


def ratchet_stop(current_stop: float | None, candidate_stop: float, *, side: str = "long") -> float:
    """A stop only ever moves in the protective direction.

    For a long position the stop may move up and never down; for a short it may move down and never
    up. Returns the stop that stays in force.
    """
    if current_stop is None:
        return float(candidate_stop)
    if side == "short":
        return min(float(current_stop), float(candidate_stop))
    return max(float(current_stop), float(candidate_stop))


# ----------------------------------------------------------------------------------------------
# Transports: the dry-run venue, and the offline stand-in
# ----------------------------------------------------------------------------------------------


class OrderTransport(Protocol):
    """What the manager needs from a venue. The production implementation talks to dry-run freqtrade."""

    def available(self) -> bool: ...

    def supports(self, intent: OrderIntent) -> bool: ...

    def submit(self, intent: OrderIntent) -> dict[str, Any]: ...


class NullTransport:
    """A transport that reports itself unavailable and refuses to submit anything.

    The paper loop uses it when no dry-run freqtrade is reachable: nothing is placed, and the loop
    records the cycle as degraded instead of inventing a fill.
    """

    def __init__(self, reason: str = "no dry-run venue configured") -> None:
        self.reason = reason

    def available(self) -> bool:
        return False

    def supports(self, intent: OrderIntent) -> bool:
        return False

    def submit(self, intent: OrderIntent) -> dict[str, Any]:
        raise RuntimeError(f"no dry-run venue available: {self.reason}")


def freqtrade_dry_run_transport(
    base_url: str | None = None, *, config_path: Path | str | None = None
) -> OrderTransport:
    """A transport for the local **dry-run** freqtrade REST API, or :class:`NullTransport`.

    The import is guarded and the credentials come from the environment, so importing this module
    never requires freqtrade and never reads a key. The dry-run config is asserted before a client is
    built; a live-looking config yields :class:`NullTransport` rather than a client.
    """
    from agentoquant.execution.freqtrade_strategy import assert_dry_run_config  # local: avoids a cycle

    try:
        assert_dry_run_config(config_path)
    except Exception as exc:  # a config that is not dry-run is never traded against
        return NullTransport(f"freqtrade config is not dry-run: {type(exc).__name__}")
    try:
        from freqtrade_client import FtRestClient  # type: ignore[import-not-found]
    except ImportError:
        return NullTransport("freqtrade_client is not installed")
    url = base_url or "http://127.0.0.1:8080"
    username = os.environ.get("AGENTOQUANT_FREQTRADE_USER", "agentoquant")
    password = os.environ.get("AGENTOQUANT_FREQTRADE_PASSWORD", "")
    return _FreqtradeDryRunTransport(FtRestClient(url, username, password), url)


class _FreqtradeDryRunTransport:
    """Submits the calls freqtrade's REST API exposes. Hook-only paths run inside the strategy.

    ``freqtrade_client`` 2026.8 exposes ``forceenter``, ``forceexit`` and ``cancel_open_order``, and
    nothing for ``adjust_trade_position``, ``custom_stoploss``, ``custom_exit`` or ``amend_order``.
    Those are interface version 3 strategy hooks, so the bridge strategy runs them in-process and the
    pipeline learns what happened from the ack. :meth:`supports` is how the manager tells the two
    apart, and it never guesses.
    """

    #: The calls freqtrade's REST API exposes directly.
    REST_CALLS: frozenset[str] = frozenset({"forceenter", "forceexit", "cancel_open_order"})

    #: REST argument names that stand in for the strategy hook's names.
    KWARG_ALIASES: dict[str, str] = {
        "custom_entry_price": "price",
        "custom_exit_price": "price",
        "entry_tag": "enter_tag",
        "stakeamount": "stake_amount",
        "trade_id": "tradeid",
    }

    def __init__(self, client: Any, base_url: str) -> None:
        self._client = client
        self.base_url = base_url

    def available(self) -> bool:
        """Whether the dry-run API answers.

        ``ping`` reports an unreachable API as ``{"status": "not_running"}`` rather than raising, so
        the response itself is inspected instead of trusting the call not to fail.
        """
        try:
            result = self._client.ping()
        except Exception:
            return False
        if isinstance(result, dict):
            return str(result.get("status", "")).lower() == "pong"
        return False

    def supports(self, intent: OrderIntent) -> bool:
        """Whether the REST API can place this intent. Hook-only paths are run by the strategy."""
        call = intent.freqtrade_call.split("+")[0]
        return call in self.REST_CALLS and callable(getattr(self._client, call, None))

    def _rest_kwargs(self, handler: Any, intent: OrderIntent) -> dict[str, Any]:
        """Map the hook's argument names onto the REST call's, and keep only what it accepts."""
        kwargs = dict(intent.freqtrade_kwargs)
        pair = kwargs.pop("pair", intent.pair)
        for alias, name in self.KWARG_ALIASES.items():
            if alias in kwargs and name not in kwargs:
                kwargs[name] = kwargs.pop(alias)
        try:
            accepted = set(inspect.signature(handler).parameters)
        except (TypeError, ValueError):  # pragma: no cover - a builtin or a C callable
            accepted = set(kwargs)
        accepted.discard("self")
        filtered = {k: v for k, v in kwargs.items() if k in accepted and v is not None}
        if pair is not None:
            filtered["pair"] = pair
        return filtered

    def submit(self, intent: OrderIntent) -> dict[str, Any]:
        call = intent.freqtrade_call.split("+")[0]
        if not self.supports(intent):
            raise NotImplementedError(
                f"{intent.freqtrade_call} is a strategy hook, not a REST call; "
                "the bridge strategy runs it in-process"
            )
        handler = getattr(self._client, call)
        kwargs = self._rest_kwargs(handler, intent)
        result = handler(**kwargs)
        if isinstance(result, dict):
            return result
        return {"status": STATUS_FILLED if result else STATUS_UNFILLED_TIMEOUT, "raw": result}


def coerce_action(value: Any) -> Action:
    """Resolve an action name to :class:`Action`, refusing the banned vocabulary by name.

    A :class:`DecisionCard` already types ``action`` as :class:`Action`, so a banned name cannot even
    be constructed as a card. This check exists for every other caller: an action string arriving from
    outside the schema (a hand-edited file, a tool call) is refused here with a named rule.
    """
    if isinstance(value, Action):
        return value
    name = getattr(value, "value", value)
    if is_rejected_action(name):
        raise UnsupportedActionError(
            name,
            rule_fired=RULE_REJECTED_ACTION,
            message=(
                f"action {name!r} is rejected: it is not part of the execution vocabulary "
                f"(rule {RULE_REJECTED_ACTION})"
            ),
        )
    try:
        return Action(str(name))
    except ValueError:
        raise UnsupportedActionError(
            name,
            rule_fired=RULE_UNKNOWN_ACTION,
            message=f"action {name!r} is not in the action vocabulary",
        ) from None


class OrderManager:
    """Turns a Decision Card plus a Risk Gate verdict into intents, then records what the venue did.

    Dry-run only. The manager builds the plan, refuses anything the plan bans, submits through the
    transport and writes one ``execution`` ledger record per intent with the cycle id.
    """

    def __init__(
        self,
        *,
        ledger: LedgerStore | None = None,
        transport: OrderTransport | None = None,
        limits: RiskLimits | None = None,
        fee_tiers: FeeTiers | None = None,
        book_value_cad: float | None = None,
        usd_cad_rate: float = 1.0,
        max_entry_position_adjustment: int = 3,
        now: datetime | None = None,
    ) -> None:
        self.ledger = ledger
        self.transport: OrderTransport = transport if transport is not None else NullTransport()
        self.limits = limits if limits is not None else load_risk_limits()
        self.fee_tiers = fee_tiers if fee_tiers is not None else load_fee_tiers()
        self.execution_limits = self.limits.execution
        if book_value_cad is None:
            try:
                book_value_cad = float(load_settings().capital.starting_capital)
            except Exception:  # pragma: no cover - a broken config fails closed at CLI startup
                book_value_cad = 0.0
        self.book_value_cad = float(book_value_cad)
        self.usd_cad_rate = float(usd_cad_rate) or 1.0
        self.max_entry_position_adjustment = int(max_entry_position_adjustment)
        self.now = now

    # -- sizing and fees ------------------------------------------------------------------------

    def book_value_usd(self) -> float:
        """Book value in the venue's quote currency. Config carries CAD; the venue trades USD."""
        return self.book_value_cad / self.usd_cad_rate

    def stake_amount(self, size_pct: float) -> float:
        """A card's ``size_pct`` as money, at the current book value."""
        return round(self.book_value_usd() * float(size_pct) / 100.0, 8)

    def fee_pct(self, order_type: str) -> float:
        """The fee percent for this order type at the configured tier (Tier 1 maker 0.40)."""
        tier = self.fee_tiers.current
        return float(tier.taker_pct if order_type == "market" else tier.maker_pct)

    def fee_for(self, fill_price: float | None, fill_qty: float | None, order_type: str) -> float | None:
        """Fee paid on a fill, at the configured tier. ``None`` when nothing filled."""
        if fill_price is None or fill_qty is None:
            return None
        return round(float(fill_price) * float(fill_qty) * self.fee_pct(order_type) / 100.0, 10)

    def clock(self) -> datetime:
        moment = self.now or datetime.now(UTC)
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)

    @property
    def max_reprices(self) -> int:
        return int(self.execution_limits.max_reprices)

    # -- planning -------------------------------------------------------------------------------

    def plan(
        self,
        card: DecisionCard,
        verdict: RiskGateVerdict,
        *,
        cycle_id: str,
        price: float | None = None,
        stop_price: float | None = None,
        target_price: float | None = None,
        rotate_from: str | None = None,
        emergency: bool = False,
        resting_order: Mapping[str, Any] | None = None,
        rebalance_targets: Mapping[str, float] | None = None,
        time_stop_minutes: int | None = None,
        slices: int | None = None,
        current_stop: float | None = None,
        trail_percent: float | None = None,
        amount: float | None = None,
    ) -> ExecutionPlan:
        """The plan for one card. Refuses a banned action before anything else is considered."""
        return self.plan_for_action(
            card.action,
            cycle_id=cycle_id,
            verdict=verdict,
            coin=card.coin,
            price=price,
            stop_price=stop_price,
            target_price=target_price,
            rotate_from=rotate_from,
            emergency=emergency,
            resting_order=resting_order,
            rebalance_targets=rebalance_targets,
            time_stop_minutes=time_stop_minutes,
            slices=slices,
            current_stop=current_stop,
            trail_percent=trail_percent,
            amount=amount,
        )

    def plan_for_action(
        self,
        action: Any,
        *,
        cycle_id: str,
        verdict: RiskGateVerdict | None = None,
        coin: str | None = None,
        price: float | None = None,
        stop_price: float | None = None,
        target_price: float | None = None,
        rotate_from: str | None = None,
        emergency: bool = False,
        resting_order: Mapping[str, Any] | None = None,
        rebalance_targets: Mapping[str, float] | None = None,
        time_stop_minutes: int | None = None,
        slices: int | None = None,
        current_stop: float | None = None,
        trail_percent: float | None = None,
        amount: float | None = None,
    ) -> ExecutionPlan:
        """Build the plan for an action name or member, with or without a gate verdict.

        With no verdict the action is treated as approved at size 0, which is what the dry-run path
        checks use; the paper loop always passes a real verdict.
        """
        resolved = coerce_action(action)
        path = EXECUTION_PATHS.get(resolved)
        if path is None:  # unreachable while the table covers the enum; kept as a closed door
            raise UnsupportedActionError(resolved, rule_fired=RULE_UNKNOWN_ACTION)
        approved = verdict is None or verdict.verdict != "rejected"
        card_id = getattr(verdict, "decision_card_id", None)
        if not approved:
            return ExecutionPlan(
                cycle_id=cycle_id,
                action=resolved,
                path=path,
                approved=False,
                rule_fired=(verdict.rule_fired if verdict is not None else None) or "risk_gate_rejected",
                decision_card_id=card_id,
                detail="the risk gate rejected the card: nothing is placed",
            )
        size_pct = float(verdict.final_size_pct) if verdict is not None else 0.0
        request = _PlanRequest(
            cycle_id=cycle_id,
            coin=coin,
            size_pct=size_pct,
            price=price,
            stop_price=stop_price,
            target_price=target_price,
            rotate_from=rotate_from,
            emergency=emergency,
            resting_order=resting_order or {},
            rebalance_targets=rebalance_targets or {},
            time_stop_minutes=time_stop_minutes,
            slices=slices,
            current_stop=current_stop,
            trail_percent=trail_percent,
            amount=amount,
        )
        intents, detail = self._build(resolved, path, request)
        return ExecutionPlan(
            cycle_id=cycle_id,
            action=resolved,
            path=path,
            approved=True,
            intents=tuple(intents),
            rule_fired=verdict.rule_fired if verdict is not None else None,
            decision_card_id=card_id,
            detail=detail,
        )

    def _build(
        self, action: Action, path: str, request: _PlanRequest
    ) -> tuple[list[OrderIntent], str]:
        """Dispatch to the builder for this path. One branch per action in the vocabulary."""
        if path == "laddered_entry":
            return self._laddered_entry(action, request)
        if path == "dca_add":
            return self._dca_add(action, request)
        if path == "partial_exit":
            return self._reduce(action, request, purpose=PURPOSE_TRIM, fraction=0.5)
        if path == "full_exit":
            return self._full_exit(action, request)
        if path == "rotate":
            return self._rotate(action, request)
        if path == "stop_on_exchange":
            return self._stop_on_exchange(action, request)
        if path == "trailing_stop_ratchet":
            return self._trailing_stop(action, request)
        if path == "take_profit_ladder":
            return self._take_profit_ladder(action, request)
        if path == "event_trade_with_time_stop":
            return self._event_trade(action, request)
        if path == "scheduled_weekly_rebalance":
            return self._rebalance(action, request)
        if path == "hold_no_op":
            return [], "hold: no order is placed"
        if path == "cancel_or_amend_resting":
            return self._cancel_or_amend(action, request)
        raise UnsupportedActionError(action, rule_fired=RULE_UNKNOWN_ACTION)

    # -- one builder per path -------------------------------------------------------------------

    def _laddered_entry(self, action: Action, request: _PlanRequest) -> tuple[list[OrderIntent], str]:
        """Two or three post-only limit slices below the reference price.

        The first slice opens the trade through the force-entry hook; the rest add to it through
        ``adjust_trade_position`` with ``custom_entry_price``, which is the freqtrade hook that takes
        a per-slice price.
        """
        slices = max(2, min(len(LADDER_OFFSETS_PCT), int(request.slices or DEFAULT_LADDER_SLICES)))
        stake = self.stake_amount(request.size_pct)
        per_slice = round(stake / slices, 8)
        intents: list[OrderIntent] = []
        for index in range(slices):
            limit_price = self._limit_price(request.price, LADDER_OFFSETS_PCT[index], side="buy")
            first = index == 0
            call = "forceenter" if first else "adjust_trade_position"
            kwargs: dict[str, Any] = {
                "pair": request.pair,
                "side": "long",
                "order_type": "limit",
                "custom_entry_price": limit_price,
                "entry_tag": request.cycle_id,
            }
            kwargs["stakeamount" if first else "stake_amount"] = per_slice
            intents.append(
                OrderIntent(
                    action=action,
                    path="laddered_entry",
                    pair=request.pair,
                    purpose=PURPOSE_ENTRY,
                    side="buy",
                    order_type="post_only_limit",
                    freqtrade_call=call,
                    freqtrade_kwargs=kwargs,
                    stake_pct=round(request.size_pct / slices, 8),
                    stake_amount=per_slice,
                    price=limit_price,
                    post_only=True,
                    reprices_allowed=self.max_reprices,
                    slice_index=index,
                    slice_count=slices,
                    notes=f"slice {index + 1} of {slices}, post-only limit below the reference price",
                )
            )
        detail = f"{slices} post-only limit slices via forceenter + adjust_trade_position"
        return intents, detail

    def _dca_add(self, action: Action, request: _PlanRequest) -> tuple[list[OrderIntent], str]:
        """A positive stake through ``adjust_trade_position``, capped by the adjustment count."""
        if self.max_entry_position_adjustment <= 0:
            raise UnsupportedActionError(
                action,
                rule_fired=RULE_MAX_ENTRY_ADJUSTMENT,
                message="max_entry_position_adjustment is 0, so no add may be placed",
            )
        slices = max(1, min(2, self.max_entry_position_adjustment))
        stake = self.stake_amount(request.size_pct)
        per_slice = round(stake / slices, 8)
        intents: list[OrderIntent] = []
        for index in range(slices):
            limit_price = self._limit_price(request.price, LADDER_OFFSETS_PCT[index], side="buy")
            intents.append(
                OrderIntent(
                    action=action,
                    path="dca_add",
                    pair=request.pair,
                    purpose=PURPOSE_ADD,
                    side="buy",
                    order_type="post_only_limit",
                    freqtrade_call="adjust_trade_position",
                    freqtrade_kwargs={
                        "pair": request.pair,
                        "stake_amount": per_slice,
                        "custom_entry_price": limit_price,
                        "entry_tag": request.cycle_id,
                    },
                    stake_pct=round(request.size_pct / slices, 8),
                    stake_amount=per_slice,
                    price=limit_price,
                    post_only=True,
                    reprices_allowed=self.max_reprices,
                    slice_index=index,
                    slice_count=slices,
                    notes=f"add {index + 1} of {slices}, positive stake capped by the adjustment count",
                )
            )
        detail = (
            f"{slices} positive-stake adjustments, "
            f"max_entry_position_adjustment={self.max_entry_position_adjustment}"
        )
        return intents, detail

    def _reduce(
        self, action: Action, request: _PlanRequest, *, purpose: str, fraction: float
    ) -> tuple[list[OrderIntent], str]:
        """A partial exit: a negative stake through ``adjust_trade_position``.

        The limit price sits above the reference price, which is the maker side for a sell.
        """
        stake = round(self.stake_amount(request.size_pct) * fraction, 8)
        limit_price = self._limit_price(request.price, LADDER_OFFSETS_PCT[0], side="sell")
        amount = request.amount
        if amount is None and limit_price:
            amount = round(stake / limit_price, 8)
        intent = OrderIntent(
            action=action,
            path="partial_exit",
            pair=request.pair,
            purpose=purpose,
            side="sell",
            order_type="post_only_limit",
            freqtrade_call="adjust_trade_position",
            freqtrade_kwargs={
                "pair": request.pair,
                "stake_amount": -stake,
                "custom_exit_price": limit_price,
                "exit_tag": request.cycle_id,
            },
            stake_pct=round(request.size_pct * fraction, 8),
            stake_amount=-stake,
            amount=amount,
            price=limit_price,
            post_only=True,
            reprices_allowed=self.max_reprices,
            notes=f"negative stake, {int(fraction * 100)} percent of the position",
        )
        return [intent], "partial exit: negative stake via adjust_trade_position"

    def _full_exit(self, action: Action, request: _PlanRequest) -> tuple[list[OrderIntent], str]:
        """Close the position. Post-only by default; a taker order only for an emergency exit."""
        if request.emergency:
            order_type = "market"
            purpose = PURPOSE_EMERGENCY_EXIT
        else:
            order_type = "post_only_limit"
            purpose = PURPOSE_EXIT
        assert_order_type_allowed(order_type, purpose, limits=self.execution_limits)
        limit_price = (
            None
            if request.emergency
            else self._limit_price(request.price, LADDER_OFFSETS_PCT[0], side="sell")
        )
        intent = OrderIntent(
            action=action,
            path="full_exit",
            pair=request.pair,
            purpose=purpose,
            side="sell",
            order_type=order_type,
            freqtrade_call="forceexit",
            freqtrade_kwargs={
                "pair": request.pair,
                "amount": request.amount,
                "ordertype": "market" if request.emergency else "limit",
                "price": limit_price,
                "exit_tag": request.cycle_id,
            },
            stake_pct=request.size_pct,
            amount=request.amount,
            price=limit_price,
            post_only=not request.emergency,
            reprices_allowed=0 if request.emergency else self.max_reprices,
            notes="emergency exit (taker)" if request.emergency else "full exit, post-only limit",
        )
        detail = "full exit via forceexit" + (" (emergency, taker)" if request.emergency else " (post-only)")
        return [intent], detail

    def _rotate(self, action: Action, request: _PlanRequest) -> tuple[list[OrderIntent], str]:
        """Two legs: leave the source coin post-only, then enter the target coin post-only."""
        intents: list[OrderIntent] = []
        if request.rotate_from:
            intents.append(
                OrderIntent(
                    action=action,
                    path="rotate",
                    pair=pair_for(request.rotate_from),
                    purpose=PURPOSE_ROTATE,
                    side="sell",
                    order_type="post_only_limit",
                    freqtrade_call="forceexit",
                    freqtrade_kwargs={
                        "pair": pair_for(request.rotate_from),
                        "ordertype": "limit",
                        "price": self._limit_price(request.price, LADDER_OFFSETS_PCT[0], side="sell"),
                        "exit_tag": f"rotate:{request.cycle_id}",
                    },
                    price=self._limit_price(request.price, LADDER_OFFSETS_PCT[0], side="sell"),
                    post_only=True,
                    reprices_allowed=self.max_reprices,
                    notes="rotate leg 1: leave the source coin",
                )
            )
        entry = OrderIntent(
            action=action,
            path="rotate",
            pair=request.pair,
            purpose=PURPOSE_ROTATE,
            side="buy",
            order_type="post_only_limit",
            freqtrade_call="forceenter",
            freqtrade_kwargs={
                "pair": request.pair,
                "side": "long",
                "ordertype": "limit",
                "stakeamount": self.stake_amount(request.size_pct),
                "custom_entry_price": self._limit_price(request.price, LADDER_OFFSETS_PCT[0], side="buy"),
                "entry_tag": f"rotate:{request.cycle_id}",
            },
            stake_pct=request.size_pct,
            stake_amount=self.stake_amount(request.size_pct),
            price=self._limit_price(request.price, LADDER_OFFSETS_PCT[0], side="buy"),
            post_only=True,
            reprices_allowed=self.max_reprices,
            notes="rotate leg 2: enter the target coin",
        )
        intents.append(entry)
        detail = f"rotate in {len(intents)} legs" + ("" if request.rotate_from else " (source coin unknown)")
        return intents, detail

    def _stop_on_exchange(self, action: Action, request: _PlanRequest) -> tuple[list[OrderIntent], str]:
        """Place the stop on the exchange. A stop is allowed to be a taker order."""
        assert_order_type_allowed("stop_loss_limit", PURPOSE_STOP_LOSS, limits=self.execution_limits)
        intent = OrderIntent(
            action=action,
            path="stop_on_exchange",
            pair=request.pair,
            purpose=PURPOSE_STOP_LOSS,
            side="sell",
            order_type="stop_loss_limit",
            freqtrade_call="stoploss_on_exchange",
            freqtrade_kwargs={
                "pair": request.pair,
                "stoploss_price": request.stop_price,
                "stoploss_on_exchange": True,
            },
            price=request.stop_price,
            post_only=False,
            reprices_allowed=0,
            notes="stop on the exchange",
        )
        detail = "stop placed on the exchange" + (
            "" if request.stop_price else " (price from strategy config)"
        )
        return [intent], detail

    def _trailing_stop(self, action: Action, request: _PlanRequest) -> tuple[list[OrderIntent], str]:
        """Ratchet the stop. The new level is the more protective of the two, never the looser one."""
        candidate = request.stop_price
        if candidate is None and request.price and request.trail_percent:
            candidate = request.price * (1 - request.trail_percent / 100.0)
        new_stop = ratchet_stop(request.current_stop, candidate or 0.0, side="long")
        intent = OrderIntent(
            action=action,
            path="trailing_stop_ratchet",
            pair=request.pair,
            purpose=PURPOSE_STOP_LOSS,
            side="sell",
            order_type="stop_loss_limit",
            freqtrade_call="custom_stoploss+stoploss_on_exchange",
            freqtrade_kwargs={
                "pair": request.pair,
                "previous_stoploss": request.current_stop,
                "new_stoploss": new_stop,
                "trail_percent": request.trail_percent,
                "after_fill": True,
            },
            price=new_stop,
            post_only=False,
            reprices_allowed=0,
            notes=f"ratchet from {request.current_stop} to {new_stop}",
        )
        detail = f"trailing stop ratcheted to {new_stop}"
        return [intent], detail

    def _take_profit_ladder(self, action: Action, request: _PlanRequest) -> tuple[list[OrderIntent], str]:
        """A take-profit ladder: a minimal ROI ladder, or a custom exit with partial exits."""
        ladder = dict(DEFAULT_MINIMAL_ROI)
        if request.target_price and request.price:
            ratio = round(float(request.target_price) / float(request.price) - 1.0, 8)
            ladder = {"0": ratio, "120": 0.0}
        intent = OrderIntent(
            action=action,
            path="take_profit_ladder",
            pair=request.pair,
            purpose=PURPOSE_TAKE_PROFIT,
            side="sell",
            order_type="post_only_limit",
            freqtrade_call="custom_exit",
            freqtrade_kwargs={
                "pair": request.pair,
                "minimal_roi": ladder,
                "partial_exits": list(PARTIAL_EXIT_FRACTIONS),
                "exit_tag": request.cycle_id,
            },
            price=request.target_price,
            post_only=True,
            reprices_allowed=self.max_reprices,
            notes="take-profit ladder with partial exits",
        )
        detail = (
            f"take-profit ladder over {len(ladder)} steps and "
            f"{len(PARTIAL_EXIT_FRACTIONS)} partial exits"
        )
        return [intent], detail

    def _event_trade(self, action: Action, request: _PlanRequest) -> tuple[list[OrderIntent], str]:
        """An event trade: a post-only entry that carries a hard time stop."""
        minutes = int(request.time_stop_minutes or EVENT_TIME_STOP_MINUTES)
        stake = self.stake_amount(request.size_pct)
        intent = OrderIntent(
            action=action,
            path="event_trade_with_time_stop",
            pair=request.pair,
            purpose=PURPOSE_ENTRY,
            side="buy",
            order_type="post_only_limit",
            freqtrade_call="forceenter+custom_exit",
            freqtrade_kwargs={
                "pair": request.pair,
                "side": "long",
                "ordertype": "limit",
                "stakeamount": stake,
                "custom_entry_price": self._limit_price(request.price, LADDER_OFFSETS_PCT[0], side="buy"),
                "entry_tag": f"event:{request.cycle_id}",
            },
            stake_pct=request.size_pct,
            stake_amount=stake,
            price=self._limit_price(request.price, LADDER_OFFSETS_PCT[0], side="buy"),
            post_only=True,
            reprices_allowed=self.max_reprices,
            time_stop_minutes=minutes,
            notes=f"event trade with a hard time stop of {minutes} minutes",
        )
        detail = f"event trade entry with a hard time stop of {minutes} minutes"
        return [intent], detail

    def _rebalance(self, action: Action, request: _PlanRequest) -> tuple[list[OrderIntent], str]:
        """The weekly rebalance, which only fires on its scheduled day and hour."""
        now = self.clock()
        if now.weekday() != REBALANCE_WEEKDAY or now.hour != REBALANCE_HOUR:
            return [], (
                f"weekly rebalance is not due at {now.isoformat()} "
                f"(scheduled weekday {REBALANCE_WEEKDAY} hour {REBALANCE_HOUR} UTC)"
            )
        intent = OrderIntent(
            action=action,
            path="scheduled_weekly_rebalance",
            pair=request.pair,
            purpose=PURPOSE_REBALANCE,
            side="none",
            order_type="post_only_limit",
            freqtrade_call="rebalance",
            freqtrade_kwargs={
                "targets": dict(request.rebalance_targets),
                "scheduled": "weekly",
                "cycle_id": request.cycle_id,
            },
            post_only=True,
            reprices_allowed=0,
            notes="weekly rebalance, scheduled rather than hourly",
        )
        detail = f"weekly rebalance over {len(request.rebalance_targets)} targets"
        return [intent], detail

    def _cancel_or_amend(self, action: Action, request: _PlanRequest) -> tuple[list[OrderIntent], str]:
        """Cancel a resting order, or amend it in place.

        ``AmendOrder`` keeps the queue position when the quantity goes down, so a pure quantity
        decrease is an amend with ``keep_queue_position`` set; a price change or an increase is a
        cancel followed by a fresh order.
        """
        order = dict(request.resting_order)
        order_id = order.get("order_id")
        if not order_id:
            return [], "no resting order given: nothing to cancel or amend"
        pair = order.get("pair") or request.pair
        current_qty = order.get("current_quantity")
        new_qty = order.get("new_quantity")
        new_price = order.get("new_price")
        quantity_decrease = (
            current_qty is not None and new_qty is not None and float(new_qty) < float(current_qty)
        )
        if quantity_decrease and new_price is None:
            intent = OrderIntent(
                action=action,
                path="cancel_or_amend_resting",
                pair=pair,
                purpose=PURPOSE_CANCEL,
                side="none",
                order_type="post_only_limit",
                freqtrade_call="amend_order",
                freqtrade_kwargs={
                    "pair": pair,
                    "order_id": order_id,
                    "qty": new_qty,
                    "keep_queue_position": True,
                },
                amount=new_qty,
                post_only=True,
                keep_queue_position=True,
                notes="quantity decrease: amend in place and keep the queue position",
            )
            return [intent], "amend in place, queue position kept"
        intents = [
            OrderIntent(
                action=action,
                path="cancel_or_amend_resting",
                pair=pair,
                purpose=PURPOSE_CANCEL,
                side="none",
                order_type="post_only_limit",
                freqtrade_call="cancel_open_order",
                freqtrade_kwargs={"pair": pair, "order_id": order_id},
                post_only=True,
                notes="cancel the resting order",
            )
        ]
        if new_price is not None or new_qty is not None:
            intents.append(
                OrderIntent(
                    action=action,
                    path="cancel_or_amend_resting",
                    pair=pair,
                    purpose=PURPOSE_CANCEL,
                    side=order.get("side", "buy"),
                    order_type="post_only_limit",
                    freqtrade_call="forceenter",
                    freqtrade_kwargs={
                        "pair": pair,
                        "ordertype": "limit",
                        "price": new_price,
                        "amount": new_qty,
                        "entry_tag": f"reprice:{request.cycle_id}",
                    },
                    price=new_price,
                    amount=new_qty,
                    post_only=True,
                    reprices_allowed=self.max_reprices,
                    notes="replacement order after the cancel; a reprice loses the queue position",
                )
            )
        return intents, "cancel and replace: a price change does not keep the queue position"

    # -- submitting and recording ---------------------------------------------------------------

    def execute(
        self, plan: ExecutionPlan, *, cycle_id: str, card_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Submit every intent and write one ``execution`` record per intent.

        The taker-order guard runs again here, so a plan built by hand cannot slip a market order past
        the purpose rule. Nothing is submitted when the plan carries no intent.
        """
        reports: list[dict[str, Any]] = []
        for intent in plan.intents:
            assert_order_type_allowed(intent.order_type, intent.purpose, limits=self.execution_limits)
            if not self._supports(intent):
                # The REST API has no endpoint for this hook, so the strategy runs it in-process. No
                # execution record is written here: the order was never placed by this call.
                reports.append(
                    {
                        "cycle_id": cycle_id,
                        "action": intent.action.value,
                        "path": intent.path,
                        "freqtrade_call": intent.freqtrade_call,
                        "submitted": False,
                        "reason": "strategy hook: the bridge strategy runs it in-process",
                        "status": None,
                        "record_id": None,
                    }
                )
                continue
            report = self.transport.submit(intent)
            reports.append(
                self._record(
                    intent,
                    report,
                    cycle_id=cycle_id,
                    card_id=card_id or plan.decision_card_id,
                )
            )
        return reports

    def _supports(self, intent: OrderIntent) -> bool:
        """Whether the transport can place this intent itself."""
        supporter = getattr(self.transport, "supports", None)
        if callable(supporter):
            return bool(supporter(intent))
        return True

    def _record(
        self,
        intent: OrderIntent,
        report: Mapping[str, Any],
        *,
        cycle_id: str,
        card_id: str | None,
    ) -> dict[str, Any]:
        """Turn a venue report into an ``execution`` ledger record and return the row summary."""
        status = str(report.get("status") or "")
        if status not in STATUSES:
            status = STATUS_FILLED if report.get("fill_qty") else STATUS_UNFILLED_TIMEOUT
        fill_price = report.get("fill_price")
        fill_qty = report.get("fill_qty")
        fee_paid = self.fee_for(fill_price, fill_qty, intent.order_type)
        payload = ExecutionPayload(
            decision_card_id=card_id or "",
            order_type=intent.order_type,
            venue=VENUE_KRAKEN,
            fill_price=fill_price,
            fill_qty=fill_qty,
            fee_paid=fee_paid,
            reprice_count=int(report.get("reprice_count", 0) or 0),
            status=status,
        )
        row: dict[str, Any] = {
            "cycle_id": cycle_id,
            "submitted": True,
            "action": intent.action.value,
            "path": intent.path,
            "freqtrade_call": intent.freqtrade_call,
            "order_type": intent.order_type,
            "venue": payload.venue,
            "status": status,
            "fill_price": fill_price,
            "fill_qty": fill_qty,
            "fee_paid": fee_paid,
            "fee_pct": self.fee_pct(intent.order_type),
            "reprice_count": payload.reprice_count,
            "stake_amount": intent.stake_amount,
            "price": intent.price,
            "record_id": None,
        }
        if self.ledger is not None:
            row["record_id"] = self.ledger.write(
                Stage.EXECUTION, cycle_id, payload, producer_role="execution"
            )
        return row

    @staticmethod
    def _limit_price(price: float | None, offset_pct: float, *, side: str) -> float | None:
        """A post-only limit price: below the reference for a buy, above it for a sell."""
        if price is None:
            return None
        factor = 1.0 - offset_pct / 100.0 if side == "buy" else 1.0 + offset_pct / 100.0
        return round(float(price) * factor, 8)


@dataclass(frozen=True)
class _PlanRequest:
    """Everything the builders need, gathered once per plan."""

    cycle_id: str
    coin: str | None = None
    size_pct: float = 0.0
    price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    rotate_from: str | None = None
    emergency: bool = False
    resting_order: Mapping[str, Any] = field(default_factory=dict)
    rebalance_targets: Mapping[str, float] = field(default_factory=dict)
    time_stop_minutes: int | None = None
    slices: int | None = None
    current_stop: float | None = None
    trail_percent: float | None = None
    amount: float | None = None

    @property
    def pair(self) -> str | None:
        return pair_for(self.coin) if self.coin else None


__all__ = [
    "DEFAULT_MARKET_PURPOSES",
    "DEFAULT_MINIMAL_ROI",
    "EXECUTION_PATHS",
    "FREQTRADE_CALLS",
    "LADDER_OFFSETS_PCT",
    "NullTransport",
    "ORDER_TYPES",
    "OrderIntent",
    "OrderManager",
    "OrderTransport",
    "PARTIAL_EXIT_FRACTIONS",
    "REBALANCE_HOUR",
    "REBALANCE_WEEKDAY",
    "REJECTED_EXECUTION_ACTIONS",
    "RULE_MARKET_ORDER_NOT_ALLOWED",
    "RULE_MAX_ENTRY_ADJUSTMENT",
    "RULE_MAX_REPRICES",
    "RULE_REJECTED_ACTION",
    "RULE_UNKNOWN_ACTION",
    "STATUSES",
    "SIZE_FREE_PATHS",
    "UnsupportedActionError",
    "assert_order_type_allowed",
    "coerce_action",
    "freqtrade_dry_run_transport",
    "is_rejected_action",
    "ratchet_stop",
]
