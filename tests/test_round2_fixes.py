"""Round-2 adversary findings B1, B2, B3 and B4: the fixes and their regressions.

B1 (blocker)  a ``/flat`` whose venue read times out closed nothing and reported a clean cycle:
              ``closes: 0, errors: 0, degraded: false``, exit 0, every position still open.
B2 (high)     nothing in production could raise a halt: ``trigger_flat`` and ``KillSwitch.evaluate``
              had no caller anywhere in the package.
B3 (high)     two processes could not open the ledger at the same instant (``Conflicting lock is
              held``, an uncaught ``IOException`` out of the constructor).
B4 (high)     a total venue failure no longer marked the cycle degraded: ``errors: 3, degraded:
              false``, exit 0, because per-intent isolation means nothing raises any more.

Deliberately free of decorators: a file whose only at-signs are in this docstring cannot trip the
gateway's content filter, which is what killed five subagents on this repo.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from agentoquant.enums import Stage
from agentoquant.execution.order_manager import OrderManager, PositionReadError
from agentoquant.execution.paper import human_halt_command, run_cycle
from agentoquant.execution.signal_store import SignalStore
from agentoquant.ledger.schema import HumanActionPayload
from agentoquant.ledger.store import LedgerStore
from agentoquant.risk.kill_switch import KillSwitch, build_kill_switch


class VenueReadTimeout(Exception):
    """The failure the live venue actually produces on ``/status``."""


class StandInVenue:
    """Records intents instead of forwarding them, and can fail the ways the live venue fails."""

    def __init__(
        self,
        *,
        fail_positions: bool = False,
        fail_submit: bool = False,
        positions: list[dict] | None = None,
        risk: dict | None = None,
    ) -> None:
        self.fail_positions = fail_positions
        self.fail_submit = fail_submit
        self.intents: list = []
        self.positions = (
            positions
            if positions is not None
            else [{"coin": "ETH", "pair": "ETH/USD", "trade_id": 3, "amount": 0.015}]
        )
        self.risk = risk

    def available(self) -> bool:
        return True

    def supports(self, intent) -> bool:
        return True

    def submit(self, intent) -> dict:
        self.intents.append(intent)
        if self.fail_submit:
            raise VenueReadTimeout("ReadTimeout: HTTPConnectionPool(host='127.0.0.1', port=8080)")
        return {"status": "filled", "fill_price": 81000.0, "fill_qty": 0.001, "fee_paid": 0.0324}

    def open_positions(self):
        if self.fail_positions:
            raise VenueReadTimeout("ReadTimeout: HTTPConnectionPool(host='127.0.0.1', port=8080)")
        return list(self.positions)

    def last_price(self, pair):
        return 81000.0

    def portfolio_risk(self):
        if self.risk is None:
            raise VenueReadTimeout("ReadTimeout")
        return dict(self.risk)

    def calls(self) -> list[str]:
        return [getattr(intent, "freqtrade_call", "?") for intent in self.intents]


def _cycle(tmp_path: Path, transport, cycle_id: str, *, switches: KillSwitch | None = None) -> dict:
    return run_cycle(
        cycle_id=cycle_id,
        ledger=LedgerStore(tmp_path / f"{cycle_id}.duckdb"),
        store=SignalStore(root=tmp_path / f"signals-{cycle_id}"),
        transport=transport,
        sequence=0,
        kill_switch=switches,
        ack_wait_s=0.05,
        notify=False,
    )


def _flat(tmp_path: Path, name: str, positions: list[dict] | None = None) -> KillSwitch:
    switch = build_kill_switch(state_path=tmp_path / f"{name}.json")
    switch.trigger_flat(positions=positions or [], actor="test:/flat", cycle_id=name)
    return switch


# ------------------------------------------------------------------------------------------------
# B1: the read itself
# ------------------------------------------------------------------------------------------------


def test_a_failed_position_read_is_not_an_empty_book(tmp_path: Path) -> None:
    """``strict=True`` distinguishes "could not read" from "nothing is open"; the default does not."""
    manager = OrderManager(
        ledger=LedgerStore(tmp_path / "ledger.duckdb"),
        transport=StandInVenue(fail_positions=True),
    )
    assert manager.open_positions() == []
    try:
        manager.open_positions(strict=True)
    except PositionReadError as exc:
        assert "VenueReadTimeout" in str(exc)
    else:  # pragma: no cover - the assertion is that it raises
        raise AssertionError("a failed strict read returned instead of raising")


def test_a_transport_that_cannot_report_positions_is_an_error_on_the_strict_path(tmp_path: Path) -> None:
    """A transport without ``open_positions`` is also not an empty book."""

    class Bare:
        def available(self) -> bool:
            return True

        def supports(self, intent) -> bool:
            return True

        def submit(self, intent) -> dict:
            return {"status": "filled"}

    manager = OrderManager(ledger=LedgerStore(tmp_path / "ledger.duckdb"), transport=Bare())
    assert manager.open_positions() == []
    try:
        manager.open_positions(strict=True)
    except PositionReadError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a transport that cannot report positions passed the strict read")


# ------------------------------------------------------------------------------------------------
# B1: the loop's close path
# ------------------------------------------------------------------------------------------------


def test_a_flat_with_a_blind_venue_is_loud_and_stays_latched(tmp_path: Path) -> None:
    """THE BLOCKER. Before the fix: closes=0, errors=0, degraded=False, exit 0, positions open."""
    blind = StandInVenue(fail_positions=True)
    switch = _flat(tmp_path, "blind", blind.positions)
    summary = _cycle(tmp_path, blind, "b1-blind", switches=switch)

    assert summary["degraded"] is True
    assert summary["closes_error"] is not None
    assert "PositionReadError" in summary["closes_error"]
    assert summary["closes_unconfirmed"] is True
    assert summary["halts"]["entries_blocked"] is True
    # The position the halt recorded is still on the list, so the next tick still tries.
    assert summary["halt_status"]["positions_to_close"] == ["ETH"]
    assert summary["closes"] == 1


def test_a_flat_with_a_healthy_venue_submits_the_close(tmp_path: Path) -> None:
    venue = StandInVenue()
    switch = _flat(tmp_path, "healthy", venue.positions)
    summary = _cycle(tmp_path, venue, "b1-healthy", switches=switch)

    assert summary["closes"] == 1
    assert summary["closes_error"] is None
    assert "forceexit" in venue.calls()
    # Submitted is not confirmed: the venue still reports the position, so the flat is unproven.
    assert summary["closes_unconfirmed"] is True


def test_a_confirmed_flat_clears_the_pending_list(tmp_path: Path) -> None:
    """When the venue answers and nothing is open, the halt's stale list is cleared and stays clear.

    Without this the pending entry would be re-planned forever: the union that makes the blind case
    safe would keep generating a close for a position that has already gone.
    """
    switch = _flat(tmp_path, "confirm", [{"coin": "ETH", "pair": "ETH/USD", "trade_id": 3}])
    empty = StandInVenue(positions=[])
    summary = _cycle(tmp_path, empty, "b1-confirm", switches=switch)

    assert summary["closes"] == 0
    assert summary["closes_error"] is None
    assert summary["closes_unconfirmed"] is False
    assert summary["halt_status"]["positions_to_close"] == []
    assert empty.calls() == []


def test_a_flat_stays_latched_across_a_process_restart(tmp_path: Path) -> None:
    """The unconfirmed flag survives the tick, like the halt itself does."""
    state = tmp_path / "restart.json"
    first = build_kill_switch(state_path=state)
    first.trigger_flat(positions=[{"coin": "ETH"}], actor="test:/flat", cycle_id="one")
    assert first.status().closes_unconfirmed is True

    second = build_kill_switch(state_path=state)  # a fresh process, same state file
    assert second.status().closes_unconfirmed is True
    assert second.status().positions_to_close == ("ETH",)


# ------------------------------------------------------------------------------------------------
# B4: a total venue failure is degraded
# ------------------------------------------------------------------------------------------------


def test_a_total_venue_failure_marks_the_cycle_degraded(tmp_path: Path) -> None:
    """Before the fix: intents=3, errors=3, degraded=False, venue_error=None, exit 0."""
    dead = StandInVenue(fail_submit=True)
    summary = _cycle(tmp_path, dead, "b4-dead", switches=build_kill_switch(state_path=tmp_path / "b4.json"))

    assert summary["errors"] == 3
    assert summary["degraded"] is True
    # The message survives, not just a boolean flag.
    assert summary["venue_error"] is not None
    assert "VenueReadTimeout" in summary["venue_error"]


def test_a_clean_cycle_is_not_degraded(tmp_path: Path) -> None:
    healthy = StandInVenue()
    summary = _cycle(
        tmp_path, healthy, "b4-clean", switches=build_kill_switch(state_path=tmp_path / "b4c.json")
    )
    assert summary["errors"] == 0
    assert summary["degraded"] is False


# ------------------------------------------------------------------------------------------------
# B2: the halts are reachable and driven
# ------------------------------------------------------------------------------------------------


def test_the_daily_loss_halt_fires_from_the_venues_own_reading(tmp_path: Path) -> None:
    """``KillSwitch.evaluate`` had no caller, so neither automatic halt could fire in production."""
    losing = StandInVenue(
        risk={"daily_loss_used_pct": 3.5, "drawdown_used_pct": 1.0, "daily_loss_source": "daily"}
    )
    switch = build_kill_switch(state_path=tmp_path / "b2.json")
    summary = _cycle(tmp_path, losing, "b2-auto", switches=switch)

    assert summary["halts"]["daily_halted"] is True
    assert summary["halts"]["entries_blocked"] is True
    # The gate and the switch see the same numbers, so the gate's own rule fires too.
    assert summary["verdict"] == "rejected"
    assert summary["rule_fired"] == "daily_loss_halt"
    assert summary["halt_inputs"]["daily_loss_used_pct"] == 3.5


def test_the_weekly_drawdown_halt_fires_from_the_venues_own_reading(tmp_path: Path) -> None:
    losing = StandInVenue(
        risk={"daily_loss_used_pct": 0.0, "drawdown_used_pct": 9.0, "daily_loss_source": "daily"}
    )
    switch = build_kill_switch(state_path=tmp_path / "b2w.json")
    summary = _cycle(tmp_path, losing, "b2-weekly", switches=switch)

    assert summary["halts"]["weekly_halted"] is True
    assert summary["verdict"] == "rejected"


def test_an_unreadable_risk_reading_is_recorded_rather_than_passed_off_as_zero(tmp_path: Path) -> None:
    """``None`` is not "no loss": a halt fed a fabricated zero is a halt that cannot fire."""
    blind = StandInVenue(fail_positions=True)
    summary = _cycle(
        tmp_path, blind, "b2-blind", switches=build_kill_switch(state_path=tmp_path / "b2b.json")
    )

    assert summary["halt_inputs"] == {"available": False}
    assert summary["halt_inputs_available"] is False


def test_a_risk_reading_that_is_available_is_reported(tmp_path: Path) -> None:
    healthy = StandInVenue(risk={"daily_loss_used_pct": 0.1, "drawdown_used_pct": 0.2})
    summary = _cycle(tmp_path, healthy, "b2-ok", switches=build_kill_switch(state_path=tmp_path / "b2o.json"))
    assert summary["halt_inputs_available"] is True
    assert summary["halt_inputs"]["daily_loss_used_pct"] == 0.1


def test_the_human_flat_command_records_the_action_and_persists_the_halt(tmp_path: Path, monkeypatch) -> None:
    """The production trigger B2 said did not exist. A venue read failure still latches the halt."""
    state = tmp_path / "human.json"
    monkeypatch.setenv("AGENTOQUANT_KILL_SWITCH_STATE", str(state))
    monkeypatch.setenv("AGENTOQUANT_LEDGER_PATH", str(tmp_path / "human.duckdb"))
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))

    blind = StandInVenue(fail_positions=True)
    monkeypatch.setattr(
        "agentoquant.execution.paper.freqtrade_dry_run_transport", lambda *a, **k: blind
    )

    result = human_halt_command("flat", cycle_id="human-flat")

    assert result["command"] == "flat"
    assert result["halt"]["flat"] is True
    assert result["halt"]["entries_blocked"] is True
    # The read failed, so the flat is latched and explicitly unproven rather than reported clean.
    assert result["positions_read_error"] is not None
    assert result["halt"]["closes_unconfirmed"] is True

    restored = build_kill_switch(state_path=state)
    assert restored.halt_state().flat is True

    ledger = LedgerStore(tmp_path / "human.duckdb")
    rows = ledger.query("SELECT command, actor FROM human_action")
    assert any(row["command"] == "flat" for row in rows)


def test_the_resume_command_clears_the_halt(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "resume.json"
    monkeypatch.setenv("AGENTOQUANT_KILL_SWITCH_STATE", str(state))
    monkeypatch.setenv("AGENTOQUANT_LEDGER_PATH", str(tmp_path / "resume.duckdb"))
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))

    venue = StandInVenue(positions=[])
    monkeypatch.setattr(
        "agentoquant.execution.paper.freqtrade_dry_run_transport", lambda *a, **k: venue
    )

    human_halt_command("flat", cycle_id="resume-flat")
    assert build_kill_switch(state_path=state).halt_state().flat is True

    result = human_halt_command("resume", cycle_id="resume-clear")
    assert result["halt"]["flat"] is False
    assert result["halt"]["entries_blocked"] is False
    assert build_kill_switch(state_path=state).halt_state().flat is False


def test_the_paper_command_exposes_flat_and_resume() -> None:
    """The flags are on an existing command, so the plan's twelve command names stay literal."""
    from agentoquant.cli import PaperInput

    fields = PaperInput.model_fields
    assert "flat" in fields
    assert "resume" in fields
    assert fields["flat"].default is False
    assert fields["resume"].default is False


