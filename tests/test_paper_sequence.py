"""The placeholder decision source must rotate across hourly ticks, not restart.

Regression for a soak bug found after nine live cycles: the hourly systemd timer runs
``agentoquant paper --placeholder --hours 1`` in a *fresh process*, and the pattern step used to be
a per-invocation counter starting at zero. Every tick therefore picked the first pattern entry, so
a three-day soak would have exercised ``enter_laddered`` and nothing else - eleven of the twelve
vocabulary actions would have gone untested while the run looked healthy.

Deliberately decorator-free: the model gateway refuses a request whose history carries a tool call
containing an at-sign followed by a dotted name, so files written by agents avoid decorators
entirely. ``tmp_path`` and ``monkeypatch`` arrive as plain arguments.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentoquant.execution.freqtrade_strategy import PLACEHOLDER_PATTERN, placeholder_action
from agentoquant.execution.paper import sequence_for

HOUR = datetime(2026, 9, 19, 13, 0, tzinfo=UTC)


def test_sequence_advances_exactly_one_step_per_hour() -> None:
    assert sequence_for(HOUR + timedelta(hours=1)) == sequence_for(HOUR) + 1
    assert sequence_for(HOUR + timedelta(hours=24)) == sequence_for(HOUR) + 24


def test_the_same_hour_always_yields_the_same_step() -> None:
    assert sequence_for(HOUR) == sequence_for(HOUR.replace(minute=59, second=59))


def test_consecutive_hourly_ticks_walk_the_whole_pattern() -> None:
    """Eight consecutive ticks produce the pattern as a cyclic rotation, in order.

    The anchor is the hour since the epoch, so the rotation starts at whatever step that hour
    happens to be; what matters is that it walks the pattern rather than repeating one entry.
    """
    steps = [sequence_for(HOUR + timedelta(hours=offset)) for offset in range(len(PLACEHOLDER_PATTERN))]
    produced = [placeholder_action(step) for step in steps]
    offset = steps[0] % len(PLACEHOLDER_PATTERN)
    expected = list(PLACEHOLDER_PATTERN[offset:]) + list(PLACEHOLDER_PATTERN[:offset])
    assert produced == expected


def test_the_old_per_invocation_counter_could_only_ever_repeat_one_action() -> None:
    """The regression stated directly: a counter that restarts at zero is not a rotation."""
    span = len(PLACEHOLDER_PATTERN)
    repeated = {placeholder_action(0) for _ in range(span)}
    rotated = {placeholder_action(sequence_for(HOUR + timedelta(hours=offset))) for offset in range(span)}
    assert len(repeated) == 1
    assert rotated == set(PLACEHOLDER_PATTERN)
    assert len(rotated) > len(repeated)


def test_two_consecutive_ticks_run_different_actions_end_to_end(tmp_path, monkeypatch) -> None:
    """Drives the real loop twice, one hour apart, the way the timer does."""
    monkeypatch.setenv("AGENTOQUANT_SIGNAL_DIR", str(tmp_path / "signals"))
    monkeypatch.delenv("AGENTOQUANT_TELEGRAM", raising=False)

    from agentoquant.execution.order_manager import NullTransport
    from agentoquant.execution.paper import run_loop
    from agentoquant.execution.signal_store import SignalStore
    from agentoquant.ledger.store import LedgerStore

    ledger = LedgerStore(tmp_path / "ledger.duckdb")
    store = SignalStore()
    actions: list[str] = []
    for hour in (0, 1):
        result = run_loop(
            hours=1,
            placeholder=True,
            sleep=False,
            interval_s=0.0,
            ledger=ledger,
            store=store,
            transport=NullTransport(),
            now=HOUR + timedelta(hours=hour),
            log_path=tmp_path / f"cycles-{hour}.jsonl",
        )
        assert result["completed"] == 1, result["failures"]
        actions.append(str(result["results"][0]["action"]))

    assert actions[0] != actions[1], f"both ticks ran {actions[0]}: the pattern restarted"
    assert actions == [
        placeholder_action(sequence_for(HOUR)).value,
        placeholder_action(sequence_for(HOUR + timedelta(hours=1))).value,
    ]
    ledger.close()
