"""Regression tests for the Phase 0 adversary findings F7, F8, F11 and F12 in the Risk Gate.

Every case here failed on the gate as merged (`main` at the review's baseline) and passes after
the fix; each test names the finding it guards. The numbers are the committed config's, read
through the real loader, and the never-enlarge invariant is asserted in every test that produces a
verdict.

Decorator-free on purpose (see `.hermes.md`): the model gateway refuses a request whose history
carries a tool call containing an at-sign followed by a dotted name, so this file uses plain
functions, plain loops over case tables and plain helper functions. `tmp_path` arrives as an
ordinary argument.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentoquant.config_loader import load_risk_limits, load_settings, repo_root
from agentoquant.enums import Action, ConfidenceBand, Sleeve
from agentoquant.ledger.schema import DecisionCard
from agentoquant.ledger.store import LedgerStore
from agentoquant.risk.gate import (
    ALL_RULES,
    INCREASING_ACTIONS,
    NEUTRAL_ACTIONS,
    REDUCING_ACTIONS,
    REJECT_RULES,
    RULE_HOLD_SIZE_ZERO,
    RULE_INCOMPLETE_CARD,
    RULE_MISSING_SIZE,
    RULE_MISSING_STOP,
    RULE_REDUCTION_CAP,
    RULE_STALE_MARKET_DATA,
    RULE_UNRESOLVED_SEVERITY5,
    RULE_VENUE_STATUS,
    SHRINK_RULES,
    STOP_MANAGEMENT_ACTIONS,
    UNRESOLVED_OBJECTION_SEVERITY,
    MarketContext,
    PortfolioContext,
    PositionContext,
    RiskGate,
    objection_severity,
)
from agentoquant.risk.kill_switch import HaltState

CYCLE = "2026-09-19T14Z-0001"
NOW = datetime(2026, 9, 19, 14, 0, tzinfo=UTC)


# ----------------------------------------------------------------------------------------------
# Helpers: the real config, a real card, a context every rule clears
# ----------------------------------------------------------------------------------------------


def make_gate(limits=None, ledger=None) -> RiskGate:
    """The real gate over the real committed limits."""
    return RiskGate(limits or load_risk_limits(), ledger)


def make_card(**overrides) -> DecisionCard:
    """A card the base context approves, with any field overridable."""
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


def base_context(**overrides) -> PortfolioContext:
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


def assert_never_enlarges(verdict) -> None:
    """The invariant, asserted at every call site in this file."""
    assert verdict.final_size_pct <= verdict.original_size_pct, verdict
    assert verdict.final_size_pct >= 0.0, verdict
    if verdict.verdict == "approved":
        assert verdict.final_size_pct == verdict.original_size_pct, verdict
    if verdict.verdict == "rejected":
        assert verdict.final_size_pct == 0.0, verdict
    assert verdict.rule_fired is None or verdict.rule_fired in ALL_RULES, verdict


def evaluate(card: DecisionCard, context: PortfolioContext, limits=None):
    verdict = make_gate(limits).evaluate(card, context)
    assert_never_enlarges(verdict)
    return verdict


#: A card that violates every rule at once, and a context with nothing working.
HOSTILE_CARD = dict(
    coin=None,
    sleeve=None,
    size_pct=99.0,
    confidence=0,
    confidence_band=ConfidenceBand.NO_TRADE,
    strongest_objection={"severity": 5, "text": "the thesis is contradicted"},
)
HOSTILE_CONTEXT = dict(
    venue_status="offline",
    stop_on_exchange=False,
    stop_price=None,
    halts=HaltState(flat=True),
    markets={},
)


# ----------------------------------------------------------------------------------------------
# F8 - the gate is a pass-through for seven of the twelve vocabulary actions
# ----------------------------------------------------------------------------------------------


def test_no_vocabulary_action_is_a_pass_through() -> None:
    """F8. Before the fix, seven of twelve actions returned approved/None/original untouched."""
    gate = make_gate()
    pass_through: list[str] = []
    for action in Action:
        card = make_card(action=action, **HOSTILE_CARD)
        verdict = gate.evaluate(card, base_context(**HOSTILE_CONTEXT))
        assert_never_enlarges(verdict)
        unchanged = verdict.final_size_pct == verdict.original_size_pct
        if verdict.verdict == "approved" and verdict.rule_fired is None and unchanged:
            pass_through.append(action.value)
    assert pass_through == [], f"actions the gate still waves through: {pass_through}"


def test_the_former_pass_throughs_are_bounded_by_a_named_rule() -> None:
    """One case per former pass-through: the rule that now shrinks or rejects it."""
    cases: dict[str, tuple[dict, dict, str, str]] = {
        "trim": (
            {"size_pct": 25.0},
            {"positions": (PositionContext("SOL", Sleeve.A, 12.0, stop_on_exchange=True),)},
            "shrunk",
            RULE_REDUCTION_CAP,
        ),
        "exit": (
            {"size_pct": 40.0},
            {"positions": (PositionContext("SOL", Sleeve.A, 3.0, stop_on_exchange=True),)},
            "shrunk",
            RULE_REDUCTION_CAP,
        ),
        "hold": ({"size_pct": 99.0}, {}, "shrunk", RULE_HOLD_SIZE_ZERO),
        "set_stop": ({}, {"venue_status": "maintenance"}, "rejected", RULE_VENUE_STATUS),
        "trail_stop": (
            {},
            {"stop_on_exchange": False, "stop_price": None},
            "rejected",
            RULE_MISSING_STOP,
        ),
        "take_profit_ladder": (
            {},
            {"stop_on_exchange": False, "stop_price": None},
            "rejected",
            RULE_MISSING_STOP,
        ),
        "cancel_order": ({"coin": None}, {}, "rejected", RULE_INCOMPLETE_CARD),
    }
    gate = make_gate()
    for name, (card_over, ctx_over, verdict_expected, rule_expected) in cases.items():
        card = make_card(action=Action(name), **card_over)
        verdict = gate.evaluate(card, base_context(**ctx_over))
        assert_never_enlarges(verdict)
        assert (verdict.verdict, verdict.rule_fired) == (verdict_expected, rule_expected), (
            name,
            verdict.verdict,
            verdict.rule_fired,
        )


def test_a_hold_never_publishes_a_size() -> None:
    """F8: a hold used to echo the card's size back as an approved ``final_size_pct``."""
    held = evaluate(make_card(action=Action.HOLD, coin=None, sleeve=None, size_pct=99.0),
                    base_context())
    assert (held.verdict, held.rule_fired, held.final_size_pct) == (
        "shrunk",
        RULE_HOLD_SIZE_ZERO,
        0.0,
    )
    nothing = evaluate(make_card(action=Action.HOLD, coin=None, sleeve=None, size_pct=None),
                       base_context())
    assert (nothing.verdict, nothing.rule_fired, nothing.final_size_pct) == ("approved", None, 0.0)


