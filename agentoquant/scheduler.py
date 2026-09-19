"""The hourly cadence driver. Owned by Task 6.

One cycle per hour, unattended, with no model in the path: a systemd **user** unit and timer
(``deploy/agentoquant-hourly.{service,timer}``) run ``agentoquant paper --placeholder --hours 1`` on
the hour. Linger is already enabled on this box, so the timer survives a logout and a reboot.

This module holds the parts of that schedule that are worth testing offline: the cycle id format, the
next tick, and the checks a test can run against the two unit files. It also gives the timer a
direct entry point (``python -m agentoquant.scheduler --once``) for a manual tick.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentoquant.config_loader import load_settings, repo_root

#: Cycle ids are ``<ISO hour>Z-<sequence>``, e.g. ``2026-09-18T14Z-0001`` (addendum section 4).
#: The sequence is appended rather than formatted into the stamp: glibc's ``strftime`` reads
#: ``%04d`` as a flagged day-of-month, so the two parts are built separately.
CYCLE_ID_FORMAT = "%Y-%m-%dT%HZ"
CYCLE_SEQUENCE_FORMAT = "%04d"

#: The two unit files the timer needs, both under ``deploy/``.
SERVICE_UNIT = "agentoquant-hourly.service"
TIMER_UNIT = "agentoquant-hourly.timer"

#: Tokens every timer file must carry, and every service file must carry.
TIMER_REQUIRED = ("[Timer]", "OnCalendar=hourly", "Persistent=true", "WantedBy=timers.target")
SERVICE_REQUIRED = ("[Service]", "Type=oneshot", "paper --placeholder", "WorkingDirectory=")


def cadence_minutes() -> int:
    """``settings.cadence_minutes`` (60), with a safe default when the config cannot be read."""
    try:
        return int(load_settings().cadence_minutes)
    except Exception:  # pragma: no cover - a broken config fails closed at CLI startup
        return 60


def cycle_id_for(moment: datetime, *, sequence: int = 1) -> str:
    """The cycle id for the hour containing ``moment``."""
    stamp = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    return stamp.strftime(CYCLE_ID_FORMAT) + "-" + (CYCLE_SEQUENCE_FORMAT % int(sequence))


def hour_start(moment: datetime) -> datetime:
    """The start of the hour containing ``moment``, in UTC."""
    stamp = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    return stamp.replace(minute=0, second=0, microsecond=0)


def next_tick(now: datetime | None = None, *, minutes: int | None = None) -> datetime:
    """The next cadence boundary strictly after ``now``."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    step = int(minutes or cadence_minutes())
    boundary = hour_start(moment) + timedelta(minutes=step)
    while boundary <= moment:
        boundary += timedelta(minutes=step)
    return boundary


def seconds_until(moment: datetime, now: datetime | None = None) -> float:
    """Seconds from ``now`` to ``moment``, never negative."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    target = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    return max(0.0, (target - current).total_seconds())


def deploy_dir() -> Path:
    """The repository's ``deploy/`` directory, where the two unit files live."""
    return repo_root() / "deploy"


def unit_path(name: str) -> Path:
    return deploy_dir() / name


def unit_text(name: str) -> str:
    path = unit_path(name)
    if not path.exists():
        raise FileNotFoundError(f"missing unit file: {path}")
    return path.read_text(encoding="utf-8")


def check_units() -> dict[str, list[str]]:
    """The required lines missing from each unit file. Empty lists mean the units are complete."""
    return {
        SERVICE_UNIT: [t for t in SERVICE_REQUIRED if t not in unit_text(SERVICE_UNIT)],
        TIMER_UNIT: [t for t in TIMER_REQUIRED if t not in unit_text(TIMER_UNIT)],
    }


def run_tick(*, placeholder: bool = True, cycle_id: str | None = None) -> dict:
    """Run exactly one hourly cycle through the paper loop. Imported lazily so the timer is cheap."""
    from agentoquant.execution import paper

    return paper.run_loop(hours=1, placeholder=placeholder, cycle_id=cycle_id, sleep=False)


def run_outcome_pass(
    *, as_of: datetime | None = None, prices: dict[str, float] | None = None
) -> dict:
    """Write the outcome rows whose horizon (+1h / +4h / +24h) has elapsed.

    The outcome stage's writer is :mod:`agentoquant.ledger.outcomes`; this is the timer's hook for it,
    so a box that never edits ``execution/paper.py`` still records horizons. Idempotent: running it on
    every tick writes each ``(card, horizon)`` once, and a tick after downtime backfills whatever
    elapsed. Imported lazily so a plain tick does not pay for the ledger's imports.
    """
    from agentoquant.ledger.outcomes import price_lookup_from, record_due_outcomes
    from agentoquant.ledger.store import LedgerStore

    return record_due_outcomes(LedgerStore(), as_of=as_of, price_lookup=price_lookup_from(prices))


def main(argv: list[str] | None = None) -> int:
    """``python -m agentoquant.scheduler``: one tick, one outcome pass, or a printed schedule."""
    parser = argparse.ArgumentParser(prog="agentoquant.scheduler", description=__doc__)
    parser.add_argument("--once", action="store_true", help="run exactly one paper cycle now")
    parser.add_argument("--placeholder", action="store_true", help="use the Phase 0 decision source")
    parser.add_argument("--cycle-id", default=None, help="override the cycle id")
    parser.add_argument("--show", action="store_true", help="print the next tick and exit")
    parser.add_argument(
        "--outcomes",
        action="store_true",
        help="write the outcome rows whose horizon has elapsed, then exit",
    )
    parser.add_argument("--as-of", default=None, help="ISO instant for --outcomes (UTC)")
    parser.add_argument(
        "--price", action="append", default=[], help="--outcomes price as COIN=VALUE (repeatable)"
    )
    args = parser.parse_args(argv)

    if args.show:
        now = datetime.now(UTC)
        print(f"now:        {now.isoformat()}")
        print(f"next tick:  {next_tick(now).isoformat()}")
        print(f"cycle id:   {cycle_id_for(now)}")
        print(f"units:      {deploy_dir()}")
        return 0

    if args.outcomes:
        from agentoquant.ledger.outcomes import main as outcomes_main

        forwarded: list[str] = []
        if args.as_of:
            forwarded += ["--as-of", args.as_of]
        for item in args.price:
            forwarded += ["--price", item]
        return outcomes_main(forwarded)

    result = run_tick(placeholder=args.placeholder or True, cycle_id=args.cycle_id)
    status = str(result.get("status", "ok"))
    print(f"tick {result.get('cycles', 0)} cycle(s): {status}")
    for cycle in result.get("results", []):
        print(f"  {cycle.get('cycle_id')}: {cycle.get('action')} {cycle.get('status')}")
    return 0 if status != "error" else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())


__all__ = [
    "CYCLE_ID_FORMAT",
    "CYCLE_SEQUENCE_FORMAT",
    "SERVICE_REQUIRED",
    "SERVICE_UNIT",
    "TIMER_REQUIRED",
    "TIMER_UNIT",
    "cadence_minutes",
    "check_units",
    "cycle_id_for",
    "deploy_dir",
    "hour_start",
    "main",
    "next_tick",
    "run_outcome_pass",
    "run_tick",
    "seconds_until",
    "unit_path",
    "unit_text",
]

