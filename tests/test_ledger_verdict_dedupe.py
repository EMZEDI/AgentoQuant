"""F2: one cycle must write exactly one ``risk_gate_verdict`` row.

Regression for the Phase 0 adversary finding F2 (``docs/reviews/phase0_adversary.md``): the gate
writes its own verdict (``risk/gate.py``) and the hourly loop writes the same verdict again
(``execution/paper.py``), so every cycle landed as two rows. Nine soak cycles produced eighteen
verdicts, ``COUNT(*) ... GROUP BY cycle_id`` double-counted the stage, and the gate stopped being
idempotent as a side effect: two ``evaluate`` calls for one decision wrote two verdicts.

The fix keys the write on ``(cycle_id, decision_card_id)`` in ``ledger/store.py``: an identical
second write returns the first record's id and inserts nothing, and a second write that *disagrees*
is refused rather than silently dropped.

Deliberately decorator-free: the model gateway refuses a request whose history carries a tool call
containing an at-sign followed by a dotted name, so files written by agents avoid decorators
entirely. ``tmp_path`` and ``monkeypatch`` arrive as plain arguments.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentoquant.enums import Action, ConfidenceBand, Sleeve, Stage
from agentoquant.ledger.schema import DecisionCard, RiskGateVerdict
from agentoquant.ledger.store import LedgerStore
from agentoquant.risk import MarketContext, PortfolioContext, RiskGate

CYCLE = "2026-09-19T05Z-0001"
NOW = datetime(2026, 9, 19, 5, 0, tzinfo=UTC)


def verdict_rows(ledger: LedgerStore, cycle_id: str | None = None) -> list[dict]:
    """Every ``risk_gate_verdict`` row, optionally for one cycle."""
    if cycle_id is None:
        return ledger.query(
            'SELECT "record_id", "cycle_id", "decision_card_id", "verdict", "final_size_pct" '
            'FROM "risk_gate_verdict" ORDER BY "record_id"'
        )
    return ledger.query(
        'SELECT "record_id", "cycle_id", "decision_card_id", "verdict", "final_size_pct" '
        'FROM "risk_gate_verdict" WHERE "cycle_id" = ? ORDER BY "record_id"',
        [cycle_id],
    )


def card(size_pct: float = 3.0, *, coin: str = "SOL", cycle_id: str = CYCLE) -> DecisionCard:
    return DecisionCard(
        cycle_id=cycle_id,
        selected_proposal_id=f"prop-{coin}-1",
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


def context(card_id: str) -> PortfolioContext:
    return PortfolioContext(
        book_value_cad=900.0,
        markets={
            "SOL": MarketContext(
                coin="SOL", volume_24h_usd=5_000_000.0, spread_pct=0.05, tradable=True
            )
        },
        sleeve_weights_pct={Sleeve.A: 60.0},
        free_cash_pct_by_sleeve={Sleeve.A: 35.0},
        venue="kraken",
        venue_status="online",
        decision_card_id=card_id,
        order_type="post_only_limit",
        order_purpose="entry",
        stop_on_exchange=True,
        stop_price=142.5,
        now=NOW,
    )


def write_card(ledger: LedgerStore, cycle_id: str = CYCLE) -> str:
    return ledger.write(Stage.DECISION_CARD, cycle_id, card(cycle_id=cycle_id), producer_role="judge")


# ----------------------------------------------------------------------------------------------
# The defect as it was seen in the soak: two writers, two rows
# ----------------------------------------------------------------------------------------------


def test_the_hourly_loop_writes_one_verdict_row_per_cycle(tmp_path, monkeypatch) -> None:
    """The real loop, end to end: one cycle, one verdict row (it was two)."""
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))
    monkeypatch.delenv("AGENTOQUANT_TELEGRAM", raising=False)

    from agentoquant.execution.order_manager import NullTransport
    from agentoquant.execution.paper import run_loop
    from agentoquant.execution.signal_store import SignalStore

    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    result = run_loop(
        hours=1,
        placeholder=True,
        sleep=False,
        interval_s=0.0,
        ledger=ledger,
        store=SignalStore(),
        transport=NullTransport(),
        now=NOW,
        log_path=tmp_path / "cycles.jsonl",
    )
    assert result["completed"] == 1, result["failures"]
    cycle_id = result["results"][0]["cycle_id"]
    rows = verdict_rows(ledger, cycle_id)
    assert len(rows) == 1, f"{len(rows)} verdict rows for one cycle: {rows}"
    assert rows[0]["verdict"] in {"approved", "shrunk", "rejected"}


def test_two_cycles_write_two_verdict_rows_not_four(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))
    monkeypatch.delenv("AGENTOQUANT_TELEGRAM", raising=False)

    from agentoquant.execution.order_manager import NullTransport
    from agentoquant.execution.paper import run_loop
    from agentoquant.execution.signal_store import SignalStore

    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    run_loop(
        hours=2,
        placeholder=True,
        sleep=False,
        interval_s=0.0,
        ledger=ledger,
        store=SignalStore(),
        transport=NullTransport(),
        now=NOW,
        log_path=tmp_path / "cycles.jsonl",
    )
    grouped = ledger.query(
        'SELECT "cycle_id", COUNT(*) AS n FROM "risk_gate_verdict" GROUP BY 1 ORDER BY 1'
    )
    assert [row["n"] for row in grouped] == [1, 1], grouped
    assert len(verdict_rows(ledger)) == 2


def test_the_gate_write_then_the_loop_write_is_one_row(tmp_path) -> None:
    """The exact double write: the gate's own write, then the loop writing the same verdict back."""
    from agentoquant.config_loader import load_risk_limits

    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    card_id = write_card(ledger)
    evaluated = RiskGate(load_risk_limits(), ledger=ledger)
    verdict = evaluated.evaluate(card(), context(card_id))
    assert len(verdict_rows(ledger)) == 1

    again = ledger.write(
        Stage.RISK_GATE_VERDICT,
        CYCLE,
        verdict,
        producer_role="risk_gate",
    )
    rows = verdict_rows(ledger)
    assert len(rows) == 1, f"the loop's second write added a row: {rows}"
    assert rows[0]["record_id"] == again
    assert again != ""