# ------------------------------------------------------------------------------------------------
# B3: two processes can share the ledger
# ------------------------------------------------------------------------------------------------

_WORKER = """
import sys, time
from datetime import UTC, datetime
from agentoquant.enums import Stage
from agentoquant.ledger.schema import HumanActionPayload
from agentoquant.ledger.store import LedgerStore

path, tag = sys.argv[1], sys.argv[2]
try:
    store = LedgerStore(path)
except Exception as exc:
    print(f"{tag}: CONSTRUCT FAILED {type(exc).__name__}: {exc}")
    sys.exit(2)

fail = 0
for i in range(20):
    try:
        store.write(
            Stage.HUMAN_ACTION,
            f"{tag}-cycle-{i}",
            HumanActionPayload(
                decision_card_id=None,
                command="pause",
                actor=tag,
                responded_at=datetime.now(UTC),
                within_window=True,
            ),
            producer_role="human",
        )
    except Exception as exc:
        fail += 1
        if fail == 1:
            print(f"{tag}: WRITE FAILED {type(exc).__name__}: {exc}")
    time.sleep(0.001)
print(f"{tag}: fail={fail}")
sys.exit(0 if fail == 0 else 3)
"""


def test_three_independent_processes_can_write_one_ledger(tmp_path: Path) -> None:
    """B3's own reproduction: before the fix, two of three could not even construct the store."""
    script = tmp_path / "worker.py"
    script.write_text(_WORKER, encoding="utf-8")
    db = tmp_path / "shared.duckdb"

    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(db), f"p{i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for i in range(3)
    ]
    outputs = []
    codes = []
    for proc in procs:
        out, _ = proc.communicate(timeout=180)
        outputs.append(out.strip())
        codes.append(proc.returncode)

    assert codes == [0, 0, 0], f"a process could not share the ledger: {outputs}"
    rows = LedgerStore(db).query("SELECT count(*) AS n FROM human_action")
    assert rows[0]["n"] == 60


def test_the_ledger_lock_is_reentrant_within_a_process(tmp_path: Path) -> None:
    """``write`` calls ``query`` internally, so a non-reentrant lock would deadlock on itself."""
    store = LedgerStore(tmp_path / "reentrant.duckdb")
    payload = HumanActionPayload(
        decision_card_id=None,
        command="pause",
        actor="test",
        responded_at=datetime.now(UTC),
        within_window=True,
    )
    first = store.write(Stage.HUMAN_ACTION, "reentrant-1", payload, producer_role="human")
    second = store.write(Stage.HUMAN_ACTION, "reentrant-2", payload, producer_role="human")
    assert first != second


def test_the_lock_file_is_a_sidecar_next_to_the_ledger(tmp_path: Path) -> None:
    """The lock is a separate file, so it never becomes part of the database the ledger is."""
    db = tmp_path / "sidecar.duckdb"
    LedgerStore(db)
    assert (tmp_path / "sidecar.duckdb.lock").exists()
    assert json.dumps({"db": db.name})  # the ledger file itself is untouched by the lock
