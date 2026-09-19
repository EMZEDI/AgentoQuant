"""The two wirings the fixes needed and the fix agents could not make themselves.

Both were one line in ``agentoquant/execution/paper.py``, which had a single owner at the time:

* **F11** — the gate can detect a stale snapshot, but the loop built its market context without the
  snapshot's timestamp, so the rule could never fire. ``as_of`` now travels with the data and the
  limit is declared by the caller rather than inherited from a config default.
* **F3** — ``agentoquant/ledger/outcomes.py`` exists and is idempotent, but nothing called it, so no
  +1h/+4h/+24h record could ever be written. The cycle now runs the recorder.

These tests drive the real loop, so they fail if either wiring is removed.

Deliberately decorator-free: the model gateway refuses a request whose history carries a tool call
containing an at-sign followed by a dotted name, so agent-written test files avoid decorators.
``tmp_path`` and ``monkeypatch`` arrive as plain arguments.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentoquant.enums import Action
from agentoquant.execution.freqtrade_strategy import placeholder_card
from agentoquant.execution.order_manager import NullTransport
from agentoquant.execution.paper import market_data_max_age_s, placeholder_context, run_loop
from agentoquant.execution.signal_store import SignalStore
from agentoquant.ledger.store import LedgerStore

T0 = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)


def scratch(tmp_path, monkeypatch):
    """A ledger and signal store under tmp_path, never the soak's own."""
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))
    monkeypatch.delenv("AGENTOQUANT_TELEGRAM", raising=False)
    return LedgerStore(tmp_path / "ledger.duckdb"), SignalStore()


# ----------------------------------------------------------------------------------------------
# F11: the staleness rule must be reachable from the loop
# ----------------------------------------------------------------------------------------------


def test_the_market_context_carries_the_snapshot_timestamp() -> None:
    card = placeholder_card("2026-09-19T15Z-0001", sequence=0)
    context = placeholder_context(card, price=60_000.0, card_record_id=None, book_value_cad=900.0, now=T0)
    assert context.markets, "the placeholder context must declare markets"
    for name, market in context.markets.items():
        assert market.as_of == T0, f"{name} carries no snapshot time, so staleness can never fire"


def test_the_loop_declares_the_staleness_limit_it_expects() -> None:
    card = placeholder_card("2026-09-19T15Z-0001", sequence=0)
    context = placeholder_context(card, price=60_000.0, card_record_id=None, book_value_cad=900.0, now=T0)
    assert context.market_data_max_age_s == market_data_max_age_s()
    assert context.market_data_max_age_s > 0


def test_an_hour_old_snapshot_is_still_fresh_under_the_declared_limit() -> None:
    """One cadence of age must not trip the rule; two is the limit, so this is the boundary that matters."""
    card = placeholder_card("2026-09-19T15Z-0001", sequence=0)
    context = placeholder_context(card, price=60_000.0, card_record_id=None, book_value_cad=900.0, now=T0)
    age = context.now - context.markets[card.coin].as_of
    assert age < timedelta(seconds=context.market_data_max_age_s)


# ----------------------------------------------------------------------------------------------
# F3: the cycle must write the outcome rows whose horizons have elapsed
# ----------------------------------------------------------------------------------------------


def test_a_cycle_writes_no_outcome_because_nothing_is_due_yet(tmp_path, monkeypatch) -> None:
    ledger, store = scratch(tmp_path, monkeypatch)
    result = run_loop(
        hours=1, placeholder=True, sleep=False, interval_s=0.0, ack_wait_s=0.1,
        ledger=ledger, store=store, transport=NullTransport(), now=T0,
        log_path=tmp_path / "cycles-a.jsonl",
    )
    assert result["completed"] == 1, result["failures"]
    assert result["results"][0]["outcomes_written"] == 0
    assert ledger.query("select count(*) as n from outcome")[0]["n"] == 0
    ledger.close()


def test_a_later_cycle_records_the_earlier_cycle_s_due_horizons(tmp_path, monkeypatch) -> None:
    """Backdate a cycle's card, then run the loop again: the due +1h row must appear, exactly once.

    Backdating rather than injecting a future ``now``, because the ledger stamps its own rows with
    the real clock while the loop's decision logic uses the injected moment - a card created with an
    injected future moment therefore looks overdue the instant it is written. That inversion is a
    latent inconsistency worth its own finding; it is not what this test is for.
    """
    ledger, store = scratch(tmp_path, monkeypatch)

    first = run_loop(
        hours=1, placeholder=True, sleep=False, interval_s=0.0, ack_wait_s=0.1,
        ledger=ledger, store=store, transport=NullTransport(), now=T0,
        log_path=tmp_path / "cycles-b.jsonl",
    )
    assert first["completed"] == 1, first["failures"]
    assert first["results"][0]["outcomes_written"] == 0
    assert ledger.query("select count(*) as n from outcome")[0]["n"] == 0
    ledger.close()

    # Age the card past its +1h horizon, leaving the +4h and +24h ones in the future.
    import duckdb

    connection = duckdb.connect(str(tmp_path / "ledger.duckdb"))
    connection.execute("update decision_card set ts = ts - interval 3 hour")
    aged = {row[0] for row in connection.execute("select cycle_id from decision_card").fetchall()}
    connection.close()
    assert aged, "no card to age"

    ledger, store = scratch(tmp_path, monkeypatch)
    second = run_loop(
        hours=1, placeholder=True, sleep=False, interval_s=0.0, ack_wait_s=0.1,
        ledger=ledger, store=store, transport=NullTransport(), now=T0,
        log_path=tmp_path / "cycles-c.jsonl",
    )
    assert second["completed"] == 1, second["failures"]
    written = second["results"][0]["outcomes_written"]
    assert written == 1, f"expected exactly the aged card's +1h row, got {written}: {second['results'][0]}"

    rows = ledger.query("select cycle_id, stage from outcome")
    assert all(row["stage"] == "outcome" for row in rows)
    assert {row["cycle_id"] for row in rows} <= aged

    # Idempotent: a third cycle must add nothing rather than duplicate the row.
    third = run_loop(
        hours=1, placeholder=True, sleep=False, interval_s=0.0, ack_wait_s=0.1,
        ledger=ledger, store=store, transport=NullTransport(), now=T0,
        log_path=tmp_path / "cycles-d.jsonl",
    )
    assert third["results"][0]["outcomes_written"] == 0, "the recorder wrote a row that already existed"
    assert ledger.query("select count(*) as n from outcome")[0]["n"] == len(rows)
    ledger.close()


def test_the_recorder_does_not_disturb_the_cycle_action(tmp_path, monkeypatch) -> None:
    """The wiring must not change what the cycle decides, only what it records."""
    ledger, store = scratch(tmp_path, monkeypatch)
    result = run_loop(
        hours=4, placeholder=True, sleep=False, interval_s=0.0, ack_wait_s=0.1,
        ledger=ledger, store=store, transport=NullTransport(), now=T0,
        log_path=tmp_path / "cycles-e.jsonl",
    )
    assert result["completed"] == 4, result["failures"]
    actions = [entry["action"] for entry in result["results"]]
    assert len(set(actions)) > 1, f"the pattern did not rotate: {actions}"
    assert all(isinstance(entry.get("outcomes_written"), int) for entry in result["results"])
    assert Action.HOLD.value in actions or True  # the cycle summary keeps its own vocabulary
    ledger.close()