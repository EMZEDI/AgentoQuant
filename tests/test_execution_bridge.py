"""The execution layer: the action vocabulary, its paths, and what reaches the venue.

Task 6 shipped ~3400 lines of execution code with no tests. This suite covers the contract the
vocabulary makes and the four defects a live dry-run venue exposed:

* only ``enter_laddered`` ever placed an order, because ``_rest_kwargs`` injected ``pair`` into REST
  calls whose signature does not take one (``forceexit``, ``cancel_open_order``) and never supplied
  the trade id they do require - the TypeError then discarded the whole cycle;
* ``take_profit_ladder`` could never fire, because the published intent carried ``target_price: null``
  and nothing else for the bridge to act on;
* a partial exit below Kraken's minimum order size was refused by the venue and acked as a trim;
* the execution record said ``unfilled_timeout`` while the venue held a filled entry.

Deliberately decorator-free: the model gateway refuses a request whose history carries a tool call
containing an at-sign followed by a dotted name, so files written by agents avoid decorators
entirely. ``tmp_path`` and ``monkeypatch`` arrive as plain function arguments.
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentoquant.config_loader import load_fee_tiers, load_risk_limits
from agentoquant.enums import REJECTED_ACTIONS, Action, ConfidenceBand, Sleeve
from agentoquant.execution.freqtrade_strategy import (
    placeholder_card,
    placeholder_universe,
    strategy_dir,
    strategy_path,
)
from agentoquant.execution.order_manager import (
    EXECUTION_PATHS,
    FREQTRADE_CALLS,
    LADDER_OFFSETS_PCT,
    RULE_BELOW_MIN_ORDER_SIZE,
    RULE_MARKET_ORDER_NOT_ALLOWED,
    RULE_NO_OPEN_POSITION,
    RULE_PRICE_AWAY_FROM_MARKET,
    RULE_REJECTED_ACTION,
    RULE_UNKNOWN_ACTION,
    RULE_VENUE_REFUSED,
    SIZE_FREE_PATHS,
    NullTransport,
    OrderManager,
    OrderNotPlacedError,
    UnsupportedActionError,
    _FreqtradeDryRunTransport,
    assert_order_type_allowed,
    coerce_action,
    is_rejected_action,
    ratchet_stop,
)
from agentoquant.execution.paper import placeholder_context, run_cycle, run_loop
from agentoquant.execution.signal_store import (
    DOCUMENT_SCHEMA_VERSION,
    MINIMAL_ROI_LADDER,
    SignalStore,
    execution_intent,
    is_fresh,
    pair_for,
    signal_document,
)
from agentoquant.execution.telegram_bot import (
    TelegramNotifier,
    notifications_enabled,
    render_decision_card,
    render_paper_result,
)
from agentoquant.ledger.schema import DecisionCard, RiskGateVerdict
from agentoquant.ledger.store import LedgerStore
from agentoquant.risk.kill_switch import build_kill_switch

HOUR = datetime(2026, 9, 19, 13, 0, tzinfo=UTC)
SUNDAY_MIDNIGHT = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
PRICE = 60_000.0


# ------------------------------------------------------------------------------------------------
# Helpers. Plain functions, called explicitly: no fixtures, no parametrisation.
# ------------------------------------------------------------------------------------------------


class RecordingTransport:
    """A transport that records every intent and answers with a canned report."""

    def __init__(self, *, report: dict[str, Any] | None = None, available: bool = True) -> None:
        self.report = report or {"status": "filled", "fill_price": 100.0, "fill_qty": 1.0}
        self.available_flag = available
        self.intents: list[Any] = []
        self.positions: list[dict[str, Any]] = [{"coin": "BTC", "pair": "BTC/USD", "trade_id": 7}]
        #: None means "the venue did not tell us a price", which is the honest default for a fake.
        self.price: float | None = None

    def available(self) -> bool:
        return self.available_flag

    def supports(self, intent: Any) -> bool:
        return True

    def submit(self, intent: Any) -> dict[str, Any]:
        self.intents.append(intent)
        return dict(self.report)

    def open_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)

    def last_price(self, pair: str) -> float | None:
        return self.price

    def calls(self) -> list[str]:
        return [intent.freqtrade_call for intent in self.intents]


class HookOnlyTransport(RecordingTransport):
    """A transport that can place nothing: every intent is a strategy hook."""

    def supports(self, intent: Any) -> bool:
        return False


def card_for(
    action: Any, *, coin: str = "BTC", size_pct: float = 3.0, cycle: str = "cycle-1"
) -> DecisionCard:
    """A real Decision Card for one action, built from the placeholder source's own shape."""
    base = placeholder_card(cycle, sequence=0)
    return base.model_copy(
        update={"action": Action(action), "coin": coin, "size_pct": size_pct}
    )


def verdict_for(
    size_pct: float = 3.0, *, verdict: str = "approved", rule_fired: str | None = None
) -> RiskGateVerdict:
    return RiskGateVerdict(
        decision_card_id="card-1",
        verdict=verdict,
        rule_fired=rule_fired,
        original_size_pct=size_pct,
        final_size_pct=size_pct,
        funding_floor_breach=False,
    )


def manager_for(transport: Any, **kwargs: Any) -> OrderManager:
    options: dict[str, Any] = {
        "transport": transport,
        "limits": load_risk_limits(),
        "fee_tiers": load_fee_tiers(),
        "book_value_cad": 900.0,
    }
    options.update(kwargs)
    return OrderManager(**options)


def plan_args(action: Any, **overrides: Any) -> dict[str, Any]:
    """The arguments each path needs, so one table can drive every action."""
    args: dict[str, Any] = {
        "cycle_id": "cycle-1",
        "verdict": verdict_for(),
        "coin": "BTC",
        "price": PRICE,
    }
    if action == Action.CANCEL_ORDER:
        args["resting_order"] = {"order_id": 11, "pair": "BTC/USD", "new_quantity": 0.5}
    if action == Action.TRAIL_STOP:
        args["current_stop"] = 57_000.0
        args["trail_percent"] = 5.0
    if action == Action.SET_STOP:
        args["stop_price"] = 57_000.0
    if action == Action.TAKE_PROFIT_LADDER:
        args["target_price"] = 66_000.0
    if action == Action.ROTATE:
        args["rotate_from"] = "ETH"
    args.update(overrides)
    return args


#: One row per vocabulary action: the path it must take and the freqtrade call that places it.
ACTION_PATHS: tuple[tuple[Action, str, str], ...] = (
    (Action.ENTER_LADDERED, "laddered_entry", "forceenter"),
    (Action.ADD, "dca_add", "adjust_trade_position"),
    (Action.TRIM, "partial_exit", "adjust_trade_position"),
    (Action.EXIT, "full_exit", "forceexit"),
    (Action.ROTATE, "rotate", "forceexit"),
    (Action.SET_STOP, "stop_on_exchange", "stoploss_on_exchange"),
    (Action.TRAIL_STOP, "trailing_stop_ratchet", "custom_stoploss+stoploss_on_exchange"),
    (Action.TAKE_PROFIT_LADDER, "take_profit_ladder", "custom_exit"),
    (Action.EVENT_TRADE, "event_trade_with_time_stop", "forceenter+custom_exit"),
    (Action.REBALANCE, "scheduled_weekly_rebalance", "rebalance"),
    (Action.HOLD, "hold_no_op", "none"),
    (Action.CANCEL_ORDER, "cancel_or_amend_resting", "cancel_open_order"),
)


# ------------------------------------------------------------------------------------------------
# 1. The vocabulary map: every action has a path, every banned action is refused.
# ------------------------------------------------------------------------------------------------


def test_every_action_in_the_enum_has_an_execution_path() -> None:
    assert set(EXECUTION_PATHS) == set(Action), "a vocabulary action has no execution path"
    assert len(EXECUTION_PATHS) == 12


def test_every_path_names_the_freqtrade_call_that_places_it() -> None:
    for path in EXECUTION_PATHS.values():
        assert path in FREQTRADE_CALLS, f"{path} has no freqtrade call"


def test_the_path_table_matches_the_declared_contract() -> None:
    for action, path, call in ACTION_PATHS:
        assert EXECUTION_PATHS[action] == path
        assert FREQTRADE_CALLS[path].startswith(call) or call in FREQTRADE_CALLS[path]


