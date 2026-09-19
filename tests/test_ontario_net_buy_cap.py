"""F9: the Ontario net-buy cap must bind when the ledger carries a real fill.

Regression for the Phase 0 adversary finding F9 (``docs/reviews/phase0_adversary.md``): the gate's
``ontario_net_buys_cad_12m`` SQL requires ``fill_price`` and ``fill_qty`` to be non-null, and F1
leaves both null on **every** execution row, so the 12-month total was permanently zero and the cap
was decorative. ``config/risk_limits.yaml`` says it is "enforced by the gate while Shahrad is a
Canadian resident", so that is a compliance gap, not a reporting one.

F1 itself (the fill write-back) is owned by another agent in ``agentoquant/execution/`` and is **not**
fixed here. What these tests do is make the cap's own code path correct and testable given a real
fill, and pin both halves of the story:

* with a real fill in the ledger the cumulative total is non-zero and the cap **binds** - outright
  (rejected) when the cumulative is at or over the limit, and by shrinking when only some headroom is
  left, with the headroom expressed in percent of book value;
* with null fills (today's shape) the total is zero and the cap cannot bind - the F1 dependency,
  asserted rather than assumed, so the day F1 lands this test says what changed;
* the end-to-end check runs the **real hourly loop** against a seeded ledger and shows the loop's own
  gate rejects a buy over the cap, so nothing else on the loop's path blocks the cap once fills exist
  (the loop constructs ``RiskGate(limits, ledger=ledger)`` and passes no ``ontario_net_buys_cad_12m``,
  so the gate's ledger-derived branch is the live one).

Deliberately decorator-free: the model gateway refuses a request whose history carries a tool call
containing an at-sign followed by a dotted name, so files written by agents avoid decorators
entirely. ``tmp_path`` and ``monkeypatch`` arrive as plain arguments.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentoquant.config_loader import load_risk_limits
from agentoquant.enums import Action, ConfidenceBand, Sleeve, Stage
from agentoquant.ledger.schema import DecisionCard, ExecutionPayload
from agentoquant.ledger.store import LedgerStore
from agentoquant.risk.gate import (
    INCREASING_ACTIONS,
    ONTARIO_EXEMPT_COINS,
    RULE_ONTARIO_NET_BUY_CAP,
    MarketContext,
    PortfolioContext,
    RiskGate,
    ontario_net_buys_cad_12m,
)

CAP_CAD = float(load_risk_limits().regulatory.ontario_net_buy_cap_cad)  # 30,000
BOOK_CAD = 900.0


# ----------------------------------------------------------------------------------------------
# Fixtures as plain helpers
# ----------------------------------------------------------------------------------------------


def make_card(ledger: LedgerStore, cycle_id: str, coin: str, *, size_pct: float = 3.0) -> str:
    return ledger.write(
        Stage.DECISION_CARD,
        cycle_id,
        DecisionCard(
            cycle_id=cycle_id,
            selected_proposal_id=f"prop-{cycle_id}",
            action=Action.ENTER_LADDERED,
            coin=coin,
            sleeve=Sleeve.A,
            size_pct=size_pct,
            confidence=70,
            confidence_band=ConfidenceBand.STANDARD,
            p_up=0.6,
            interval_low=0.5,
            interval_high=0.7,
            ev_after_fees=0.01,
            fee_tier_assumed="tier1",
            evidence_split={"primary": 1, "verified": 0, "unverified": 0},
            strongest_objection=None,
            flip_condition="a close below the 20h mean",
        ),
        producer_role="judge",
    )


def make_fill(
    ledger: LedgerStore,
    cycle_id: str,
    card_id: str,
    *,
    fill_price: float | None,
    fill_qty: float | None,
    fee_paid: float | None = 0.6,
    status: str = "filled",
) -> str:
    """One execution row. ``None`` fills are the Phase 0 shape (F1); real values are the target."""
    return ledger.write(
        Stage.EXECUTION,
        cycle_id,
        ExecutionPayload(
            decision_card_id=card_id,
            order_type="post_only_limit",
            venue="kraken",
            fill_price=fill_price,
            fill_qty=fill_qty,
            fee_paid=fee_paid,
            reprice_count=0,
            status=status,
        ),
        producer_role="order_manager",
    )


def backdate(ledger: LedgerStore, table: str, record_id: str, moment: datetime) -> None:
    ledger.query(f'UPDATE "{table}" SET "ts" = ? WHERE "record_id" = ?', [moment, record_id])


def gate_context(*, now: datetime, coin: str, usd_cad_rate: float = 1.0) -> PortfolioContext:
    return PortfolioContext(
        book_value_cad=BOOK_CAD,
        markets={
            coin: MarketContext(
                coin=coin, volume_24h_usd=5_000_000.0, spread_pct=0.05, tradable=True
            )
        },
        sleeve_weights_pct={Sleeve.A: 60.0},
        free_cash_pct_by_sleeve={Sleeve.A: 35.0},
        venue="kraken",
        venue_status="online",
        order_type="post_only_limit",
        order_purpose="entry",
        stop_on_exchange=True,
        stop_price=142.5,
        usd_cad_rate=usd_cad_rate,
        now=now,
    )


def buy_card(coin: str, *, size_pct: float = 3.0) -> DecisionCard:
    return DecisionCard(
        cycle_id="2026-09-19T05Z-0001",
        selected_proposal_id="prop-sol-1",
        action=Action.ENTER_LADDERED,
        coin=coin,
        sleeve=Sleeve.A,
        size_pct=size_pct,
        confidence=70,
        confidence_band=ConfidenceBand.STANDARD,
        p_up=0.6,
        interval_low=0.5,
        interval_high=0.7,
        ev_after_fees=0.01,
        fee_tier_assumed="tier1",
        evidence_split={"primary": 1, "verified": 0, "unverified": 0},
        strongest_objection=None,
        flip_condition="a close below the 20h mean",
    )


def hour_with_an_ontario_buy() -> tuple[datetime, str]:
    """An hour whose placeholder cycle is a buy on a coin the Ontario cap applies to."""
    from agentoquant.execution.freqtrade_strategy import placeholder_card
    from agentoquant.execution.paper import sequence_for

    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    for offset in range(200):
        moment = base + timedelta(hours=offset)
        card = placeholder_card("probe-cycle", sequence=sequence_for(moment))
        coin = (card.coin or "").upper()
        if card.action.value in INCREASING_ACTIONS and coin not in ONTARIO_EXEMPT_COINS:
            return moment, coin
    raise AssertionError("no hour in the next 200 ticks buys a non-exempt coin")


# ----------------------------------------------------------------------------------------------
# The defect: null fills make the cap inert (the F1 dependency, asserted)
# ----------------------------------------------------------------------------------------------


def test_null_fills_leave_the_cap_unable_to_bind(tmp_path) -> None:
    """Today's shape: no fill anywhere, so the cumulative is zero and a buy is approved."""
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    card_id = make_card(ledger, "2026-09-19T05Z-0001", "SOL")
    make_fill(ledger, "2026-09-19T05Z-0001", card_id, fill_price=None, fill_qty=None, fee_paid=None,
              status="unfilled_timeout")

    assert ontario_net_buys_cad_12m(ledger, as_of=now) == {}
    gate = RiskGate(load_risk_limits(), ledger=ledger)
    verdict = gate.evaluate(buy_card("SOL"), gate_context(now=now, coin="SOL"))
    assert verdict.verdict == "approved"
    assert verdict.rule_fired is None
    assert verdict.final_size_pct == 3.0


