"""Adversarial suite for the Risk Gate, the kill switch and the funding floor (Task 5).

Every number used here is read from the real ``config/*.yaml``: the test file asserts the gate's
limits against the raw YAML, so a config change cannot silently pass. The adversarial cases are the
ones the task names (oversized, over-concentrated, no stop, taker order, illiquid coin, over the
turnover cap, over the Ontario net-buy cap) plus one case per rule the gate can fire.

The load-bearing invariant is asserted for **every** verdict in the suite and again in a dedicated
sweep: ``final_size_pct <= original_size_pct``. The gate can shrink or reject, never enlarge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from agentoquant.config_loader import repo_root
from agentoquant.enums import Action, ConfidenceBand, Sleeve
from agentoquant.ledger.schema import DecisionCard
from agentoquant.ledger.store import LedgerStore
from agentoquant.risk.funding_floor import (
    free_cash_floor_pct_of_book,
)
from agentoquant.risk.gate import (
    ALL_RULES,
    RULE_CONFIDENCE_BAND_NO_TRADE,
    RULE_COOLDOWN,
    RULE_DAILY_LOSS_HALT,
    RULE_DAILY_TURNOVER_CAP,
    RULE_ENTRY_CONFIDENCE,
    RULE_FREE_CASH,
    RULE_FREE_CASH_FLOOR,
    RULE_INCOMPLETE_CARD,
    RULE_KILL_SWITCH_FLAT,
    RULE_MAX_CONCURRENT_POSITIONS,
    RULE_MAX_SPREAD,
    RULE_MIN_LIQUIDITY,
    RULE_MISSING_SIZE,
    RULE_MISSING_STOP,
    RULE_NOT_TRADABLE,
    RULE_ONTARIO_NET_BUY_CAP,
    RULE_POSITION_CAP,
    RULE_POST_ONLY_REQUIRED,
    RULE_SLEEVE_CAP,
    RULE_SLEEVE_DISABLED,
    RULE_UNSUPPORTED_ACTION,
    RULE_VENUE_STATUS,
    RULE_WEEKLY_DRAWDOWN_HALT,
    MarketContext,
    PortfolioContext,
    PositionContext,
    RiskGate,
)
from agentoquant.risk.kill_switch import (
    HaltState,
)

CYCLE = "2026-09-19T14Z-0001"
NOW = datetime(2026, 9, 19, 14, 0, tzinfo=UTC)


# ----------------------------------------------------------------------------------------------
# Fixtures: the real config, a real ledger, and a gate built from both
# ----------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def limits():
    from agentoquant.config_loader import load_risk_limits

    return load_risk_limits()


@pytest.fixture(scope="module")
def sleeves():
    from agentoquant.config_loader import load_sleeves

    return load_sleeves()


@pytest.fixture
def ledger(tmp_path: Path) -> LedgerStore:
    return LedgerStore(db_path=tmp_path / "ledger.duckdb")


@pytest.fixture
def gate(limits, ledger) -> RiskGate:
    return RiskGate(limits, ledger)


def raw_config(name: str) -> dict:
    """The committed YAML, unparsed by pydantic: the test's independent source of truth."""
    return yaml.safe_load((repo_root() / "config" / name).read_text(encoding="utf-8"))


def make_card(**overrides) -> DecisionCard:
    """A card that the base context approves, with any field overridable."""
    fields = {
        "cycle_id": CYCLE,
        "selected_proposal_id": "PROPOSAL-0001",
        "action": Action.ENTER_LADDERED,
        "coin": "SOL",
        "sleeve": Sleeve.A,
        "size_pct": 5.0,
        "confidence": 70,
        "confidence_band": ConfidenceBand.STANDARD,
        "p_up": 0.61,
        "interval_low": 0.54,
        "interval_high": 0.68,
        "ev_after_fees": 0.42,
        "fee_tier_assumed": "Tier 1",
        "evidence_split": {"primary": 2, "verified": 1, "unverified": 0},
        "strongest_objection": {"severity": 2, "text": "fee drag at Tier 1"},
        "flip_condition": "BTC closes below its 50-day moving average",
    }
    fields.update(overrides)
    return DecisionCard(**fields)


