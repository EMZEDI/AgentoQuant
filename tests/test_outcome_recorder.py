"""F3: the outcome stage must have a writer, and it must be idempotent.

Regression for the Phase 0 adversary finding F3 (``docs/reviews/phase0_adversary.md``): ``Stage.OUTCOME``
was declared and ``LedgerStore.due_outcomes`` / ``write_outcome`` existed, but **nothing called them**.
No +1h, +4h or +24h record was ever produced, for executed, rejected or vetoed proposals, so a cycle
could run to completion and leave the ledger with no outcome row at all.

These tests drive the real recorder (``agentoquant.ledger.outcomes``) over a scratch ledger and assert
the three properties the fix promises:

* a due horizon is written **exactly once**, however many times the pass runs;
* a horizon that has not elapsed is **not** written;
* executed, rejected and vetoed cards all get a row, with ``still_open`` set from the ledger's own
  evidence and PnL filled when a price source is supplied.

The store stamps ``ts`` at write time, so a test that needs a *past* cycle backdates the column
directly (``backdate`` below): the recorder's clock is ``as_of``, but the cards it reads must be old
enough for their horizons to have elapsed.

Deliberately decorator-free: the model gateway refuses a request whose history carries a tool call
containing an at-sign followed by a dotted name, so files written by agents avoid decorators
entirely. ``tmp_path`` and ``monkeypatch`` arrive as plain arguments.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentoquant.enums import Action, ConfidenceBand, Sleeve, Stage
from agentoquant.ledger import outcomes as outcome_recorder
from agentoquant.ledger.outcomes import (
    OutcomeRecordingError,
    record_due_outcomes,
    still_open,
)
from agentoquant.ledger.schema import (
    DecisionCard,
    ExecutionPayload,
    HumanActionPayload,
    RiskGateVerdict,
)
from agentoquant.ledger.store import LedgerStore

HORIZONS = ("1h", "4h", "24h")


# ----------------------------------------------------------------------------------------------
# Fixtures as plain helpers
# ----------------------------------------------------------------------------------------------


def make_card(
    ledger: LedgerStore,
    cycle_id: str,
    *,
    action: Action = Action.ENTER_LADDERED,
    coin: str | None = "SOL",
    size_pct: float | None = 3.0,
) -> str:
    """Write one decision card and return its ``record_id``."""
    return ledger.write(
        Stage.DECISION_CARD,
        cycle_id,
        DecisionCard(
            cycle_id=cycle_id,
            selected_proposal_id=f"prop-{cycle_id}",
            action=action,
            coin=coin,
            sleeve=Sleeve.A if coin else None,
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


def make_execution(
    ledger: LedgerStore,
    cycle_id: str,
    card_id: str,
    *,
    fill_price: float | None = 150.0,
    fill_qty: float | None = 100.0,
    fee_paid: float | None = 0.6,
    status: str = "filled",
) -> str:
    """Write one execution row. Null fills are the Phase 0 shape (F1)."""
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


def make_verdict(ledger: LedgerStore, cycle_id: str, card_id: str, verdict: str, rule: str | None) -> str:
    return ledger.write(
        Stage.RISK_GATE_VERDICT,
        cycle_id,
        RiskGateVerdict(
            decision_card_id=card_id,
            verdict=verdict,
            rule_fired=rule,
            original_size_pct=3.0,
            final_size_pct=0.0 if verdict == "rejected" else 3.0,
            funding_floor_breach=False,
        ),
        producer_role="risk_gate",
    )


def make_veto(ledger: LedgerStore, cycle_id: str, card_id: str, moment: datetime) -> str:
    return ledger.write(
        Stage.HUMAN_ACTION,
        cycle_id,
        HumanActionPayload(
            decision_card_id=card_id,
            command="veto",
            actor="shahrad",
            responded_at=moment,
            within_window=True,
        ),
        producer_role="human",
    )


def backdate(ledger: LedgerStore, table: str, record_id: str, moment: datetime) -> None:
    """Move a record's ``ts`` into the past. The store stamps write time, so tests do this."""
    ledger.query(f'UPDATE "{table}" SET "ts" = ? WHERE "record_id" = ?', [moment, record_id])