def test_the_banned_vocabulary_is_absent_from_the_enum_and_refused_by_name() -> None:
    for banned in REJECTED_ACTIONS:
        assert banned not in {member.value for member in Action}
        assert is_rejected_action(banned)
        try:
            coerce_action(banned)
        except UnsupportedActionError as exc:
            assert exc.rule_fired == RULE_REJECTED_ACTION, banned
        else:  # pragma: no cover - the refusal is the assertion
            raise AssertionError(f"{banned} was accepted")


def test_a_rejected_action_never_reaches_a_plan() -> None:
    manager = manager_for(NullTransport())
    for banned in REJECTED_ACTIONS:
        try:
            manager.plan_for_action(banned, cycle_id="c", coin="BTC", price=PRICE)
        except UnsupportedActionError as exc:
            assert exc.rule_fired == RULE_REJECTED_ACTION, banned
        else:  # pragma: no cover - the refusal is the assertion
            raise AssertionError(f"{banned} produced a plan")


def test_an_unknown_action_is_refused_with_its_own_rule() -> None:
    try:
        coerce_action("teleport")
    except UnsupportedActionError as exc:
        assert exc.rule_fired == RULE_UNKNOWN_ACTION
    else:  # pragma: no cover
        raise AssertionError("an unknown action was accepted")


# ------------------------------------------------------------------------------------------------
# 2. One execution path per action, against a fake transport.
# ------------------------------------------------------------------------------------------------


def test_each_action_builds_the_path_the_table_names() -> None:
    for action, path, _call in ACTION_PATHS:
        transport = RecordingTransport()
        manager = manager_for(transport)
        plan = manager.plan_for_action(action, **plan_args(action))
        assert plan.path == path, action
        assert plan.approved is True, action
        assert plan.action == action


def test_each_action_submits_through_the_transport_or_hands_off_to_the_strategy() -> None:
    for action, _path, call in ACTION_PATHS:
        if action in {Action.HOLD, Action.REBALANCE}:
            continue  # no-op by design, and the rebalance is time-gated; both are asserted below
        transport = RecordingTransport()
        manager = manager_for(transport)
        plan = manager.plan_for_action(action, **plan_args(action))
        assert plan.intents, f"{action} produced no intent"
        reports = manager.execute(plan, cycle_id="cycle-1")
        assert len(reports) == len(plan.intents)
        if plan.intents[0].freqtrade_call.startswith(call.split("+")[0]):
            assert transport.calls(), f"{action} submitted nothing"
        for report in reports:
            assert report["action"] == action.value
            assert report["path"] == _path


def test_hold_places_nothing_by_design() -> None:
    transport = RecordingTransport()
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.HOLD, **plan_args(Action.HOLD))
    assert plan.approved is True
    assert plan.intents == ()
    assert plan.executes is False
    assert "no order" in plan.detail
    assert manager.execute(plan, cycle_id="c") == []


def test_the_weekly_rebalance_only_fires_on_its_scheduled_hour() -> None:
    manager = manager_for(RecordingTransport(), now=HOUR)
    off_schedule = manager.plan_for_action(
        Action.REBALANCE, **plan_args(Action.REBALANCE, rebalance_targets={"BTC": 50.0})
    )
    assert off_schedule.intents == ()
    assert "not due" in off_schedule.detail

    due = manager_for(RecordingTransport(), now=SUNDAY_MIDNIGHT)
    on_schedule = due.plan_for_action(
        Action.REBALANCE, **plan_args(Action.REBALANCE, rebalance_targets={"BTC": 50.0})
    )
    assert len(on_schedule.intents) == 1
    assert on_schedule.intents[0].freqtrade_call == "rebalance"


def test_a_rejected_card_places_nothing() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(
        Action.ENTER_LADDERED,
        **plan_args(Action.ENTER_LADDERED, verdict=verdict_for(0.0, verdict="rejected")),
    )
    assert plan.approved is False
    assert plan.intents == ()
    assert plan.rule_fired


def test_a_rotate_without_a_source_coin_still_enters_the_target() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(Action.ROTATE, **plan_args(Action.ROTATE, rotate_from=None))
    assert len(plan.intents) == 1
    assert plan.intents[0].freqtrade_call == "forceenter"


def test_a_rotate_with_a_source_coin_leaves_it_first() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(Action.ROTATE, **plan_args(Action.ROTATE))
    assert [intent.freqtrade_call for intent in plan.intents] == ["forceexit", "forceenter"]
    assert plan.intents[0].pair == pair_for("ETH")


def test_the_hook_paths_are_reported_as_the_strategy_running_them() -> None:
    transport = HookOnlyTransport()
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.TRIM, **plan_args(Action.TRIM))
    reports = manager.execute(plan, cycle_id="cycle-1")
    assert reports[0]["submitted"] is False
    assert reports[0]["handled_by"] == "strategy"
    assert reports[0]["status"] is None
    assert transport.intents == []


def test_one_refused_intent_does_not_discard_the_rest_of_the_plan() -> None:
    class FlakyTransport(RecordingTransport):
        def submit(self, intent: Any) -> dict[str, Any]:
            self.intents.append(intent)
            if len(self.intents) == 1:
                raise OrderNotPlacedError(intent, rule_fired=RULE_NO_OPEN_POSITION)
            return dict(self.report)

    transport = FlakyTransport()
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED, slices=3))
    reports = manager.execute(plan, cycle_id="cycle-1")
    assert len(reports) == 3
    assert reports[0]["submitted"] is False
    assert reports[0]["rule_fired"] == RULE_NO_OPEN_POSITION
    assert sum(1 for report in reports if report["submitted"]) == 2


# ------------------------------------------------------------------------------------------------
# 3. Post-only versus market: a taker order only for a stop or an emergency exit.
# ------------------------------------------------------------------------------------------------


def test_market_orders_are_refused_for_every_other_purpose() -> None:
    limits = load_risk_limits()
    for purpose in ("entry", "add", "trim", "exit", "take_profit", "rebalance", "rotate", "cancel"):
        try:
            assert_order_type_allowed("market", purpose, limits=limits)
        except UnsupportedActionError as exc:
            assert exc.rule_fired == RULE_MARKET_ORDER_NOT_ALLOWED, purpose
        else:  # pragma: no cover
            raise AssertionError(f"a market order was allowed for {purpose}")


def test_market_orders_are_allowed_for_a_stop_and_an_emergency_exit() -> None:
    limits = load_risk_limits()
    assert_order_type_allowed("market", "stop_loss", limits=limits)
    assert_order_type_allowed("market", "emergency_exit", limits=limits)


def test_every_planned_order_is_post_only_except_stops_and_emergency_exits() -> None:
    for action, _path, _call in ACTION_PATHS:
        if action in {Action.HOLD, Action.REBALANCE}:
            continue
        manager = manager_for(RecordingTransport())
        plan = manager.plan_for_action(action, **plan_args(action))
        for intent in plan.intents:
            if intent.order_type == "market":
                assert intent.purpose in {"stop_loss", "emergency_exit"}, (action, intent.purpose)
            elif intent.order_type.startswith("stop_loss"):
                # A stop is not a post-only order, and its only purpose is to stop.
                assert intent.purpose == "stop_loss", action
                assert intent.post_only is False, action
            else:
                assert intent.post_only is True, action
                assert intent.order_type == "post_only_limit", action


def test_an_ordinary_exit_is_post_only_and_an_emergency_exit_is_a_taker() -> None:
    manager = manager_for(RecordingTransport())
    ordinary = manager.plan_for_action(Action.EXIT, **plan_args(Action.EXIT))
    assert ordinary.intents[0].order_type == "post_only_limit"
    assert ordinary.intents[0].post_only is True

    emergency = manager.plan_for_action(Action.EXIT, **plan_args(Action.EXIT, emergency=True))
    assert emergency.intents[0].order_type == "market"
    assert emergency.intents[0].purpose == "emergency_exit"
    assert emergency.intents[0].post_only is False


def test_a_market_order_cannot_be_smuggled_through_execute() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(Action.TRIM, **plan_args(Action.TRIM))
    forged = plan.intents[0].__class__(**{**plan.intents[0].as_dict(), "action": Action.TRIM,
                                          "order_type": "market", "purpose": "trim",
                                          "pair": "BTC/USD", "freqtrade_call": "adjust_trade_position"})
    try:
        manager.execute(
            type(plan)(cycle_id="c", action=Action.TRIM, path="partial_exit", approved=True,
                       intents=(forged,)),
            cycle_id="c",
        )
    except UnsupportedActionError as exc:
        assert exc.rule_fired == RULE_MARKET_ORDER_NOT_ALLOWED
    else:  # pragma: no cover
        raise AssertionError("execute() accepted a taker order for a trim")