def deep_market(coin: str, **overrides) -> MarketContext:
    """A coin that clears the real liquidity and spread floors by a wide margin."""
    fields = {"coin": coin, "volume_24h_usd": 50_000_000.0, "spread_pct": 0.05, "tradable": True}
    fields.update(overrides)
    return MarketContext(**fields)


def base_context(limits, **overrides) -> PortfolioContext:
    """A context in which :func:`make_card` is approved. Every rule is clear."""
    fields = {
        "book_value_cad": 900.0,
        "positions": (PositionContext("BTC", Sleeve.A, 10.0, stop_on_exchange=True),),
        "sleeve_weights_pct": {Sleeve.A: 40.0, Sleeve.B: 10.0, Sleeve.C: 0.0},
        "free_cash_pct_by_sleeve": {Sleeve.A: 30.0, Sleeve.B: 10.0, Sleeve.C: 5.0},
        "markets": {"SOL": deep_market("SOL"), "BTC": deep_market("BTC")},
        "daily_turnover_used_pct": 0.0,
        "daily_loss_used_pct": 0.0,
        "drawdown_used_pct": 0.0,
        "consecutive_losses": 0,
        "venue": "kraken",
        "venue_status": "online",
        "order_type": "post_only_limit",
        "order_purpose": "entry",
        "stop_on_exchange": True,
        "stop_price": 118.5,
        "now": NOW,
    }
    fields.update(overrides)
    return PortfolioContext(**fields)


def verdict_rows(ledger: LedgerStore) -> list[dict]:
    return ledger.query('SELECT * FROM "risk_gate_verdict" ORDER BY "ts", "record_id"')


def funding_rows(ledger: LedgerStore) -> list[dict]:
    return ledger.query('SELECT * FROM "funding_request" ORDER BY "ts", "record_id"')


def assert_never_enlarges(verdict) -> None:
    """The invariant every verdict must satisfy, asserted at every call site in this file."""
    assert verdict.final_size_pct <= verdict.original_size_pct
    assert verdict.final_size_pct >= 0.0
    if verdict.verdict == "approved":
        assert verdict.final_size_pct == verdict.original_size_pct
    if verdict.verdict == "rejected":
        assert verdict.final_size_pct == 0.0
    assert verdict.rule_fired is None or verdict.rule_fired in ALL_RULES


# ----------------------------------------------------------------------------------------------
# The numbers are the committed config's, not invented
# ----------------------------------------------------------------------------------------------


def test_limits_come_from_the_committed_config(limits, sleeves):
    raw_risk = raw_config("risk_limits.yaml")
    raw_sleeves = raw_config("sleeves.yaml")

    assert limits.positions.min_concurrent == raw_risk["positions"]["min_concurrent"] == 3
    assert limits.positions.max_concurrent == raw_risk["positions"]["max_concurrent"] == 5
    assert limits.positions.max_position_pct == raw_risk["positions"]["max_position_pct"] == 15
    assert limits.turnover.daily_turnover_cap_pct == raw_risk["turnover"]["daily_turnover_cap_pct"]
    assert limits.halts.daily_loss_halt_pct == raw_risk["halts"]["daily_loss_halt_pct"] == 3
    assert limits.halts.weekly_drawdown_halt_pct == raw_risk["halts"]["weekly_drawdown_halt_pct"] == 8
    assert limits.halts.weekly_halt_requires_human_restart is True
    assert limits.execution.post_only_default is True
    assert limits.execution.mandatory_stop_on_exchange is True
    assert limits.execution.market_orders_allowed_for == ["stop_loss", "emergency_exit"]
    assert limits.liquidity.min_24h_volume_usd == raw_risk["liquidity"]["min_24h_volume_usd"]
    assert limits.liquidity.max_spread_pct == raw_risk["liquidity"]["max_spread_pct"] == 0.5
    assert limits.cooldown.consecutive_losses == 2
    assert limits.cooldown.cooldown_hours == 6
    assert limits.regulatory.ontario_net_buy_cap_cad == 30000
    assert limits.regulatory.ontario_net_buy_window_months == 12
    assert limits.funding.monthly_request_cap == raw_risk["funding"]["monthly_request_cap"] == 4

    assert sleeves.get(Sleeve.A).entry_confidence == 60
    assert sleeves.get(Sleeve.B).entry_confidence == 65
    assert sleeves.get(Sleeve.C).entry_confidence == 75
    assert sleeves.get(Sleeve.A).cap_pct == 100
    assert sleeves.get(Sleeve.B).cap_pct == 35
    assert sleeves.get(Sleeve.C).cap_pct == 20
    assert sleeves.get(Sleeve.C).enabled is False
    assert raw_sleeves["sleeves"]["C"]["enabled"] is False