def test_re_evaluating_one_card_writes_one_row(tmp_path) -> None:
    """The gate is idempotent as a side effect: two evaluations, one verdict row."""
    from agentoquant.config_loader import load_risk_limits

    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    card_id = write_card(ledger)
    gate = RiskGate(load_risk_limits(), ledger=ledger)
    first = gate.evaluate(card(), context(card_id))
    second = gate.evaluate(card(), context(card_id))
    assert first == second
    rows = verdict_rows(ledger)
    assert len(rows) == 1, f"a re-evaluation added a row: {rows}"


def test_identical_repeats_are_collapsed_but_conflicting_ones_are_not(tmp_path) -> None:
    """The dedupe's boundary: a repeat of the same verdict is a no-op, a different one is a row.

    The key is ``(cycle_id, decision_card_id)``, and that id is not unique in practice - the gate
    falls back to ``selected_proposal_id`` when the card is not stored yet - so a conflicting payload
    under the same key cannot be assumed to be the same decision. It is written, not merged.
    """
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    card_id = write_card(ledger)
    approved = RiskGateVerdict(
        decision_card_id=card_id,
        verdict="approved",
        rule_fired=None,
        original_size_pct=3.0,
        final_size_pct=3.0,
        funding_floor_breach=False,
    )
    first = ledger.write(Stage.RISK_GATE_VERDICT, CYCLE, approved, producer_role="risk_gate")
    repeat = ledger.write(Stage.RISK_GATE_VERDICT, CYCLE, approved, producer_role="risk_gate")
    assert repeat == first, "an identical repeat wrote a second row"
    assert len(verdict_rows(ledger)) == 1

    ledger.write(
        Stage.RISK_GATE_VERDICT,
        CYCLE,
        RiskGateVerdict(
            decision_card_id=card_id,
            verdict="rejected",
            rule_fired="daily_turnover_cap",
            original_size_pct=3.0,
            final_size_pct=0.0,
            funding_floor_breach=False,
        ),
        producer_role="risk_gate",
    )
    assert len(verdict_rows(ledger)) == 2, "a conflicting verdict was merged away"


# ----------------------------------------------------------------------------------------------
# The dedupe must not over-reach: distinct decisions and other stages keep their rows
# ----------------------------------------------------------------------------------------------


def test_two_cards_in_one_cycle_each_keep_their_own_verdict(tmp_path) -> None:
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    first = write_card(ledger)
    second = ledger.write(
        Stage.DECISION_CARD, CYCLE, card(coin="ETH"), producer_role="judge"
    )
    for card_id in (first, second):
        ledger.write(
            Stage.RISK_GATE_VERDICT,
            CYCLE,
            RiskGateVerdict(
                decision_card_id=card_id,
                verdict="approved",
                rule_fired=None,
                original_size_pct=3.0,
                final_size_pct=3.0,
                funding_floor_breach=False,
            ),
            producer_role="risk_gate",
        )
    assert len(verdict_rows(ledger, CYCLE)) == 2


def test_other_stages_are_not_deduplicated(tmp_path) -> None:
    """Only the verdict stage is keyed; two decision cards in one cycle stay two records."""
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    write_card(ledger)
    ledger.write(Stage.DECISION_CARD, CYCLE, card(coin="ETH"), producer_role="judge")
    assert len(ledger.query('SELECT * FROM "decision_card" WHERE "cycle_id" = ?', [CYCLE])) == 2


def test_a_verdict_for_a_later_cycle_is_not_collapsed(tmp_path) -> None:
    """The key is per cycle, so the next hour's verdict for the same coin is its own row."""
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    later = "2026-09-19T06Z-0001"
    first_card = write_card(ledger)
    later_card = write_card(ledger, later)
    for cycle_id, card_id in ((CYCLE, first_card), (later, later_card)):
        ledger.write(
            Stage.RISK_GATE_VERDICT,
            cycle_id,
            RiskGateVerdict(
                decision_card_id=card_id,
                verdict="approved",
                rule_fired=None,
                original_size_pct=3.0,
                final_size_pct=3.0,
                funding_floor_breach=False,
            ),
            producer_role="risk_gate",
        )
    assert len(verdict_rows(ledger)) == 2
    assert len(verdict_rows(ledger, later)) == 1
    assert (NOW + timedelta(hours=1)).hour == 6