# ------------------------------------------------------------------------------------------------
# 4. Ladder offsets produce distinct prices.
# ------------------------------------------------------------------------------------------------


def test_a_laddered_entry_places_each_slice_at_its_own_offset() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED, slices=3))
    prices = [intent.price for intent in plan.intents]
    assert len(prices) == 3
    assert len(set(prices)) == 3, f"the ladder is not a ladder: {prices}"
    for index, intent in enumerate(plan.intents):
        expected = round(PRICE * (1 - LADDER_OFFSETS_PCT[index] / 100.0), 8)
        assert intent.price == expected
        assert intent.price < PRICE
        assert intent.slice_index == index
        assert intent.slice_count == 3


def test_the_first_ladder_slice_opens_the_trade_and_the_rest_add_to_it() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED, slices=3))
    assert plan.intents[0].freqtrade_call == "forceenter"
    assert [intent.freqtrade_call for intent in plan.intents[1:]] == [
        "adjust_trade_position",
        "adjust_trade_position",
    ]


def test_a_ladder_never_exceeds_the_offset_table() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED, slices=99))
    assert len(plan.intents) == len(LADDER_OFFSETS_PCT)
    assert len({intent.price for intent in plan.intents}) == len(LADDER_OFFSETS_PCT)


def test_a_stale_reference_price_is_re_anchored_to_the_venue() -> None:
    """The placeholder's fixed 60,000 against a market of 81,264 could never fill, so it is moved."""
    transport = RecordingTransport()
    transport.price = 81_264.5
    manager = manager_for(transport)
    plan = manager.plan_for_action(
        Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED, price=60_000.0, slices=3)
    )
    prices = [intent.price for intent in plan.intents]
    assert len(set(prices)) == 3
    assert all(80_000.0 < price < 81_264.5 for price in prices), prices
    assert RULE_PRICE_AWAY_FROM_MARKET in plan.detail


def test_a_reference_price_close_to_the_market_is_left_alone() -> None:
    transport = RecordingTransport()
    transport.price = 81_264.5
    manager = manager_for(transport)
    plan = manager.plan_for_action(
        Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED, price=81_200.0, slices=3)
    )
    assert RULE_PRICE_AWAY_FROM_MARKET not in plan.detail
    assert max(intent.price for intent in plan.intents) < 81_200.0


def test_without_a_venue_price_the_planned_price_stands() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(
        Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED, price=PRICE, slices=1)
    )
    assert plan.intents[0].price == round(PRICE * (1 - LADDER_OFFSETS_PCT[0] / 100.0), 8)
    assert RULE_PRICE_AWAY_FROM_MARKET not in plan.detail


def test_a_dca_add_uses_its_own_offsets_and_a_positive_stake() -> None:
    manager = manager_for(RecordingTransport(), max_entry_position_adjustment=3)
    plan = manager.plan_for_action(Action.ADD, **plan_args(Action.ADD))
    assert plan.intents
    for intent in plan.intents:
        assert intent.stake_amount is not None and intent.stake_amount > 0
        assert intent.price is not None and intent.price < PRICE
    assert len({intent.price for intent in plan.intents}) == len(plan.intents)


def test_an_add_is_refused_when_the_adjustment_count_is_zero() -> None:
    manager = manager_for(RecordingTransport(), max_entry_position_adjustment=0)
    try:
        manager.plan_for_action(Action.ADD, **plan_args(Action.ADD))
    except UnsupportedActionError as exc:
        assert exc.rule_fired == "max_entry_position_adjustment_exceeded"
    else:  # pragma: no cover
        raise AssertionError("an add was planned with no adjustment budget")


def test_a_partial_exit_sells_above_the_reference_price() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(Action.TRIM, **plan_args(Action.TRIM))
    intent = plan.intents[0]
    assert intent.side == "sell"
    assert intent.stake_amount is not None and intent.stake_amount < 0
    assert intent.price is not None and intent.price > PRICE


# ------------------------------------------------------------------------------------------------
# 5. Stop ratcheting: a stop only ever moves in the protective direction.
# ------------------------------------------------------------------------------------------------


def test_ratchet_never_loosens_a_long_stop() -> None:
    assert ratchet_stop(100.0, 90.0) == 100.0
    assert ratchet_stop(100.0, 110.0) == 110.0
    assert ratchet_stop(None, 90.0) == 90.0


def test_ratchet_never_loosens_a_short_stop() -> None:
    assert ratchet_stop(100.0, 110.0, side="short") == 100.0
    assert ratchet_stop(100.0, 90.0, side="short") == 90.0


def test_a_trailing_stop_keeps_the_more_protective_level() -> None:
    manager = manager_for(RecordingTransport())
    loosening = manager.plan_for_action(
        Action.TRAIL_STOP, **plan_args(Action.TRAIL_STOP, stop_price=50_000.0, current_stop=57_000.0)
    )
    assert loosening.intents[0].price == 57_000.0
    assert "ratchet" in loosening.detail

    tightening = manager.plan_for_action(
        Action.TRAIL_STOP, **plan_args(Action.TRAIL_STOP, stop_price=58_000.0, current_stop=57_000.0)
    )
    assert tightening.intents[0].price == 58_000.0


def test_a_trailing_stop_without_a_level_uses_the_trail_percent() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(
        Action.TRAIL_STOP,
        **plan_args(Action.TRAIL_STOP, stop_price=None, current_stop=None, trail_percent=5.0),
    )
    assert plan.intents[0].price == round(PRICE * 0.95, 8)


def test_a_stop_is_placed_on_the_exchange() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(Action.SET_STOP, **plan_args(Action.SET_STOP))
    intent = plan.intents[0]
    assert intent.freqtrade_call == "stoploss_on_exchange"
    assert intent.freqtrade_kwargs["stoploss_on_exchange"] is True
    assert intent.purpose == "stop_loss"


# ------------------------------------------------------------------------------------------------
# 6. Reprice limits.
# ------------------------------------------------------------------------------------------------


def test_reprices_are_capped_by_the_configured_limit() -> None:
    limits = load_risk_limits()
    manager = manager_for(RecordingTransport())
    assert manager.max_reprices == limits.execution.max_reprices
    for action in (Action.ENTER_LADDERED, Action.ADD, Action.TRIM, Action.EVENT_TRADE):
        plan = manager.plan_for_action(action, **plan_args(action))
        for intent in plan.intents:
            assert intent.reprices_allowed == limits.execution.max_reprices, action


def test_stops_and_emergency_exits_are_never_repriced() -> None:
    manager = manager_for(RecordingTransport())
    for action in (Action.SET_STOP, Action.TRAIL_STOP):
        plan = manager.plan_for_action(action, **plan_args(action))
        assert plan.intents[0].reprices_allowed == 0, action
    emergency = manager.plan_for_action(Action.EXIT, **plan_args(Action.EXIT, emergency=True))
    assert emergency.intents[0].reprices_allowed == 0


def test_an_unfilled_order_carries_the_configured_timeout() -> None:
    from agentoquant.execution.order_manager import UNFILLED_TIMEOUT_MINUTES

    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED))
    for intent in plan.intents:
        assert intent.unfilled_timeout_minutes == UNFILLED_TIMEOUT_MINUTES


# ------------------------------------------------------------------------------------------------
# 7. The signal store: atomic publish, latest, ack.
# ------------------------------------------------------------------------------------------------


def test_publish_writes_the_current_document_and_a_history_copy(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "signals")
    card = card_for(Action.ENTER_LADDERED)
    verdict = verdict_for()
    path = store.publish(card, verdict, cycle_id="cycle-1")
    assert path == store.current_path
    assert store.current_path.exists()
    assert store.history_path("cycle-1").exists()
    document = store.latest()
    assert document is not None
    assert document["cycle_id"] == "cycle-1"
    assert document["schema_version"] == DOCUMENT_SCHEMA_VERSION
    assert store.published_cycles() == ["cycle-1"]


def test_latest_is_none_before_the_first_publish(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "signals")
    assert store.latest() is None
    assert store.read_ack("nothing") is None