def test_free_cash_floor_converts_sleeve_target_to_book_value(limits, sleeves):
    raw = raw_config("risk_limits.yaml")["funding"]["free_cash_floor_pct"]
    for sleeve in Sleeve:
        expected = raw[sleeve.value] * sleeves.get(sleeve).cap_pct / 100.0
        assert free_cash_floor_pct_of_book(sleeve, limits, sleeves) == pytest.approx(expected)
    assert free_cash_floor_pct_of_book(Sleeve.A, limits, sleeves) == pytest.approx(10.0)
    assert free_cash_floor_pct_of_book(Sleeve.B, limits, sleeves) == pytest.approx(3.5)
    assert free_cash_floor_pct_of_book(Sleeve.C, limits, sleeves) == pytest.approx(2.0)


# ----------------------------------------------------------------------------------------------
# The happy path, so a rejection below is the rule firing and not a broken fixture
# ----------------------------------------------------------------------------------------------


def test_base_card_is_approved_and_unchanged(gate, limits, ledger):
    card = make_card()
    before = card.model_dump()
    verdict = gate.evaluate(card, base_context(limits))
    assert_never_enlarges(verdict)
    assert verdict.verdict == "approved"
    assert verdict.rule_fired is None
    assert verdict.final_size_pct == 5.0
    assert verdict.funding_floor_breach is False
    assert card.model_dump() == before, "the gate must never mutate the card"


def test_reductions_and_hold_are_never_blocked_by_a_halt(gate, limits):
    halted = base_context(limits, halts=HaltState(flat=True, daily_halted=True, weekly_halted=True))
    for action in (Action.TRIM, Action.EXIT, Action.HOLD, Action.SET_STOP, Action.TRAIL_STOP):
        size = None if action is Action.HOLD else 4.0
        card = make_card(action=action, size_pct=size)
        verdict = gate.evaluate(card, halted)
        assert_never_enlarges(verdict)
        assert verdict.verdict == "approved", action
        assert verdict.rule_fired is None


# ----------------------------------------------------------------------------------------------
# One adversarial case per rule the gate can fire
# ----------------------------------------------------------------------------------------------

#: Five open positions, so a card for a sixth coin trips the concurrency rule.
FIVE_POSITIONS = tuple(
    PositionContext(coin, Sleeve.A, 5.0, stop_on_exchange=True)
    for coin in ("BTC", "ETH", "SOL", "ADA", "DOT")
)