def outcome_rows(ledger: LedgerStore) -> list[dict]:
    return ledger.query(
        'SELECT "decision_card_id", "cycle_id", "horizon", "pnl_pct", "pnl_abs", '
        '"realized_up", "still_open", "producer_role" FROM "outcome" '
        'ORDER BY "decision_card_id", "horizon"'
    )


# ----------------------------------------------------------------------------------------------
# The defect: a past cycle with no outcome rows, and none being written
# ----------------------------------------------------------------------------------------------


def test_a_past_cycle_gets_its_due_outcome_rows_exactly_once(tmp_path) -> None:
    """A cycle three hours old: the +1h row appears once, +4h and +24h are not yet due."""
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-19T05Z-0001"
    card_id = make_card(ledger, cycle_id)
    execution_id = make_execution(ledger, cycle_id, card_id)
    for table, record_id in (("decision_card", card_id), ("execution", execution_id)):
        backdate(ledger, table, record_id, now - timedelta(hours=3))

    # Before the recorder runs, the ledger is exactly the state the adversary found: no outcome row.
    assert outcome_rows(ledger) == []
    due = ledger.due_outcomes("1h", as_of=now)
    assert [row["decision_card_id"] for row in due] == [card_id]

    first = record_due_outcomes(ledger, as_of=now)
    assert first["written_count"] == 1, first
    assert first["by_horizon"] == {"1h": 1, "4h": 0, "24h": 0}
    rows = outcome_rows(ledger)
    assert [(row["decision_card_id"], row["horizon"]) for row in rows] == [(card_id, "1h")]
    assert rows[0]["cycle_id"] == cycle_id, "the outcome must stay linked to its cycle"
    assert rows[0]["producer_role"] == "outcome_meter"

    second = record_due_outcomes(ledger, as_of=now)
    assert second["due_count"] == 0 and second["written_count"] == 0, second
    assert len(outcome_rows(ledger)) == 1, "the recorder wrote the horizon twice"

    third = record_due_outcomes(ledger, as_of=now)
    assert len(outcome_rows(ledger)) == 1
    assert third["due_count"] == 0


def test_a_horizon_that_has_not_elapsed_is_not_written(tmp_path) -> None:
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    card_id = make_card(ledger, "2026-09-19T05Z-0001")
    make_execution(ledger, "2026-09-19T05Z-0001", card_id)

    before = record_due_outcomes(ledger, as_of=now + timedelta(minutes=59))
    assert before["due_count"] == 0, "a horizon was written before it elapsed"
    assert outcome_rows(ledger) == []
    assert record_due_outcomes(ledger, as_of=now + timedelta(minutes=59), dry_run=True)["due_count"] == 0

    after = record_due_outcomes(ledger, as_of=now + timedelta(hours=1, minutes=1))
    assert after["by_horizon"] == {"1h": 1, "4h": 0, "24h": 0}
    assert {row["horizon"] for row in outcome_rows(ledger)} == {"1h"}


def test_a_run_after_downtime_backfills_the_horizons_that_elapsed(tmp_path) -> None:
    """A card 30 hours old gets 1h, 4h and 24h - one row each - in a single pass."""
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-18T00Z-0001"
    card_id = make_card(ledger, cycle_id)
    execution_id = make_execution(ledger, cycle_id, card_id)
    for table, record_id in (("decision_card", card_id), ("execution", execution_id)):
        backdate(ledger, table, record_id, now - timedelta(hours=30))

    result = record_due_outcomes(ledger, as_of=now)
    assert result["by_horizon"] == {"1h": 1, "4h": 1, "24h": 1}, result
    rows = outcome_rows(ledger)
    assert sorted(row["horizon"] for row in rows) == ["1h", "24h", "4h"]
    assert len({(row["decision_card_id"], row["horizon"]) for row in rows}) == 3

    assert record_due_outcomes(ledger, as_of=now)["written_count"] == 0


# ----------------------------------------------------------------------------------------------
# Every verdict path gets a row: executed, rejected and vetoed alike
# ----------------------------------------------------------------------------------------------