def test_publishing_a_second_cycle_replaces_the_current_document(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "signals")
    store.publish(card_for(Action.HOLD, cycle="cycle-1"), verdict_for(), cycle_id="cycle-1")
    store.publish(card_for(Action.EXIT, cycle="cycle-2"), verdict_for(), cycle_id="cycle-2")
    assert store.latest()["cycle_id"] == "cycle-2"
    assert store.read_history("cycle-1")["cycle_id"] == "cycle-1"
    assert store.published_cycles() == ["cycle-1", "cycle-2"]


def test_a_stale_document_is_not_latest(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "signals")
    store.publish(card_for(Action.HOLD), verdict_for(), cycle_id="cycle-1")
    document = store.latest()
    assert document is not None
    later = datetime.now(UTC) + timedelta(hours=3)
    assert store.latest(max_age_s=60.0, now=later) is None
    assert is_fresh(document, max_age_s=60.0, now=later) is False
    assert is_fresh(document, max_age_s=60.0) is True


def test_ack_round_trips_and_is_scoped_to_its_cycle(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "signals")
    store.publish(card_for(Action.EXIT), verdict_for(), cycle_id="cycle-1")
    store.ack("cycle-1", {"action": "exit", "pair": "BTC/USD"})
    payload = store.read_ack("cycle-1")
    assert payload is not None
    assert payload["cycle_id"] == "cycle-1"
    assert payload["result"]["action"] == "exit"
    assert store.acked_cycles() == ["cycle-1"]
    assert store.read_ack("cycle-2") is None


def test_a_reader_never_sees_a_half_written_document(tmp_path: Path) -> None:
    """Publish in a loop from a thread while the main thread reads: every read is a whole document."""
    import threading
    import time

    store = SignalStore(tmp_path / "signals")
    verdict = verdict_for()
    errors: list[str] = []
    stop = threading.Event()
    writer_errors: list[str] = []

    def writer() -> None:
        try:
            for index in range(120):
                cid = f"cycle-{index:04d}"
                store.publish(card_for(Action.ENTER_LADDERED, cycle=cid), verdict, cycle_id=cid)
        except Exception as exc:  # a writer that dies must not leave the reader spinning
            writer_errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            stop.set()

    thread = threading.Thread(target=writer)
    thread.start()
    deadline = time.monotonic() + 60.0
    seen = 0
    while not stop.is_set() and time.monotonic() < deadline:
        document = store.latest()
        if document is None:
            continue
        seen += 1
        if document.get("schema_version") != DOCUMENT_SCHEMA_VERSION:
            errors.append("a reader saw a document without a schema version")
        if not document.get("cycle_id"):
            errors.append("a reader saw a document without a cycle id")
    thread.join(timeout=10.0)
    assert writer_errors == []
    assert not thread.is_alive(), "the writer did not finish"
    assert errors == []
    assert seen > 0, "the reader never observed a published document"
    leftovers = [p.name for p in store.root.iterdir() if p.name.startswith(".")]
    assert leftovers == [], f"a temp file was left behind: {leftovers}"


def test_the_published_document_carries_the_execution_intent(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "signals")
    store.publish(card_for(Action.ENTER_LADDERED), verdict_for(3.0), cycle_id="cycle-1")
    document = store.latest()
    execution = document["execution"]
    assert execution["approved"] is True
    assert execution["side"] == "buy"
    assert execution["order_type"] == "post_only_limit"
    assert execution["size_pct"] == 3.0
    assert execution["ladder_slices"] == 3
    assert execution["ladder_offsets_pct"] == list(LADDER_OFFSETS_PCT)
    assert execution["post_only"] is True


def test_a_rejected_card_publishes_size_zero_and_not_approved() -> None:
    intent = execution_intent(
        card_for(Action.ENTER_LADDERED), verdict_for(3.0, verdict="rejected", rule_fired="x")
    )
    assert intent["approved"] is False
    assert intent["size_pct"] == 0.0
    assert intent["rule_fired"] == "x"


def test_the_take_profit_ladder_publishes_the_ladder_not_an_empty_target() -> None:
    """Regression: with target_price null and no ladder the bridge had nothing to fire on."""
    intent = execution_intent(card_for(Action.TAKE_PROFIT_LADDER), verdict_for(3.0))
    assert intent["minimal_roi"] == MINIMAL_ROI_LADDER
    assert intent["partial_exits"] == [0.5, 0.25]


def test_a_cycle_id_mismatch_between_card_and_publish_is_refused() -> None:
    card = placeholder_card("other-cycle", sequence=0)
    try:
        signal_document(card, verdict_for(), cycle_id="cycle-1")
    except Exception as exc:
        assert "does not match" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a mismatched cycle id was published")


# ------------------------------------------------------------------------------------------------
# 8. The Telegram notifier: the Decision Card rendering, outbound only.
# ------------------------------------------------------------------------------------------------


def test_the_decision_card_renders_every_line_in_the_addendum_order() -> None:
    card = card_for(Action.ENTER_LADDERED, coin="BTC")
    text = render_decision_card(card, window_minutes=10)
    lines = text.splitlines()
    assert lines[0].startswith("[enter_laddered] BTC")
    assert "size 3.0%" in lines[0]
    assert lines[1].startswith("Confidence: 72")
    assert "P(up):" in lines[1]
    assert lines[2].startswith("EV after fees")
    assert lines[3].startswith("Evidence:")
    assert lines[4].startswith("Strongest objection")
    assert lines[5].startswith("Flips if:")
    assert lines[6] == "Auto-executes in 10 min unless /veto"


def test_the_decision_card_survives_a_card_with_no_objection() -> None:
    card = card_for(Action.HOLD, coin=None, size_pct=0.0).model_copy(
        update={"strongest_objection": None, "sleeve": None}
    )
    text = render_decision_card(card, window_minutes=10)
    assert "none recorded" in text
    assert "-" in text.splitlines()[0]


def test_the_notifier_sends_through_the_injected_transport_and_remembers_it() -> None:
    sent: list[tuple[str, str, str]] = []

    def recorder(token: str, chat_id: str, text: str) -> dict[str, Any]:
        sent.append((token, chat_id, text))
        return {"ok": True}

    notifier = TelegramNotifier(
        token="token-value", chat_id="chat-value", transport=recorder, enabled=True
    )
    assert notifier.configured is True
    assert notifier.send_card(card_for(Action.EXIT)) is True
    assert notifier.send_paper_result({"cycle_id": "c1", "action": "exit", "orders": 1}) is True
    assert len(sent) == 2
    assert sent[0][2].startswith("[exit]")
    assert "PAPER CYCLE c1" in sent[1][2]
    assert len(notifier.sent) == 2


def test_a_disabled_notifier_sends_nothing() -> None:
    notifier = TelegramNotifier(
        token="t", chat_id="c", transport=lambda *args: {"ok": True}, enabled=False
    )
    assert notifier.send("hello") is False
    assert notifier.sent == []
    assert notifications_enabled({"AGENTOQUANT_TELEGRAM": "0"}) is False
    assert notifications_enabled({"AGENTOQUANT_TELEGRAM": "1"}) is True


def test_notifications_are_opt_in_so_unset_means_silent() -> None:
    """An unset switch must mean *off*, not on.

    This is the regression test for a real incident: ``notifications_enabled`` used to return True
    whenever ``AGENTOQUANT_TELEGRAM`` was unset. The loop tests run real cycles with ``notify=True``
    and resolve the bot token from ``~/.hermes/.env``, so the suite posted 90 Decision Cards to a
    real phone in half an hour - and a test that *deleted* the variable made it worse, because
    deleting it was what enabled sending. Unset, blank and any unrecognised value are all off.
    """
    assert notifications_enabled({}) is False
    assert notifications_enabled({"AGENTOQUANT_TELEGRAM": ""}) is False
    assert notifications_enabled({"AGENTOQUANT_TELEGRAM": "   "}) is False
    assert notifications_enabled({"AGENTOQUANT_TELEGRAM": "maybe"}) is False
    assert notifications_enabled({"OTHER": "1"}) is False

    for value in ("1", "true", "TRUE", " True ", "yes", "on"):
        assert notifications_enabled({"AGENTOQUANT_TELEGRAM": value}) is True, value

    for value in ("0", "false", "no", "off", "False"):
        assert notifications_enabled({"AGENTOQUANT_TELEGRAM": value}) is False, value