# ----------------------------------------------------------------------------------------------
# Given a real fill: the total is real and the cap binds
# ----------------------------------------------------------------------------------------------


def test_a_real_fill_makes_the_ontario_total_non_zero(tmp_path) -> None:
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-19T05Z-0001"
    card_id = make_card(ledger, cycle_id, "SOL")
    make_fill(ledger, cycle_id, card_id, fill_price=150.0, fill_qty=100.0)

    totals = ontario_net_buys_cad_12m(ledger, as_of=now)
    assert totals == {"SOL": 15_000.0}, totals
    assert ontario_net_buys_cad_12m(ledger, as_of=now, usd_cad_rate=1.37) == {"SOL": 20_550.0}


def test_the_cap_rejects_a_buy_once_the_cumulative_reaches_it(tmp_path) -> None:
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-19T05Z-0001"
    card_id = make_card(ledger, cycle_id, "SOL")
    make_fill(ledger, cycle_id, card_id, fill_price=150.0, fill_qty=CAP_CAD / 150.0)

    gate = RiskGate(load_risk_limits(), ledger=ledger)
    verdict = gate.evaluate(buy_card("SOL"), gate_context(now=now, coin="SOL"))
    assert verdict.verdict == "rejected", verdict
    assert verdict.rule_fired == RULE_ONTARIO_NET_BUY_CAP
    assert verdict.final_size_pct == 0.0
    assert verdict.final_size_pct <= verdict.original_size_pct