def test_executed_rejected_and_vetoed_cards_all_get_a_row(tmp_path) -> None:
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    old = now - timedelta(hours=3)

    executed_cycle = "2026-09-19T05Z-0001"
    executed = make_card(ledger, executed_cycle)
    make_verdict(ledger, executed_cycle, executed, "approved", None)
    execution_id = make_execution(ledger, executed_cycle, executed)
    backdate(ledger, "execution", execution_id, old)

    rejected_cycle = "2026-09-19T06Z-0001"
    rejected = make_card(ledger, rejected_cycle)
    make_verdict(ledger, rejected_cycle, rejected, "rejected", "daily_turnover_cap")

    vetoed_cycle = "2026-09-19T07Z-0001"
    vetoed = make_card(ledger, vetoed_cycle)
    make_verdict(ledger, vetoed_cycle, vetoed, "approved", None)
    make_veto(ledger, vetoed_cycle, vetoed, now)

    for card_id in (executed, rejected, vetoed):
        backdate(ledger, "decision_card", card_id, old)

    due = ledger.due_outcomes("1h", as_of=now)
    assert {row["decision_card_id"] for row in due} == {executed, rejected, vetoed}
    verdicts = {row["decision_card_id"]: row["verdict"] for row in due}
    commands = {row["decision_card_id"]: row["human_command"] for row in due}
    assert verdicts == {executed: "approved", rejected: "rejected", vetoed: "approved"}
    assert commands[vetoed] == "veto"

    result = record_due_outcomes(ledger, as_of=now)
    assert result["by_horizon"] == {"1h": 3, "4h": 0, "24h": 0}, result
    rows = {row["decision_card_id"]: row for row in outcome_rows(ledger)}
    assert set(rows) == {executed, rejected, vetoed}
    assert rows[executed]["still_open"] is True, "a filled entry is an open position"
    assert rows[rejected]["still_open"] is False, "a rejected card opened nothing"
    assert rows[vetoed]["still_open"] is False, "a vetoed card opened nothing"
    assert all(row["realized_up"] is None for row in rows.values())
    assert all(row["pnl_pct"] is None for row in rows.values())


def test_a_null_fill_records_the_horizon_with_honest_nulls(tmp_path) -> None:
    """The Phase 0 shape (F1): fills are null, so PnL is unknowable - but the row still exists."""
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-19T05Z-0001"
    card_id = make_card(ledger, cycle_id)
    execution_id = make_execution(
        ledger, cycle_id, card_id, fill_price=None, fill_qty=None, fee_paid=None, status="unfilled_timeout"
    )
    backdate(ledger, "decision_card", card_id, now - timedelta(hours=3))
    backdate(ledger, "execution", execution_id, now - timedelta(hours=3))

    assert record_due_outcomes(ledger, as_of=now)["written_count"] == 1
    row = outcome_rows(ledger)[0]
    assert row["pnl_pct"] is None and row["pnl_abs"] is None and row["realized_up"] is None
    assert row["still_open"] is False


def test_a_price_source_fills_pnl_for_a_filled_card(tmp_path) -> None:
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-19T05Z-0001"
    card_id = make_card(ledger, cycle_id)
    execution_id = make_execution(ledger, cycle_id, card_id, fill_price=150.0, fill_qty=100.0, fee_paid=0.6)
    backdate(ledger, "decision_card", card_id, now - timedelta(hours=3))
    backdate(ledger, "execution", execution_id, now - timedelta(hours=3))

    result = record_due_outcomes(
        ledger, as_of=now, price_lookup=outcome_recorder.price_lookup_from({"SOL": 155.0})
    )
    assert result["written_count"] == 1
    row = outcome_rows(ledger)[0]
    assert round(row["pnl_pct"], 6) == round((155.0 - 150.0) / 150.0 * 100.0, 6)
    assert round(row["pnl_abs"], 6) == round(5.0 * 100.0 - 0.6, 6)
    assert row["realized_up"] is True
    assert row["still_open"] is True

    # A losing price on the next horizon is recorded as such.
    later = record_due_outcomes(
        ledger,
        as_of=now + timedelta(hours=2),
        horizons=("4h",),
        price_lookup=outcome_recorder.price_lookup_from({"SOL": 140.0}),
    )
    assert later["written_count"] == 1
    rows = {row["horizon"]: row for row in outcome_rows(ledger)}
    assert rows["4h"]["realized_up"] is False
    assert rows["4h"]["pnl_abs"] < 0