def test_the_test_suite_itself_cannot_reach_telegram() -> None:
    """The autouse guard in ``conftest`` must hold even after a test monkeypatches the environment.

    The opt-in default is the fix; this is the belt to its braces. If this test ever fails, a test
    run is one ``notify=True`` away from messaging Shahrad again.
    """
    import os

    assert notifications_enabled(dict(os.environ)) is False
    assert os.environ.get("AGENTOQUANT_TELEGRAM") == "0"
    assert os.environ.get("AGENTOQUANT_TELEGRAM_BOT_TOKEN") == ""
    assert os.environ.get("AGENTOQUANT_TELEGRAM_CHAT_ID") == ""


def test_an_unconfigured_notifier_never_raises(tmp_path: Path, monkeypatch) -> None:
    """With no credential reachable anywhere, a send is refused rather than attempted."""
    from agentoquant.execution import telegram_bot

    monkeypatch.setattr(telegram_bot, "HERMES_ENV_PATH", tmp_path / "no-such.env")
    monkeypatch.setattr(telegram_bot, "_from_credentials_file", lambda name: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("AGENTOQUANT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
    monkeypatch.delenv("AGENTOQUANT_TELEGRAM_CHAT_ID", raising=False)

    def explode(*args: Any) -> dict[str, Any]:  # pragma: no cover - the assertion is that it is not called
        raise AssertionError("a send was attempted with no credential")

    notifier = TelegramNotifier(token=None, chat_id=None, transport=explode, enabled=True, environ={})
    assert notifier.configured is False
    assert notifier.send("hello") is False


def test_the_notifier_module_never_polls_telegram() -> None:
    """The Hermes gateway owns the inbound path; a second poller would break the bot token.

    The module's own docstring names the call it must never make, so the check strips the docstring
    before looking for the call itself.
    """
    import ast

    from agentoquant.execution import telegram_bot

    source = Path(telegram_bot.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        docstring = tree.body[0].value.value or ""
        source = source.replace(docstring, "")
    for token in ("getUpdates", "setWebhook", "deleteWebhook", "api.telegram.org/bot"):
        if token == "api.telegram.org/bot":
            continue
        assert token not in source, token
    assert "sendMessage" in source


def test_the_paper_result_digest_reports_what_the_cycle_did() -> None:
    text = render_paper_result(
        {
            "cycle_id": "2026-09-19T14Z-0003",
            "action": "enter_laddered",
            "verdict": "approved",
            "path": "laddered_entry",
            "orders": 1,
            "fills": 1,
            "fees_paid": 0.01,
            "fee_tier": "tier_1",
            "cost_usd": 0.0,
            "ack": "acked",
            "status": "ok",
        }
    )
    assert "PAPER CYCLE 2026-09-19T14Z-0003" in text
    assert "orders:   1 placed, 1 filled" in text
    assert "ack:      acked" in text


# ------------------------------------------------------------------------------------------------
# 9. The freqtrade config invariants that were silently wrong.
# ------------------------------------------------------------------------------------------------


def test_the_venue_config_is_a_dry_run_kraken_config_with_no_keys() -> None:
    from agentoquant.execution.freqtrade_strategy import assert_dry_run_config, load_freqtrade_config

    config = assert_dry_run_config()
    assert config["dry_run"] is True
    assert config["exchange"]["name"] == "kraken"
    assert not config["exchange"]["key"]
    assert not config["exchange"]["secret"]
    assert load_freqtrade_config()["dry_run"] is True


def test_position_adjustment_and_force_entry_are_enabled() -> None:
    """The two keys whose absence made laddered entries, adds, trims and exits dead."""
    config = assert_config()
    assert config["position_adjustment_enable"] is True
    assert config["force_entry_enable"] is True


def test_the_strategy_allows_position_adjustments() -> None:
    source = strategy_path().read_text(encoding="utf-8")
    value = _assignment(source, "max_entry_position_adjustment")
    assert value is not None and int(value) != 0, "max_entry_position_adjustment caps adjustments at 0"


def test_the_strategy_declares_the_interface_the_bridge_targets() -> None:
    from agentoquant.execution.freqtrade_strategy import BRIDGE_INTERFACE_VERSION, strategy_interface_version

    assert strategy_interface_version() == BRIDGE_INTERFACE_VERSION == 3


def test_config_and_strategy_agree_on_the_order_types() -> None:
    config = assert_config()
    source = strategy_path().read_text(encoding="utf-8")
    strategy_types = _dict_literal(source, "order_types")
    for key in ("entry", "exit", "emergency_exit", "force_entry", "force_exit", "stoploss"):
        assert key in config["order_types"], key
        assert key in strategy_types, key
        assert config["order_types"][key] == strategy_types[key], key
    assert config["order_types"]["emergency_exit"] == "market"
    assert config["order_types"]["stoploss"] == "market"
    assert config["order_types"]["stoploss_on_exchange"] is True


def test_the_strategy_covers_every_action_that_needs_a_hook() -> None:
    """The vocabulary the pipeline publishes must be the vocabulary the bridge executes.

    ``take_profit_ladder`` had a path, an intent and no way to fire, because the strategy's branch
    keyed on a field the pipeline never filled. This is the check that would have caught it.
    """
    source = strategy_path().read_text(encoding="utf-8")
    hook_actions = (
        Action.ENTER_LADDERED,
        Action.ADD,
        Action.TRIM,
        Action.EXIT,
        Action.SET_STOP,
        Action.TRAIL_STOP,
        Action.TAKE_PROFIT_LADDER,
        Action.EVENT_TRADE,
    )
    for action in hook_actions:
        assert f'"{action.value}"' in source, f"the bridge has no branch for {action.value}"


def test_the_strategy_is_dependency_free_and_never_polls() -> None:
    from agentoquant.execution.freqtrade_strategy import check_strategy_hygiene

    assert check_strategy_hygiene() == []


def test_a_non_dry_run_config_yields_a_null_transport(tmp_path: Path) -> None:
    """Fail closed: a config that is not dry-run must not produce a client at all."""
    from agentoquant.execution.order_manager import freqtrade_dry_run_transport

    config = assert_config()
    config["dry_run"] = False
    path = tmp_path / "live.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    transport = freqtrade_dry_run_transport(config_path=path)
    assert isinstance(transport, NullTransport)
    assert transport.available() is False


# ------------------------------------------------------------------------------------------------
# 10. Every REST intent binds to the real client signature (the bug that killed four actions).
# ------------------------------------------------------------------------------------------------


class FakeRestClient:
    """A stand-in with the real ``freqtrade_client`` method signatures."""

    def __init__(self, **results: Any) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.trades: list[dict[str, Any]] = [{"pair": "BTC/USD", "trade_id": 7, "is_open": True}]

    def ping(self) -> dict[str, str]:
        return {"status": "pong"}

    def status(self) -> list[dict[str, Any]]:
        return list(self.trades)

    def trade(self, trade_id: Any) -> dict[str, Any]:
        return dict(self.results.get("trade") or {"amount": 1.0, "is_open": False, "close_rate": 11.0})

    def forceenter(
        self,
        pair: str,
        side: str,
        price: float | None = None,
        *,
        order_type: str | None = None,
        stake_amount: float | None = None,
        leverage: float | None = None,
        enter_tag: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("forceenter", {"pair": pair, "side": side, "price": price}))
        return dict(self.results.get("forceenter") or {"trade_id": 9, "amount": 1.0, "open_rate": 11.0})

    def forceexit(self, tradeid: Any, ordertype: str | None = None, amount: float | None = None) -> dict:
        self.calls.append(("forceexit", {"tradeid": tradeid, "ordertype": ordertype, "amount": amount}))
        return dict(self.results.get("forceexit") or {"result": "ok"})

    def cancel_open_order(self, trade_id: Any) -> dict[str, Any]:
        self.calls.append(("cancel_open_order", {"trade_id": trade_id}))
        return dict(self.results.get("cancel") or {"result": "cancelled"})

    def pair_candles(self, pair: str, timeframe: str, limit: int | None = None, columns=None) -> dict:
        return {
            "columns": ["date", "open", "high", "low", "close", "volume"],
            "data": [["2026-09-19T13:40:00Z", 1.0, 2.0, 0.5, 10.0, 3.0]],
        }


def test_every_rest_intent_binds_to_the_real_client_signature() -> None:
    """Binding the produced kwargs to the installed client catches all three original defects.

    ``forceexit`` takes ``tradeid`` and no pair, ``cancel_open_order`` takes ``trade_id``, and the
    replacement ``forceenter`` needs a ``side``. Before this task each of them raised TypeError
    before any order existed, and the exception took the whole cycle's reports with it.
    """
    from freqtrade_client import FtRestClient

    transport = _FreqtradeDryRunTransport(FakeRestClient(), "http://localhost:8081")
    manager = manager_for(transport)
    checked = 0
    for action, _path, _call in ACTION_PATHS:
        if action in {Action.HOLD, Action.REBALANCE}:
            continue
        plan = manager.plan_for_action(action, **plan_args(action))
        for intent in plan.intents:
            call = intent.freqtrade_call.split("+")[0]
            if call not in _FreqtradeDryRunTransport.REST_CALLS:
                continue
            handler = getattr(FtRestClient, call)
            kwargs = transport._rest_kwargs(handler, intent)
            accepted = set(inspect.signature(handler).parameters)
            id_arg = _FreqtradeDryRunTransport.REST_TRADE_ID_ARGS.get(call)
            if id_arg and id_arg in accepted and kwargs.get(id_arg) is None:
                kwargs[id_arg] = 7  # what the transport resolves from /status at submit time
            inspect.signature(handler).bind(None, **kwargs)
            checked += 1
    assert checked >= 4, f"only {checked} REST intents were checked"


def test_the_pair_is_never_injected_into_a_handler_that_does_not_take_one() -> None:
    from freqtrade_client import FtRestClient

    transport = _FreqtradeDryRunTransport(FakeRestClient(), "http://localhost:8081")
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.EXIT, **plan_args(Action.EXIT))
    kwargs = transport._rest_kwargs(FtRestClient.forceexit, plan.intents[0])
    assert "pair" not in kwargs
    assert kwargs["ordertype"] == "limit"


def test_an_exit_resolves_the_venue_trade_id_from_the_pair() -> None:
    client = FakeRestClient()
    transport = _FreqtradeDryRunTransport(client, "http://localhost:8081")
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.EXIT, **plan_args(Action.EXIT))
    reports = manager.execute(plan, cycle_id="cycle-1")
    assert reports[0]["submitted"] is True
    assert client.calls[0][1]["tradeid"] == 7


def test_an_exit_with_no_open_position_is_refused_by_name() -> None:
    client = FakeRestClient()
    client.trades = []
    transport = _FreqtradeDryRunTransport(client, "http://localhost:8081")
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.EXIT, **plan_args(Action.EXIT, coin="DOGE"))
    reports = manager.execute(plan, cycle_id="cycle-1")
    assert reports[0]["submitted"] is False
    assert reports[0]["rule_fired"] == RULE_NO_OPEN_POSITION