def test_the_cap_shrinks_a_buy_when_headroom_is_left(tmp_path) -> None:
    """29,940 of a 30,000 cap leaves 60 CAD of headroom = 6.667 percent of a 900 CAD book."""
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-19T05Z-0001"
    card_id = make_card(ledger, cycle_id, "SOL")
    make_fill(ledger, cycle_id, card_id, fill_price=150.0, fill_qty=199.6)

    gate = RiskGate(load_risk_limits(), ledger=ledger)
    verdict = gate.evaluate(buy_card("SOL", size_pct=10.0), gate_context(now=now, coin="SOL"))
    expected = round(60.0 / BOOK_CAD * 100.0, 6)
    assert verdict.verdict == "shrunk", verdict
    assert verdict.rule_fired == RULE_ONTARIO_NET_BUY_CAP
    assert round(verdict.final_size_pct, 6) == expected
    assert verdict.final_size_pct < verdict.original_size_pct


def test_the_cap_uses_the_cad_conversion_the_context_carries(tmp_path) -> None:
    """At a 1.37 rate the same fill is worth more CAD, so the cap binds sooner."""
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-19T05Z-0001"
    card_id = make_card(ledger, cycle_id, "SOL")
    make_fill(ledger, cycle_id, card_id, fill_price=150.0, fill_qty=150.0)  # 22,500 USD

    gate = RiskGate(load_risk_limits(), ledger=ledger)
    at_parity = gate.evaluate(buy_card("SOL", size_pct=10.0), gate_context(now=now, coin="SOL"))
    assert at_parity.verdict == "approved", at_parity  # 22,500 + 90 < 30,000

    converted = gate.evaluate(
        buy_card("SOL", size_pct=10.0), gate_context(now=now, coin="SOL", usd_cad_rate=1.37)
    )
    assert converted.verdict == "shrunk", converted  # 30,825 CAD is over the cap
    assert converted.rule_fired == RULE_ONTARIO_NET_BUY_CAP


def test_exempt_coins_and_old_fills_do_not_count(tmp_path) -> None:
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-19T05Z-0001"
    btc = make_card(ledger, cycle_id, "BTC")
    make_fill(ledger, cycle_id, btc, fill_price=60_000.0, fill_qty=1.0)

    old = "2025-01-01T00Z-0001"
    old_card = make_card(ledger, old, "SOL")
    old_execution = make_fill(ledger, old, old_card, fill_price=150.0, fill_qty=400.0)
    backdate(ledger, "decision_card", old_card, now - timedelta(days=400))
    backdate(ledger, "execution", old_execution, now - timedelta(days=400))

    assert ontario_net_buys_cad_12m(ledger, as_of=now) == {}
    gate = RiskGate(load_risk_limits(), ledger=ledger)
    verdict = gate.evaluate(buy_card("BTC"), gate_context(now=now, coin="BTC"))
    assert verdict.verdict == "approved"
    assert verdict.rule_fired is None
    assert "BTC" in ONTARIO_EXEMPT_COINS


def test_a_sell_subtracts_from_the_net_buy_total(tmp_path) -> None:
    """The window is a *net* buy window: trim and exit subtract."""
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    buy_cycle = "2026-09-19T05Z-0001"
    buy = make_card(ledger, buy_cycle, "SOL")
    make_fill(ledger, buy_cycle, buy, fill_price=150.0, fill_qty=200.0)

    sell_cycle = "2026-09-19T06Z-0001"
    sell = ledger.write(
        Stage.DECISION_CARD,
        sell_cycle,
        DecisionCard(
            cycle_id=sell_cycle,
            selected_proposal_id="prop-trim-1",
            action=Action.TRIM,
            coin="SOL",
            sleeve=Sleeve.A,
            size_pct=1.5,
            confidence=70,
            confidence_band=ConfidenceBand.STANDARD,
            p_up=0.5,
            interval_low=0.4,
            interval_high=0.6,
            ev_after_fees=0.0,
            fee_tier_assumed="tier1",
            evidence_split={"primary": 1, "verified": 0, "unverified": 0},
            strongest_objection=None,
            flip_condition="a close below the 20h mean",
        ),
        producer_role="judge",
    )
    make_fill(ledger, sell_cycle, sell, fill_price=150.0, fill_qty=50.0)

    totals = ontario_net_buys_cad_12m(ledger, as_of=now)
    assert totals == {"SOL": 22_500.0}, totals  # 200 bought, 50 sold