def test_an_exit_card_is_not_still_open(tmp_path) -> None:
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-19T05Z-0001"
    card_id = make_card(ledger, cycle_id, action=Action.EXIT)
    execution_id = make_execution(ledger, cycle_id, card_id, status="filled")
    backdate(ledger, "decision_card", card_id, now - timedelta(hours=3))
    backdate(ledger, "execution", execution_id, now - timedelta(hours=3))

    assert record_due_outcomes(ledger, as_of=now)["written_count"] == 1
    assert outcome_rows(ledger)[0]["still_open"] is False
    assert still_open("exit", {"fill_price": 1.0}) is False
    assert still_open("trim", {"fill_price": 1.0}) is True
    assert still_open("enter_laddered", None) is False


def test_a_hold_card_is_never_due(tmp_path) -> None:
    """``hold`` carries no coin and no position, so it has no horizon outcome."""
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    card_id = make_card(ledger, "2026-09-19T05Z-0001", action=Action.HOLD, coin=None, size_pct=None)
    backdate(ledger, "decision_card", card_id, now - timedelta(hours=30))
    assert record_due_outcomes(ledger, as_of=now)["due_count"] == 0
    assert outcome_rows(ledger) == []


def test_the_exit_horizon_and_unknown_horizons_are_refused(tmp_path) -> None:
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    for horizons in (("exit",), ("1h", "48h")):
        try:
            record_due_outcomes(ledger, horizons=horizons)
        except OutcomeRecordingError as exc:
            assert "exit" in str(exc) or "unknown" in str(exc)
        else:  # pragma: no cover - the assertion below is the point
            raise AssertionError(f"horizons {horizons} were accepted")


# ----------------------------------------------------------------------------------------------
# Entry points: the module CLI and the scheduler hook run the same pass
# ----------------------------------------------------------------------------------------------


def test_the_module_entry_point_writes_then_finds_nothing_to_do(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOQUANT_LEDGER_PATH", str(tmp_path / "ledger.duckdb"))
    now = datetime.now(UTC)
    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-19T05Z-0001"
    card_id = make_card(ledger, cycle_id)
    execution_id = make_execution(ledger, cycle_id, card_id)
    backdate(ledger, "decision_card", card_id, now - timedelta(hours=3))
    backdate(ledger, "execution", execution_id, now - timedelta(hours=3))

    assert outcome_recorder.main(["--json"]) == 0
    assert len(outcome_rows(ledger)) == 1

    assert outcome_recorder.main(["--dry-run"]) == 0
    assert len(outcome_rows(ledger)) == 1

    assert outcome_recorder.main([]) == 0
    assert len(outcome_rows(ledger)) == 1

    assert outcome_recorder.main(["--horizon", "exit"]) == 2
    assert outcome_recorder.main(["--as-of", "not-a-time"]) == 2
    assert outcome_recorder.main(["--price", "SOL"]) == 2


def test_the_scheduler_hook_runs_the_same_pass(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOQUANT_LEDGER_PATH", str(tmp_path / "ledger.duckdb"))
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))
    monkeypatch.setenv("AGENTOQUANT_CALL_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.delenv("AGENTOQUANT_TELEGRAM", raising=False)
    now = datetime.now(UTC)

    from agentoquant import scheduler

    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    cycle_id = "2026-09-19T05Z-0001"
    card_id = make_card(ledger, cycle_id)
    execution_id = make_execution(ledger, cycle_id, card_id)
    backdate(ledger, "decision_card", card_id, now - timedelta(hours=5))
    backdate(ledger, "execution", execution_id, now - timedelta(hours=5))

    result = scheduler.run_outcome_pass()
    assert result["by_horizon"] == {"1h": 1, "4h": 1, "24h": 0}, result
    assert len(outcome_rows(ledger)) == 2

    assert scheduler.main(["--outcomes"]) == 0
    assert len(outcome_rows(ledger)) == 2, "the scheduler hook wrote a duplicate horizon"