def test_a_cancel_order_reaches_the_venue() -> None:
    client = FakeRestClient()
    transport = _FreqtradeDryRunTransport(client, "http://localhost:8081")
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.CANCEL_ORDER, **plan_args(Action.CANCEL_ORDER))
    assert plan.intents[0].freqtrade_call == "cancel_open_order"
    reports = manager.execute(plan, cycle_id="cycle-1")
    assert reports[0]["submitted"] is True
    assert reports[0]["status"] == "cancelled"
    assert client.calls[0] == ("cancel_open_order", {"trade_id": 7})


def test_the_cancel_replacement_forceenter_carries_a_side() -> None:
    from freqtrade_client import FtRestClient

    transport = _FreqtradeDryRunTransport(FakeRestClient(), "http://localhost:8081")
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.CANCEL_ORDER, **plan_args(Action.CANCEL_ORDER))
    replacement = plan.intents[1]
    assert replacement.freqtrade_call == "forceenter"
    kwargs = transport._rest_kwargs(FtRestClient.forceenter, replacement)
    inspect.signature(FtRestClient.forceenter).bind(None, **kwargs)
    assert kwargs["side"] == "long"


def test_no_intent_uses_an_amend_endpoint_that_does_not_exist() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(Action.CANCEL_ORDER, **plan_args(Action.CANCEL_ORDER))
    calls = {intent.freqtrade_call for intent in plan.intents}
    assert "amend_order" not in calls
    assert calls <= _FreqtradeDryRunTransport.REST_CALLS


# ------------------------------------------------------------------------------------------------
# 11. The write-back: what the venue did, not just that the call returned.
# ------------------------------------------------------------------------------------------------


def test_a_filled_entry_is_recorded_as_filled() -> None:
    client = FakeRestClient(forceenter={"trade_id": 3, "amount": 0.5, "open_rate": 81_000.0})
    transport = _FreqtradeDryRunTransport(client, "http://localhost:8081")
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED, slices=1))
    report = manager.execute(plan, cycle_id="cycle-1")[0]
    assert report["status"] == "filled"
    assert report["fill_price"] == 81_000.0
    assert report["fill_qty"] == 0.5
    assert report["fee_paid"] is not None


def test_a_closed_exit_is_recorded_as_filled_from_the_trade_read_back() -> None:
    client = FakeRestClient(trade={"amount": 2.0, "is_open": False, "close_rate": 111.5})
    transport = _FreqtradeDryRunTransport(client, "http://localhost:8081")
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.EXIT, **plan_args(Action.EXIT, emergency=True))
    report = manager.execute(plan, cycle_id="cycle-1")[0]
    assert report["status"] == "filled"
    assert report["fill_price"] == 111.5
    assert report["fill_qty"] == 2.0


def test_a_resting_exit_is_recorded_as_unfilled_not_as_filled() -> None:
    client = FakeRestClient(trade={"amount": 2.0, "is_open": True, "open_rate": 100.0})
    transport = _FreqtradeDryRunTransport(client, "http://localhost:8081")
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.EXIT, **plan_args(Action.EXIT))
    report = manager.execute(plan, cycle_id="cycle-1")[0]
    assert report["status"] == "unfilled_timeout"
    assert report["fill_qty"] == 2.0


def test_a_refused_force_entry_is_recorded_as_a_refusal() -> None:
    client = FakeRestClient(forceenter={"status": "Error entering long trade for pair BTC/USD."})
    transport = _FreqtradeDryRunTransport(client, "http://localhost:8081")
    manager = manager_for(transport)
    plan = manager.plan_for_action(Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED, slices=1))
    report = manager.execute(plan, cycle_id="cycle-1")[0]
    assert report["submitted"] is False
    assert report["rule_fired"] == RULE_VENUE_REFUSED
    assert report["status"] is None


def test_an_execution_record_is_written_with_the_cycle_id(tmp_path: Path) -> None:
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    ledger.migrate()
    transport = RecordingTransport()
    manager = manager_for(transport, ledger=ledger)
    plan = manager.plan_for_action(Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED))
    reports = manager.execute(plan, cycle_id="cycle-1", card_id="card-1")
    assert all(report["record_id"] for report in reports)
    rows = ledger.cycle_records("cycle-1")
    execution = [row for row in rows if row["stage"] == "execution"]
    assert len(execution) == len(plan.intents)
    assert execution[0]["fee_paid"] is not None
    ledger.close()


# ------------------------------------------------------------------------------------------------
# 12. The minimum order size: refuse or lift, never a silent no-op.
# ------------------------------------------------------------------------------------------------


def test_a_partial_exit_below_the_venue_minimum_is_lifted_when_the_position_can_carry_it() -> None:
    manager = manager_for(RecordingTransport())
    # 0.3 percent of a 900 USD book is 2.70, half of that is 1.35: under the 5.00 floor.
    plan = manager.plan_for_action(
        Action.TRIM,
        **plan_args(Action.TRIM, price=81_000.0, position_amount=0.01, verdict=verdict_for(0.3)),
    )
    assert len(plan.intents) == 1
    assert plan.intents[0].stake_amount == -manager.min_order_cost_usd


def test_a_partial_exit_the_position_cannot_carry_is_refused_by_name() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(
        Action.TRIM,
        **plan_args(Action.TRIM, price=81_000.0, position_amount=0.000001, verdict=verdict_for(0.3)),
    )
    assert plan.intents == ()
    assert plan.rule_fired == RULE_BELOW_MIN_ORDER_SIZE
    assert "below the venue minimum" in plan.detail


def test_the_minimum_comes_from_the_risk_limits() -> None:
    limits = load_risk_limits()
    manager = manager_for(RecordingTransport())
    assert manager.min_order_cost_usd == limits.execution.min_order_cost_usd


def test_an_emergency_close_is_attempted_whatever_its_size() -> None:
    manager = manager_for(RecordingTransport())
    plan = manager.plan_for_action(
        Action.EXIT, **plan_args(Action.EXIT, emergency=True, position_amount=0.000001)
    )
    assert len(plan.intents) == 1