def test_a_reduction_is_bounded_by_the_position_it_reduces() -> None:
    """F8: a trim of 25% of the book against a 12% position is shrunk to the position."""
    context = base_context(positions=(PositionContext("SOL", Sleeve.A, 12.0, stop_on_exchange=True),))
    shrunk = evaluate(make_card(action=Action.TRIM, size_pct=25.0), context)
    assert (shrunk.verdict, shrunk.rule_fired, shrunk.final_size_pct) == (
        "shrunk",
        RULE_REDUCTION_CAP,
        12.0,
    )
    full = evaluate(make_card(action=Action.EXIT, size_pct=12.0), context)
    assert (full.verdict, full.final_size_pct) == ("approved", 12.0)
    closed = evaluate(
        make_card(action=Action.EXIT, size_pct=4.0),
        base_context(positions=(PositionContext("SOL", Sleeve.A, 0.0, stop_on_exchange=True),)),
    )
    assert (closed.verdict, closed.rule_fired) == ("rejected", RULE_REDUCTION_CAP)


def test_a_reduction_the_gate_cannot_verify_is_left_alone() -> None:
    """A position the context does not name is not guessed at: blocking a reduction is the one
    failure mode worth avoiding, so an unverifiable reduction passes unchanged."""
    verdict = evaluate(make_card(action=Action.TRIM, size_pct=4.0), base_context())
    assert (verdict.verdict, verdict.rule_fired, verdict.final_size_pct) == ("approved", None, 4.0)


def test_a_reduction_must_name_a_coin_and_a_size() -> None:
    for card, rule in (
        (make_card(action=Action.TRIM, coin=None, size_pct=4.0), RULE_INCOMPLETE_CARD),
        (make_card(action=Action.EXIT, size_pct=None), RULE_MISSING_SIZE),
        (make_card(action=Action.EXIT, size_pct=0.0), RULE_MISSING_SIZE),
    ):
        verdict = evaluate(card, base_context())
        assert (verdict.verdict, verdict.rule_fired) == ("rejected", rule), verdict


