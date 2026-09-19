"""Task 5 acceptance check: the gate never enlarges, the kill switch closes and blocks, and
every verdict (including a funding-floor breach) lands in the ledger with the rule that fired.

Real DuckDB and the real committed config, no mocks.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentoquant.config_loader import load_risk_limits, load_sleeves
from agentoquant.enums import Action, ConfidenceBand, Sleeve
from agentoquant.ledger.schema import DecisionCard
from agentoquant.ledger.store import LedgerStore
from agentoquant.risk import (
    VERDICT_APPROVED,
    VERDICT_REJECTED,
    VERDICT_SHRUNK,
    KillSwitch,
    MarketContext,
    PortfolioContext,
    PositionContext,
    RiskGate,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
CYCLE = "2026-09-19T12Z-0001"

limits = load_risk_limits()
sleeves = load_sleeves()

tmp = Path(tempfile.mkdtemp(prefix="task5-accept-"))
ledger = LedgerStore(db_path=tmp / "ledger.duckdb")

gate = RiskGate(limits, ledger)
ks = KillSwitch(limits, ledger=ledger, now=lambda: NOW)


def market(coin: str, **kw) -> MarketContext:
    base = dict(coin=coin, volume_24h_usd=50_000_000.0, spread_pct=0.05, tradable=True)
    base.update(kw)
    return MarketContext(**base)


def card(**kw) -> DecisionCard:
    base = dict(
        cycle_id=CYCLE,
        selected_proposal_id="PROPOSAL-0001",
        action=Action.ENTER_LADDERED,
        coin="SOL",
        sleeve=Sleeve.A,
        size_pct=5.0,
        confidence=70,
        confidence_band=ConfidenceBand.STANDARD,
        p_up=0.61,
        interval_low=0.54,
        interval_high=0.68,
        ev_after_fees=0.42,
        fee_tier_assumed="Tier 1",
        evidence_split={"primary": 2, "verified": 1, "unverified": 0},
        strongest_objection={"severity": 2, "text": "fee drag at Tier 1"},
        flip_condition="BTC closes below its 50-day moving average",
    )
    base.update(kw)
    return DecisionCard(**base)


def context(**kw) -> PortfolioContext:
    base = dict(
        book_value_cad=900.0,
        positions=(PositionContext("BTC", Sleeve.A, 10.0, stop_on_exchange=True),),
        sleeve_weights_pct={Sleeve.A: 40.0, Sleeve.B: 10.0, Sleeve.C: 0.0},
        free_cash_pct_by_sleeve={Sleeve.A: 30.0, Sleeve.B: 10.0, Sleeve.C: 5.0},
        markets={"SOL": market("SOL"), "BTC": market("BTC"), "TAO": market("TAO")},
        daily_turnover_used_pct=0.0,
        daily_loss_used_pct=0.0,
        drawdown_used_pct=0.0,
        consecutive_losses=0,
        venue="kraken",
        venue_status="online",
        order_type="post_only_limit",
        order_purpose="entry",
        stop_on_exchange=True,
        stop_price=118.5,
        now=NOW,
    )
    base.update(kw)
    return PortfolioContext(**base)


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


# --- Criterion 1: the gate can only shrink or reject, never enlarge -----------------
base = gate.evaluate(card(), context())
check(
    "C1 a compliant card is approved and unchanged",
    base.verdict == VERDICT_APPROVED and base.final_size_pct == base.original_size_pct,
    f"{base.verdict} {base.final_size_pct}%",
)

oversized = gate.evaluate(card(size_pct=25.0), context())
check(
    "C1 oversized 25% request is shrunk to the 15% position cap",
    oversized.verdict == VERDICT_SHRUNK and oversized.final_size_pct <= 15.0,
    f"{oversized.verdict} -> {oversized.final_size_pct}% rule={oversized.rule_fired}",
)

naked = gate.evaluate(card(), context(stop_price=None, stop_on_exchange=False))
check(
    "C1 no-stop proposal is rejected",
    naked.verdict == VERDICT_REJECTED,
    f"{naked.verdict} rule={naked.rule_fired}",
)

taker = gate.evaluate(card(), context(order_type="market"))
check(
    "C1 taker/market entry is rejected",
    taker.verdict == VERDICT_REJECTED,
    f"{taker.verdict} rule={taker.rule_fired}",
)

thin = gate.evaluate(card(), context(markets={"SOL": market("SOL", volume_24h_usd=500_000.0)}))
check(
    "C1 illiquid coin is rejected",
    thin.verdict == VERDICT_REJECTED,
    f"{thin.verdict} rule={thin.rule_fired}",
)

over_turnover = gate.evaluate(card(), context(daily_turnover_used_pct=22.0))
check(
    "C1 over the daily turnover cap is rejected",
    over_turnover.verdict == VERDICT_REJECTED,
    f"{over_turnover.verdict} rule={over_turnover.rule_fired}",
)

verdicts = [base, oversized, naked, taker, thin, over_turnover]
enlarged = [v for v in verdicts if v.final_size_pct > v.original_size_pct]
check(
    "C1 no verdict enlarged a proposal (the invariant)",
    not enlarged,
    f"{len(verdicts)} verdicts checked, {len(enlarged)} violations",
)

# --- Criterion 2: /flat and the daily halt close positions and block entries --------
positions = (
    PositionContext("BTC", Sleeve.A, 10.0, stop_on_exchange=True),
    PositionContext("TAO", Sleeve.B, 8.0, stop_on_exchange=True),
)
plan = ks.trigger_flat(positions=positions, cycle_id=CYCLE, actor="telegram:/flat")
check(
    "C2 /flat plans a close for every open position",
    tuple(plan.positions_to_close) == ("BTC", "TAO") and plan.entries_blocked,
    f"closes={plan.positions_to_close} entries_blocked={plan.entries_blocked}",
)
check(
    "C2 /flat blocks new entries",
    ks.is_entry_blocked(),
    f"halt={ks.halt_state().reasons}",
)

flat_card = gate.evaluate(card(), context(halts=ks.halt_state(), positions=positions))
check(
    "C2 an entry under /flat is rejected by the kill-switch rule",
    flat_card.verdict == VERDICT_REJECTED and flat_card.rule_fired == "kill_switch_flat",
    f"{flat_card.verdict} rule={flat_card.rule_fired}",
)

reducing = gate.evaluate(
    card(action=Action.TRIM, size_pct=3.0),
    context(halts=ks.halt_state(), positions=positions),
)
check(
    "C2 reductions are still allowed while flat (never blocked)",
    reducing.verdict != VERDICT_REJECTED,
    f"{reducing.verdict} rule={reducing.rule_fired}",
)

ks.resume(cycle_id=CYCLE, actor="telegram:/resume")
check("C2 /resume clears the halt", not ks.is_entry_blocked(), f"halt={ks.halt_state().reasons}")

daily = ks.trigger_daily_halt(reason="daily loss halt", positions=positions, cycle_id=CYCLE)
check(
    "C2 daily halt blocks entries, closes positions and expires on the next reset",
    ks.is_entry_blocked()
    and daily.daily_halt_expires_at is not None
    and tuple(daily.positions_to_close) == ("BTC", "TAO"),
    f"expires {daily.daily_halt_expires_at} closes={daily.positions_to_close}",
)

weekly = ks.trigger_weekly_halt(reason="weekly drawdown", cycle_id=CYCLE)
auto = ks.resume(cycle_id=CYCLE, actor="scheduler", human=False)
check(
    "C2 the weekly drawdown halt survives an automatic resume (human restart required)",
    weekly.entries_blocked and auto.entries_blocked,
    f"after auto-resume: blocked={auto.entries_blocked} reasons={auto.reasons}",
)
ks.resume(cycle_id=CYCLE, actor="telegram:/resume")
check("C2 a human restart clears the weekly halt", not ks.is_entry_blocked(), "cleared")

# --- Criterion 3: every verdict, including a floor breach, is in the ledger ---------
breach = gate.evaluate(
    card(coin="TAO", sleeve=Sleeve.B, size_pct=6.0, confidence=70),
    context(
        positions=positions,
        free_cash_pct_by_sleeve={Sleeve.A: 30.0, Sleeve.B: 0.5, Sleeve.C: 0.0},
    ),
)
check(
    "C3 a free-cash floor breach raises a funding request",
    breach.funding_floor_breach,
    f"rule={breach.rule_fired} breach={breach.funding_floor_breach}",
)

rows = ledger.query('SELECT * FROM "risk_gate_verdict"')
rules = {r["rule_fired"] for r in rows if r.get("rule_fired")}
check(
    "C3 every verdict is in the ledger with the rule that fired",
    len(rows) >= len(verdicts) and "kill_switch_flat" in rules,
    f"{len(rows)} verdict rows, {len(rules)} distinct rules fired",
)

funding = ledger.query('SELECT * FROM "funding_request"')
check(
    "C3 the Funding Request is in the ledger",
    len(funding) >= 1,
    f"{len(funding)} rows: " + ", ".join(str(r.get("reason")) for r in funding),
)

# --- report ------------------------------------------------------------------------
width = max(len(n) for n, _, _ in results)
failed = 0
print(f"\nTask 5 acceptance  (ledger: {len(rows)} verdicts, {len(funding)} funding requests)")
print(f"scratch ledger: {tmp}\n")
for name, ok, detail in results:
    if not ok:
        failed += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"         {detail}")

print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