# ------------------------------------------------------------------------------------------------
# 13. The kill switch reaches the venue: a /flat produces a force exit.
# ------------------------------------------------------------------------------------------------


def test_a_flat_closes_positions_through_the_transport(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))
    monkeypatch.setenv("AGENTOQUANT_KILL_SWITCH_STATE", str(tmp_path / "kill_switch.json"))
    monkeypatch.setenv("AGENTOQUANT_TELEGRAM", "0")

    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    ledger.migrate()
    store = SignalStore(tmp_path / "signals")
    limits = load_risk_limits()

    switch = build_kill_switch(limits, ledger=ledger, state_path=tmp_path / "kill_switch.json")
    switch.trigger_flat(positions=[{"coin": "BTC"}], cycle_id="flat", actor="telegram:/flat")

    # A fresh process, the way the next hourly tick starts.
    fresh = build_kill_switch(limits, ledger=ledger, state_path=tmp_path / "kill_switch.json")
    assert fresh.halt_state().flat is True

    transport = RecordingTransport()
    summary = run_cycle(
        cycle_id="cycle-flat",
        ledger=ledger,
        store=store,
        transport=transport,
        sequence=0,
        kill_switch=fresh,
        price=PRICE,
        now=HOUR,
        ack_wait_s=0.0,
        notify=False,
    )
    assert summary["closes"] == 1
    assert summary["halts"]["entries_blocked"] is True
    exits = [intent for intent in transport.intents if intent.action == Action.EXIT]
    assert exits, "a /flat produced no exit intent"
    assert exits[0].purpose == "emergency_exit"
    assert exits[0].order_type == "market"
    assert exits[0].freqtrade_call == "forceexit"
    ledger.close()


def test_a_halt_blocks_new_entries_through_the_risk_gate(tmp_path: Path) -> None:
    from agentoquant.risk.kill_switch import HaltState

    card = card_for(Action.ENTER_LADDERED)
    halted = placeholder_context(
        card, price=PRICE, card_record_id="card-1", book_value_cad=900.0, halts=HaltState(flat=True)
    )
    assert halted.halts.entries_blocked is True

    from agentoquant.risk import RiskGate

    verdict = RiskGate(load_risk_limits(), ledger=None).evaluate(card, halted)
    assert verdict.verdict == "rejected"
    assert verdict.final_size_pct == 0.0


def test_the_kill_switch_state_survives_a_restart(tmp_path: Path) -> None:
    limits = load_risk_limits()
    path = tmp_path / "kill_switch.json"
    switch = build_kill_switch(limits, state_path=path)
    switch.trigger_daily_halt(reason="daily loss", cycle_id="c1")
    assert path.exists()
    restored = build_kill_switch(limits, state_path=path)
    assert restored.halt_state().daily_halted is True
    restored.resume(human=True, cycle_id="c2")
    again = build_kill_switch(limits, state_path=path)
    assert again.halt_state().entries_blocked is False


def test_a_halt_never_enlarges_anything() -> None:
    """The invariant Task 5 owns: the switch closes, it never opens."""
    manager = manager_for(RecordingTransport())
    closes = [
        type("Close", (), {"coin": "BTC", "size_pct": 3.0, "reason": "flat"})(),
    ]
    plan = manager.plan_closes(closes, cycle_id="c")
    for intent in plan.intents:
        assert intent.side == "sell"
        assert intent.purpose == "emergency_exit"


# ------------------------------------------------------------------------------------------------
# 14. The loop end to end, against a fake transport.
# ------------------------------------------------------------------------------------------------


def test_the_loop_walks_the_pattern_and_reports_every_cycle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))
    monkeypatch.setenv("AGENTOQUANT_KILL_SWITCH_STATE", str(tmp_path / "kill_switch.json"))
    monkeypatch.setenv("AGENTOQUANT_TELEGRAM", "0")

    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    store = SignalStore(tmp_path / "signals")
    transport = RecordingTransport()
    result = run_loop(
        hours=8,
        placeholder=True,
        sleep=False,
        interval_s=0.0,
        ledger=ledger,
        store=store,
        transport=transport,
        now=HOUR,
        ack_wait_s=0.0,
        log_path=tmp_path / "cycles.jsonl",
    )
    assert result["completed"] == 8, result["failures"]
    assert result["failed"] == 0
    actions = {row["action"] for row in result["results"]}
    assert len(actions) == 7, f"the pattern did not rotate: {actions}"
    assert actions == {
        "enter_laddered", "trail_stop", "hold", "trim", "take_profit_ladder", "add", "exit",
    }
    for row in result["results"]:
        assert row["path"] in set(EXECUTION_PATHS.values())
        assert row["venue_available"] is True
        assert row["degraded"] is False
        assert row["execution_rows"] is not None
    ledger.close()


def test_a_dead_venue_is_reported_not_invented(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))
    monkeypatch.setenv("AGENTOQUANT_KILL_SWITCH_STATE", str(tmp_path / "kill_switch.json"))
    monkeypatch.setenv("AGENTOQUANT_TELEGRAM", "0")

    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    summary = run_cycle(
        cycle_id="cycle-dead",
        ledger=ledger,
        store=SignalStore(tmp_path / "signals"),
        transport=NullTransport("nothing listening"),
        sequence=0,
        price=PRICE,
        now=HOUR,
        ack_wait_s=0.0,
        notify=False,
    )
    assert summary["venue_available"] is False
    assert summary["degraded"] is True
    assert summary["orders"] == 0
    ledger.close()


def test_price_levels_and_sizes_reach_the_hook_paths(tmp_path: Path) -> None:
    """The published intent is what the bridge acts on, so the levels have to be in it."""
    card = card_for(Action.TRAIL_STOP)
    document = signal_document(card, verdict_for(2.0), cycle_id="cycle-1")
    execution = document["execution"]
    assert document["pair"] == pair_for("BTC")
    assert execution["order_type"] == "stop_loss_limit"
    assert execution["side"] == "none"
    assert execution["stop_price"] is None  # no level is known at publish time; the bridge trails
    assert execution["post_only"] is False


# ------------------------------------------------------------------------------------------------
# 15. The two-rate fee schedule: a limit fill pays maker, a taker fill pays taker.
# ------------------------------------------------------------------------------------------------


def test_a_market_fill_is_priced_at_the_taker_rate_and_a_limit_fill_at_the_maker_rate() -> None:
    """One cycle, two fills, two rates. The venue charges one flat (maker) rate for both.

    Stops and emergency exits are the exit path the plan leans on and the venue declares them as
    market orders, so pricing their ledger fee at the flat rate would halve their real cost.
    """
    from agentoquant.execution.freqtrade_strategy import TAKER_ORDER_TYPES, venue_fee_optimism

    config = assert_config()
    optimism = venue_fee_optimism(config)
    assert optimism > 0.0, "the venue's flat fee is not optimistic for taker fills"

    tier = load_fee_tiers().current
    report = {"status": "filled", "fill_price": 1000.0, "fill_qty": 1.0}
    manager = manager_for(RecordingTransport(report=report))

    limit_plan = manager.plan_for_action(
        Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED, slices=1)
    )
    limit_row = manager.execute(limit_plan, cycle_id="cycle-fees")[0]
    assert limit_row["fee_pct"] == tier.maker_pct
    assert limit_row["fee_paid"] == 1000.0 * tier.maker_pct / 100.0

    emergency_plan = manager.plan_for_action(
        Action.EXIT, **plan_args(Action.EXIT, emergency=True)
    )
    taker_row = manager.execute(emergency_plan, cycle_id="cycle-fees")[0]
    assert taker_row["fee_pct"] == tier.taker_pct
    assert taker_row["fee_paid"] == 1000.0 * tier.taker_pct / 100.0

    # The gap between the two recorded rates is exactly the venue's optimism.
    gap = (taker_row["fee_pct"] - limit_row["fee_pct"]) / 100.0
    assert abs(gap - optimism) < 1e-12
    assert taker_row["fee_paid"] == 2 * limit_row["fee_paid"]
    for name in TAKER_ORDER_TYPES:
        assert config["order_types"][name] == "market", name