def test_management_needs_a_coin_and_an_online_venue() -> None:
    for action in (Action.SET_STOP, Action.TRAIL_STOP, Action.TAKE_PROFIT_LADDER,
                   Action.CANCEL_ORDER):
        nameless = evaluate(make_card(action=action, coin=None), base_context())
        assert (nameless.verdict, nameless.rule_fired) == ("rejected", RULE_INCOMPLETE_CARD), action
        offline = evaluate(make_card(action=action), base_context(venue_status="offline"))
        assert (offline.verdict, offline.rule_fired) == ("rejected", RULE_VENUE_STATUS), action
        clean = evaluate(make_card(action=action), base_context())
        assert clean.verdict == "approved", (action, clean.rule_fired)


def test_management_and_reductions_are_not_blocked_by_a_halt() -> None:
    """A halt closes positions: freezing stop management would be the opposite of what it is for."""
    halted = base_context(halts=HaltState(flat=True, daily_halted=True, weekly_halted=True))
    for action in (Action.TRIM, Action.EXIT, Action.HOLD, Action.SET_STOP, Action.TRAIL_STOP,
                   Action.TAKE_PROFIT_LADDER, Action.CANCEL_ORDER):
        size = None if action is Action.HOLD else 4.0
        verdict = evaluate(make_card(action=action, size_pct=size), halted)
        assert verdict.verdict == "approved", (action, verdict.rule_fired)


def test_a_severity_5_objection_blocks_entries_only() -> None:
    """F7 and F8 together: the objection stops new exposure, never a way out of one."""
    objection = {"severity": 5, "text": "the thesis is contradicted"}
    entry = evaluate(make_card(strongest_objection=objection), base_context())
    assert (entry.verdict, entry.rule_fired) == ("rejected", RULE_UNRESOLVED_SEVERITY5)
    for action in (Action.TRIM, Action.EXIT):
        verdict = evaluate(
            make_card(action=action, size_pct=4.0, strongest_objection=objection), base_context()
        )
        assert verdict.verdict == "approved", action


def test_objection_severity_parsing() -> None:
    """A malformed objection is unresolved rather than absent: it fails closed."""
    assert objection_severity(make_card(strongest_objection=None)) == 0
    assert objection_severity(make_card(strongest_objection={"severity": 2})) == 2
    for broken in ({}, {"severity": None}, {"severity": "high"}, {"text": "no severity"}):
        assert objection_severity(make_card(strongest_objection=broken)) == (
            UNRESOLVED_OBJECTION_SEVERITY
        ), broken


def test_the_action_sets_partition_the_vocabulary() -> None:
    """Every action belongs to exactly one evaluation path, and there are twelve of them."""
    sets = (INCREASING_ACTIONS, REDUCING_ACTIONS, NEUTRAL_ACTIONS)
    assert set().union(*sets) == set(Action)
    assert sum(len(s) for s in sets) == len(Action) == 12
    assert STOP_MANAGEMENT_ACTIONS <= NEUTRAL_ACTIONS
    assert Action.HOLD not in STOP_MANAGEMENT_ACTIONS


def test_the_rule_lists_are_complete_and_unique() -> None:
    assert ALL_RULES == REJECT_RULES + SHRINK_RULES
    assert len(set(ALL_RULES)) == len(ALL_RULES)
    assert RULE_HOLD_SIZE_ZERO in SHRINK_RULES
    assert RULE_REDUCTION_CAP in SHRINK_RULES


def test_no_action_and_no_size_can_be_enlarged() -> None:
    """The invariant, swept over every action and every hostile size."""
    sizes: list[float | None] = [None, 0.0, -50.0, 1e-9, 1.0, 25.0, 1e9, float("inf"),
                                 float("nan")]
    contexts = {
        "clean": base_context(),
        "hostile": base_context(**HOSTILE_CONTEXT),
        "halted": base_context(halts=HaltState(flat=True, daily_halted=True, weekly_halted=True)),
        "over-turnover": base_context(daily_turnover_used_pct=24.0),
        "positioned": base_context(
            positions=(PositionContext("SOL", Sleeve.A, 12.0, stop_on_exchange=True),)
        ),
    }
    gate = make_gate()
    checked = 0
    for action in Action:
        for size in sizes:
            for name, context in contexts.items():
                card = make_card(action=action, size_pct=size)
                verdict = gate.evaluate(card, context)
                assert verdict.final_size_pct <= verdict.original_size_pct, (action, size, name)
                assert verdict.final_size_pct >= 0.0, (action, size, name)
                assert verdict.rule_fired is None or verdict.rule_fired in ALL_RULES, (
                    action,
                    size,
                    name,
                )
                checked += 1
    assert checked == len(Action) * len(sizes) * len(contexts) == 540