#: rule -> (card overrides, context overrides). Each case must fire exactly that rule.
REJECT_CASES: dict[str, tuple[dict, dict]] = {
    RULE_UNSUPPORTED_ACTION: ({"action": "grid_trade"}, {}),
    RULE_MISSING_SIZE: ({"size_pct": None}, {}),
    RULE_INCOMPLETE_CARD: ({"coin": None}, {}),
    RULE_KILL_SWITCH_FLAT: ({}, {"halts": HaltState(flat=True)}),
    RULE_DAILY_LOSS_HALT: ({}, {"halts": HaltState(daily_halted=True)}),
    RULE_WEEKLY_DRAWDOWN_HALT: ({}, {"halts": HaltState(weekly_halted=True)}),
    RULE_COOLDOWN: ({}, {"consecutive_losses": 2, "last_loss_at": NOW - timedelta(hours=1)}),
    RULE_VENUE_STATUS: ({}, {"venue_status": "maintenance"}),
    RULE_SLEEVE_DISABLED: (
        {"sleeve": Sleeve.C, "coin": "XYZ", "confidence": 80},
        {"markets": {"XYZ": deep_market("XYZ")}},
    ),
    RULE_MAX_CONCURRENT_POSITIONS: (
        {"coin": "AVAX", "confidence": 70},
        {"positions": FIVE_POSITIONS, "markets": {"AVAX": deep_market("AVAX")}},
    ),
    RULE_CONFIDENCE_BAND_NO_TRADE: ({"confidence": 40, "confidence_band": ConfidenceBand.NO_TRADE}, {}),
    RULE_ENTRY_CONFIDENCE: ({"confidence": 58, "confidence_band": ConfidenceBand.SMALL}, {}),
    RULE_MISSING_STOP: ({}, {"stop_on_exchange": False, "stop_price": None}),
    RULE_POST_ONLY_REQUIRED: ({}, {"order_type": "market", "order_purpose": "entry"}),
    RULE_NOT_TRADABLE: ({}, {"markets": {"SOL": deep_market("SOL", tradable=False)}}),
    RULE_MIN_LIQUIDITY: ({}, {"markets": {"SOL": deep_market("SOL", volume_24h_usd=500_000.0)}}),
    RULE_MAX_SPREAD: ({}, {"markets": {"SOL": deep_market("SOL", spread_pct=0.9)}}),
    RULE_DAILY_TURNOVER_CAP: ({}, {"daily_turnover_used_pct": 22.0}),
    RULE_ONTARIO_NET_BUY_CAP: ({}, {"ontario_net_buys_cad_12m": {"SOL": 30_000.0}}),
    RULE_FREE_CASH_FLOOR: (
        {},
        {"free_cash_pct_by_sleeve": {Sleeve.A: 8.0, Sleeve.B: 10.0, Sleeve.C: 5.0}},
    ),
}

#: rule -> (card overrides, context overrides, the size the rule permits).
SHRINK_CASES: dict[str, tuple[dict, dict, float]] = {
    # A 15% per-position cap against a 25% request.
    RULE_POSITION_CAP: ({"size_pct": 25.0}, {}, 15.0),
    # Sleeve B is 33% of the book against a 35% cap, so only 2% of headroom is left.
    RULE_SLEEVE_CAP: (
        {"sleeve": Sleeve.B, "coin": "TAO", "size_pct": 10.0, "confidence": 70},
        {
            "sleeve_weights_pct": {Sleeve.A: 40.0, Sleeve.B: 33.0, Sleeve.C: 0.0},
            "markets": {"TAO": deep_market("TAO")},
        },
        2.0,
    ),
    # Sleeve A free cash 12% against a 10% floor leaves 2% of headroom.
    RULE_FREE_CASH: (
        {"size_pct": 8.0},
        {"free_cash_pct_by_sleeve": {Sleeve.A: 12.0, Sleeve.B: 10.0, Sleeve.C: 5.0}},
        2.0,
    ),
}


def card_for(overrides: dict) -> DecisionCard:
    """Build a card, bypassing validation when a case needs an out-of-vocabulary action."""
    if isinstance(overrides.get("action"), str):
        return make_card().model_copy(update=overrides)
    return make_card(**overrides)


@pytest.mark.parametrize("rule", sorted(REJECT_CASES))
def test_every_reject_rule_fires(gate, limits, rule):
    card_over, ctx_over = REJECT_CASES[rule]
    verdict = gate.evaluate(card_for(card_over), base_context(limits, **ctx_over))
    assert_never_enlarges(verdict)
    assert verdict.verdict == "rejected", f"{rule} produced {verdict.verdict}"
    assert verdict.rule_fired == rule
    assert verdict.final_size_pct == 0.0


@pytest.mark.parametrize("rule", sorted(SHRINK_CASES))
def test_every_shrink_rule_fires(gate, limits, rule):
    card_over, ctx_over, permitted = SHRINK_CASES[rule]
    verdict = gate.evaluate(card_for(card_over), base_context(limits, **ctx_over))
    assert_never_enlarges(verdict)
    assert verdict.verdict == "shrunk", f"{rule} produced {verdict.verdict}"
    assert verdict.rule_fired == rule
    assert verdict.final_size_pct == pytest.approx(permitted)