# ----------------------------------------------------------------------------------------------
# End to end: the real hourly loop's own gate applies the cap
# ----------------------------------------------------------------------------------------------


def test_the_hourly_loop_rejects_a_buy_over_the_cap_when_the_ledger_has_fills(tmp_path, monkeypatch) -> None:
    """Nothing else on the loop's path blocks the cap once the ledger carries fills."""
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))
    monkeypatch.delenv("AGENTOQUANT_TELEGRAM", raising=False)

    from agentoquant.execution.order_manager import NullTransport
    from agentoquant.execution.paper import run_loop
    from agentoquant.execution.signal_store import SignalStore

    moment, coin = hour_with_an_ontario_buy()
    ledger = LedgerStore(tmp_path / "ledger.duckdb")

    # The cumulative history: one filled buy of the same coin, already over the cap.
    seeded_cycle = "2026-09-18T00Z-0001"
    seeded_card = make_card(ledger, seeded_cycle, coin)
    seeded_execution = make_fill(
        ledger, seeded_cycle, seeded_card, fill_price=150.0, fill_qty=CAP_CAD / 150.0
    )
    backdate(ledger, "decision_card", seeded_card, datetime.now(UTC) - timedelta(hours=24))
    backdate(ledger, "execution", seeded_execution, datetime.now(UTC) - timedelta(hours=24))

    result = run_loop(
        hours=1,
        placeholder=True,
        sleep=False,
        interval_s=0.0,
        ledger=ledger,
        store=SignalStore(),
        transport=NullTransport(),
        now=moment,
        log_path=tmp_path / "cycles.jsonl",
    )
    assert result["completed"] == 1, result["failures"]
    summary = result["results"][0]
    assert summary["action"] == "enter_laddered", summary
    assert summary["coin"] == coin, summary
    assert summary["verdict"] == "rejected", summary
    assert summary["rule_fired"] == RULE_ONTARIO_NET_BUY_CAP, summary
    assert summary["size_pct"] == 0.0
    assert summary["orders"] == 0, "a rejected card must place nothing"

    rows = ledger.query(
        'SELECT "cycle_id", COUNT(*) AS n FROM "risk_gate_verdict" WHERE "cycle_id" = ? GROUP BY 1',
        [summary["cycle_id"]],
    )
    assert rows == [{"cycle_id": summary["cycle_id"], "n": 1}], rows


def test_the_same_loop_approves_when_the_ledger_carries_no_fills(tmp_path, monkeypatch) -> None:
    """The control: identical cycle, no fills in the ledger, so the cap is inert (F1)."""
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))
    monkeypatch.delenv("AGENTOQUANT_TELEGRAM", raising=False)

    from agentoquant.execution.order_manager import NullTransport
    from agentoquant.execution.paper import run_loop
    from agentoquant.execution.signal_store import SignalStore

    moment, coin = hour_with_an_ontario_buy()
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    seeded_cycle = "2026-09-18T00Z-0001"
    seeded_card = make_card(ledger, seeded_cycle, coin)
    seeded_execution = make_fill(
        ledger, seeded_cycle, seeded_card, fill_price=None, fill_qty=None, status="unfilled_timeout"
    )
    backdate(ledger, "decision_card", seeded_card, datetime.now(UTC) - timedelta(hours=24))
    backdate(ledger, "execution", seeded_execution, datetime.now(UTC) - timedelta(hours=24))

    result = run_loop(
        hours=1,
        placeholder=True,
        sleep=False,
        interval_s=0.0,
        ledger=ledger,
        store=SignalStore(),
        transport=NullTransport(),
        now=moment,
        log_path=tmp_path / "cycles.jsonl",
    )
    summary = result["results"][0]
    assert summary["action"] == "enter_laddered", summary
    assert summary["verdict"] == "approved", summary
    assert summary["rule_fired"] is None