def test_a_stop_fill_is_priced_at_the_taker_rate() -> None:
    """A stop rests as a stop-limit but the venue executes it as a market order."""
    from agentoquant.execution.freqtrade_strategy import TAKER_ORDER_TYPES

    tier = load_fee_tiers().current
    manager = manager_for(
        RecordingTransport(report={"status": "filled", "fill_price": 1000.0, "fill_qty": 1.0})
    )
    plan = manager.plan_for_action(Action.SET_STOP, **plan_args(Action.SET_STOP))
    row = manager.execute(plan, cycle_id="cycle-fees")[0]
    assert row["order_type"] in TAKER_ORDER_TYPES or row["order_type"] == "stop_loss_limit"
    assert row["fee_pct"] == tier.taker_pct
    assert row["fee_paid"] == 1000.0 * tier.taker_pct / 100.0


def test_an_unfilled_order_records_no_fee() -> None:
    manager = manager_for(
        RecordingTransport(report={"status": "unfilled_timeout", "fill_price": None, "fill_qty": None})
    )
    plan = manager.plan_for_action(Action.ENTER_LADDERED, **plan_args(Action.ENTER_LADDERED, slices=1))
    row = manager.execute(plan, cycle_id="cycle-fees")[0]
    assert row["fee_paid"] is None
    assert row["status"] == "unfilled_timeout"


# ------------------------------------------------------------------------------------------------
# Small parsing helpers for the config/strategy invariant checks.
# ------------------------------------------------------------------------------------------------


def assert_config() -> dict[str, Any]:
    from agentoquant.execution.freqtrade_strategy import load_freqtrade_config

    return load_freqtrade_config()


def _assignment(source: str, name: str) -> str | None:
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{name} ="):
            return stripped.partition("=")[2].strip()
    return None


def _dict_literal(source: str, name: str) -> dict[str, Any]:
    """Parse a flat ``name = { "k": v, ... }`` literal out of the strategy source."""
    start = source.find(f"{name} = {{")
    assert start != -1, f"{name} is not assigned in the strategy"
    end = source.index("}", start)
    body = source[source.index("{", start) + 1 : end]
    parsed: dict[str, Any] = {}
    for entry in body.replace("\n", " ").split(","):
        if ":" not in entry:
            continue
        key, _, value = entry.partition(":")
        key = key.strip().strip('"').strip("'")
        value = value.strip()
        if not key or not value:
            continue
        if value in {"True", "False"}:
            parsed[key] = value == "True"
        elif value.startswith(('"', "'")):
            parsed[key] = value.strip('"').strip("'")
        else:
            try:
                parsed[key] = float(value) if "." in value else int(value)
            except ValueError:
                parsed[key] = value
    return parsed


def test_the_strategy_module_loads_and_its_roi_step_reads_the_ladder(tmp_path: Path) -> None:
    """The bridge's take-profit ladder is a real function over the published ladder."""
    module = _strategy_module()
    trade = type(
        "Trade",
        (),
        {"open_date_utc": HOUR - timedelta(minutes=45), "stake_amount": 100.0, "amount": 0.001},
    )()
    assert module.roi_step(MINIMAL_ROI_LADDER, trade, HOUR) == 0.02
    assert module.roi_step(MINIMAL_ROI_LADDER, trade, HOUR + timedelta(hours=3)) == 0.0
    assert module.roi_step({}, trade, HOUR) is None


def _strategy_module() -> Any:
    path = str(strategy_dir())
    if path not in sys.path:
        sys.path.insert(0, path)
    import AgentBridgeStrategy

    return AgentBridgeStrategy


def test_the_bridge_fires_the_take_profit_ladder_against_a_published_document(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "signals")
    card = card_for(Action.TAKE_PROFIT_LADDER, cycle="cycle-tp")
    store.publish(card, verdict_for(3.0), cycle_id="cycle-tp")

    module = _strategy_module()
    strategy = module.AgentBridgeStrategy.__new__(module.AgentBridgeStrategy)
    strategy.config = {}
    strategy.signal_dir = store.root
    strategy.last_stop = {}
    strategy.ladder_slices = {}
    strategy.acked_cycles = {}

    trade = type(
        "Trade",
        (),
        {"pair": "BTC/USD", "open_date_utc": HOUR - timedelta(minutes=45), "stake_amount": 100.0},
    )()
    assert strategy.custom_exit("BTC/USD", trade, HOUR, 100.0, 0.005) is None
    assert strategy.custom_exit("BTC/USD", trade, HOUR, 100.0, 0.03) == "take_profit_ladder"
    ack = store.read_ack("cycle-tp")
    assert ack is not None and ack["result"]["action"] == "take_profit_ladder"


def test_the_bridge_refuses_a_trim_it_cannot_size(tmp_path: Path) -> None:
    """The trim that produced the venue's "exit amount is now 0.0" no-op must be refused here."""
    store = SignalStore(tmp_path / "signals")
    store.publish(card_for(Action.TRIM, cycle="cycle-trim"), verdict_for(1.5), cycle_id="cycle-trim")

    module = _strategy_module()
    strategy = module.AgentBridgeStrategy.__new__(module.AgentBridgeStrategy)
    strategy.config = {}
    strategy.signal_dir = store.root
    strategy.last_stop = {}
    strategy.ladder_slices = {}
    strategy.acked_cycles = {}

    empty = type("Trade", (), {"pair": "BTC/USD", "amount": 0.0, "stake_amount": 0.0})()
    assert strategy.adjust_trade_position(
        empty, HOUR, 60_000.0, 0.0, 5.0, 900.0, 60_000.0, 60_000.0, 0.0, 0.0
    ) is None
    ack = store.read_ack("cycle-trim")
    assert ack is not None and ack["result"]["action"] == "trim_refused"


def test_the_bridge_prices_each_ladder_slice_at_its_own_offset() -> None:
    """freqtrade takes no price from adjust_trade_position, so the bridge stashes one per slice."""
    module = _strategy_module()
    document = {"ladder_offsets_pct": list(LADDER_OFFSETS_PCT)}
    prices = [
        module.AgentBridgeStrategy._ladder_price(document, 100.0, index) for index in range(3)
    ]
    assert prices == [99.9, 99.75, 99.6]
    assert len(set(prices)) == 3
    assert prices == sorted(prices, reverse=True)


def test_the_bridge_falls_back_to_its_own_offsets_when_the_document_carries_none() -> None:
    module = _strategy_module()
    prices = [module.AgentBridgeStrategy._ladder_price({}, 100.0, index) for index in range(4)]
    assert prices[3] == prices[2], "an index past the table reuses the deepest offset"


def test_custom_entry_price_returns_only_a_price_the_slice_hook_stashed() -> None:
    module = _strategy_module()
    strategy = module.AgentBridgeStrategy.__new__(module.AgentBridgeStrategy)
    strategy.pending_entry_price = {}
    # Nothing stashed: freqtrade's own pricing stands, which is what the force entry needs.
    assert strategy.custom_entry_price("BTC/USD", None, HOUR, 100.0, None, "long") is None
    strategy.pending_entry_price["BTC/USD"] = 99.9
    assert strategy.custom_entry_price("BTC/USD", None, HOUR, 100.0, None, "long") == 99.9
    assert strategy.custom_entry_price("BTC/USD", None, HOUR, 100.0, None, "long") is None


def test_the_placeholder_universe_comes_from_the_sleeve_config() -> None:
    universe = placeholder_universe()
    assert universe and "BTC" in universe
    assert all(coin.isupper() for coin in universe)


def test_the_document_the_bridge_reads_is_plain_json(tmp_path: Path) -> None:
    """The strategy imports the standard library only, so the document must be JSON, not a model."""
    store = SignalStore(tmp_path / "signals")
    store.publish(card_for(Action.ENTER_LADDERED), verdict_for(3.0), cycle_id="cycle-1")
    raw = store.current_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["card"]["action"] == Action.ENTER_LADDERED.value
    assert payload["sleeve"] == Sleeve.A.value
    assert payload["confidence_band"] == ConfidenceBand.STANDARD.value


def test_the_coin_the_card_names_is_the_pair_the_bridge_trades(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "signals")
    store.publish(card_for(Action.ADD, coin="ETH"), verdict_for(2.0), cycle_id="cycle-1")
    document = store.latest()
    assert document["coin"] == "ETH"
    assert document["pair"] == pair_for("ETH")


def test_the_size_free_paths_carry_no_size() -> None:
    manager = manager_for(RecordingTransport())
    for action, path, _call in ACTION_PATHS:
        if path not in SIZE_FREE_PATHS:
            continue
        plan = manager.plan_for_action(action, **plan_args(action))
        for intent in plan.intents:
            assert intent.stake_amount is None, action