def test_a_ledger_attached_does_not_change_a_verdict(tmp_path: Path) -> None:
    """The gate's verdicts are pure: attaching a ledger only records them."""
    ledger = LedgerStore(db_path=tmp_path / "ledger.duckdb")
    with_ledger = RiskGate(load_risk_limits(), ledger)
    without = make_gate()
    for action in Action:
        card = make_card(action=action, **HOSTILE_CARD)
        context = base_context(**HOSTILE_CONTEXT)
        first = with_ledger.evaluate(card, context)
        second = without.evaluate(card, context)
        assert (first.verdict, first.rule_fired, first.final_size_pct) == (
            second.verdict,
            second.rule_fired,
            second.final_size_pct,
        ), action
    ledger.close()


# ----------------------------------------------------------------------------------------------
# F11 - nothing in the gate could detect stale data
# ----------------------------------------------------------------------------------------------

#: One cadence plus a margin, from ``config/settings.yaml`` (60 minutes).
STALENESS_LIMIT_S = 2 * 60 * 60


def test_the_staleness_limit_follows_the_cadence() -> None:
    """The limit is derived from ``settings.cadence_minutes``, not invented inside the gate."""
    cadence_s = float(load_settings().cadence_minutes) * 60.0
    assert make_gate().max_market_data_age_s == cadence_s * 2.0 == STALENESS_LIMIT_S


def test_a_stale_snapshot_is_rejected() -> None:
    """F11: a three-hour-old snapshot used to be indistinguishable from a fresh one."""
    for age in (timedelta(hours=2, seconds=1), timedelta(hours=3), timedelta(days=1)):
        context = base_context(markets={"SOL": deep_market("SOL", as_of=NOW - age)})
        verdict = evaluate(make_card(), context)
        assert (verdict.verdict, verdict.rule_fired) == (
            "rejected",
            RULE_STALE_MARKET_DATA,
        ), age


def test_a_snapshot_inside_the_limit_is_fresh() -> None:
    for age in (timedelta(0), timedelta(minutes=59), timedelta(hours=1, minutes=59)):
        context = base_context(markets={"SOL": deep_market("SOL", as_of=NOW - age)})
        verdict = evaluate(make_card(), context)
        assert verdict.verdict == "approved", (age, verdict.rule_fired)


def test_the_raw_snapshot_staleness_flag_is_honoured() -> None:
    """``RawSnapshotPayload.is_stale`` now reaches the gate instead of stopping at the ledger."""
    context = base_context(markets={"SOL": deep_market("SOL", is_stale=True)})
    verdict = evaluate(make_card(), context)
    assert (verdict.verdict, verdict.rule_fired) == ("rejected", RULE_STALE_MARKET_DATA)


def test_a_missing_timestamp_is_not_guessed() -> None:
    """No ``as_of`` is unknown rather than fresh: the gate refuses staleness it was told about."""
    verdict = evaluate(make_card(), base_context())
    assert (verdict.verdict, verdict.rule_fired) == ("approved", None)


def test_the_context_can_tighten_the_staleness_limit() -> None:
    five_minutes_old = {"SOL": deep_market("SOL", as_of=NOW - timedelta(minutes=5))}
    tight = evaluate(make_card(), base_context(markets=five_minutes_old, market_data_max_age_s=60.0))
    assert (tight.verdict, tight.rule_fired) == ("rejected", RULE_STALE_MARKET_DATA)
    loose = evaluate(
        make_card(),
        base_context(markets=five_minutes_old, market_data_max_age_s=4 * 60 * 60.0),
    )
    assert loose.verdict == "approved"


def test_a_stale_snapshot_is_the_first_market_rule() -> None:
    """An old snapshot is not even a market: staleness is checked before tradability and spread."""
    context = base_context(
        markets={
            "SOL": deep_market(
                "SOL",
                as_of=NOW - timedelta(hours=4),
                volume_24h_usd=1.0,
                spread_pct=9.0,
                tradable=False,
            )
        }
    )
    verdict = evaluate(make_card(), context)
    assert verdict.rule_fired == RULE_STALE_MARKET_DATA


def test_stale_data_does_not_block_a_reduction_or_a_stop() -> None:
    """Stale market data stops new exposure; it does not trap a position."""
    stale = {"SOL": deep_market("SOL", is_stale=True)}
    for action in (Action.TRIM, Action.EXIT, Action.SET_STOP, Action.TRAIL_STOP):
        verdict = evaluate(make_card(action=action, size_pct=4.0), base_context(markets=stale))
        assert verdict.verdict == "approved", (action, verdict.rule_fired)
