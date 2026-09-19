"""The ``outcome`` stage's writer: the +1h / +4h / +24h record for every decision card.

Phase 0 declared :data:`agentoquant.enums.Stage.OUTCOME` and shipped ``LedgerStore.due_outcomes`` /
``LedgerStore.write_outcome``, but nothing ever called them: the outcome stage had **no writer at
all**, so no executed, rejected or vetoed proposal ever produced a horizon record
(``docs/reviews/phase0_adversary.md``, F3). The plan requires outcomes at +1h, +4h and +24h and the
ledger is the single source of truth for whether the system works, so a stage with zero rows and no
code path that could produce one is an unimplemented requirement rather than an empty table.

This module is that writer. It is deliberately a **self-contained, idempotent, importable unit** with
its own entry point (``python -m agentoquant.ledger.outcomes``, and ``python -m agentoquant.scheduler
--outcomes``) rather than a call inside ``execution/paper.py``:

* idempotent - it asks the store which ``(card, horizon)`` pairs are due and not yet written, and
  ``write_outcome`` refuses a duplicate, so running it twice in one hour writes nothing the second
  time and running it late (after a restart) still fills the horizons that elapsed while it was down;
* honest about the clock - a horizon that has not elapsed is **not** written, and the row records
  ``still_open`` from the ledger's own evidence;
* complete - it covers executed, rejected and vetoed cards alike, because veto value and Adversary
  accuracy are measured from the counterfactual outcomes of the cards that were *not* executed.

Wiring the hourly loop still needs (one line, deferred because ``execution/paper.py`` is owned by
another agent in this wave): in ``agentoquant/execution/paper.py``, import this module at the top and
call it once per cycle, after the orders have been submitted and before the cycle log is appended::

    from agentoquant.ledger.outcomes import record_due_outcomes
    ...
    record_due_outcomes(ledger, as_of=moment)

Nothing else is required: the recorder reads the ledger, not the cycle, so it needs no state from
the loop and no change to the loop's signature. Until that line lands the same pass can be run by
the timer (``python -m agentoquant.scheduler --outcomes``) or by hand.

PnL, and what Phase 0 can honestly fill
---------------------------------------
The addendum's ``outcome`` payload is ``decision_card_id``, ``horizon``, ``pnl_pct``, ``pnl_abs``,
``realized_up``, ``still_open``. The entry price is the card's own execution row (``fill_price`` /
``fill_qty``), and the exit price at the horizon is supplied by the caller as ``price_lookup``:
computing market prices is Task 7's and Task 12's job, not this module's, and Phase 0 has no ingest
in the hourly loop. With no price source the row is still written, with ``pnl_pct``, ``pnl_abs`` and
``realized_up`` honestly null and ``still_open`` set from the fill - the horizon is recorded rather
than silently skipped, which is the defect this module removes. A non-null ``fill_price`` and
``fill_qty`` are the signal that a card was executed; that is the same rule the Ontario net-buy query
uses (``risk/gate.py``), so the two never disagree about what counts as a fill.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentoquant.enums import Action
from agentoquant.ledger.store import HORIZON_DURATIONS, OUTCOME_HORIZONS, LedgerStore

#: A price source: ``(coin, at) -> price`` in quote currency, or ``None`` when unknown.
PriceLookup = Callable[[str, datetime], float | None]

#: The horizons the clock drives. ``exit`` is event-driven (the executor writes it when the position
#: closes), so it is never in this module's due list.
TIME_HORIZONS: tuple[str, ...] = tuple(
    horizon for horizon in OUTCOME_HORIZONS if HORIZON_DURATIONS[horizon] is not None
)

#: The default producer role for an outcome row. ``outcome_meter`` names the stage's writer, the way
#: ``risk_gate`` names the verdict's.
PRODUCER_ROLE = "outcome_meter"

#: Actions that opened the position the outcome is measured against.
OPENING_ACTIONS: frozenset[Action] = frozenset(
    {Action.ENTER_LADDERED, Action.ADD, Action.EVENT_TRADE}
)

#: Actions that left no position behind, so ``still_open`` is false whatever the fill says.
CLOSING_ACTIONS: frozenset[Action] = frozenset({Action.EXIT})


class OutcomeRecordingError(RuntimeError):
    """The pass could not be run: an unknown horizon, or an unusable price source."""


# ----------------------------------------------------------------------------------------------
# Reading what the ledger already knows
# ----------------------------------------------------------------------------------------------


def latest_fill(store: LedgerStore, decision_card_id: str) -> dict[str, Any] | None:
    """The card's most recent execution row, when it carries a real fill, else ``None``.

    A non-null ``fill_price`` **and** ``fill_qty`` is the signal, deliberately not the ``status``
    string: the same rule drives the Ontario net-buy query, and a status field that says
    ``unfilled_timeout`` next to a real fill is a writer bug (F1), not a reason to ignore the fill.
    """
    rows = store.query(
        'SELECT "fill_price", "fill_qty", "fee_paid", "status", "ts" FROM "execution" '
        'WHERE "decision_card_id" = ? ORDER BY "ts" DESC, "record_id" DESC LIMIT 1',
        [decision_card_id],
    )
    if not rows:
        return None
    row = rows[0]
    if row.get("fill_price") is None or row.get("fill_qty") is None:
        return None
    return row


def still_open(action: str | None, fill: dict[str, Any] | None) -> bool:
    """Whether a position from this card is still open as far as the ledger knows.

    False when nothing was filled (a rejected or vetoed card opened nothing) and false for an
    ``exit`` card (the position is closed by definition). Everything else that filled is treated as
    open: Phase 0 has no position tracker, so "no evidence that it closed" is the honest answer, and
    the exit horizon is written by the executor when it does close.
    """
    if fill is None:
        return False
    try:
        parsed = Action(action) if action is not None else None
    except ValueError:
        parsed = None
    return parsed not in CLOSING_ACTIONS


def outcome_fields(
    store: LedgerStore,
    due_row: Mapping[str, Any],
    *,
    price_lookup: PriceLookup | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """The payload fields for one due ``(card, horizon)``: PnL when it is knowable, nulls when not."""
    card_id = str(due_row["decision_card_id"])
    moment = as_of or datetime.now(UTC)
    fill = latest_fill(store, card_id)
    coin = due_row.get("coin")
    price: float | None = None
    if price_lookup is not None and coin:
        price = price_lookup(str(coin), moment)

    entry = float(fill["fill_price"]) if fill and fill.get("fill_price") else None
    qty = float(fill["fill_qty"]) if fill and fill.get("fill_qty") else 0.0
    fee = float(fill.get("fee_paid") or 0.0) if fill else 0.0

    pnl_pct: float | None = None
    pnl_abs: float | None = None
    realized_up: bool | None = None
    if entry and price is not None:
        pnl_pct = round((price - entry) / entry * 100.0, 8)
        pnl_abs = round((price - entry) * qty - fee, 8)
        realized_up = bool(price > entry)

    return {
        "pnl_pct": pnl_pct,
        "pnl_abs": pnl_abs,
        "realized_up": realized_up,
        "still_open": still_open(due_row.get("action"), fill),
    }


# ----------------------------------------------------------------------------------------------
# The pass
# ----------------------------------------------------------------------------------------------


def record_due_outcomes(
    store: LedgerStore,
    *,
    as_of: datetime | None = None,
    horizons: Sequence[str] = TIME_HORIZONS,
    price_lookup: PriceLookup | None = None,
    producer_role: str = PRODUCER_ROLE,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write every outcome row whose horizon has elapsed and is not yet in the ledger.

    Idempotent by construction: the due list is "cards whose horizon has passed and which have no
    row for it yet", so a second run in the same hour writes nothing and a run after downtime fills
    exactly the horizons that elapsed. ``as_of`` defaults to now (UTC); pass it to backfill or to
    test against a fixed instant. ``price_lookup`` supplies the horizon price (Task 7/12's job);
    without it the rows are written with honest nulls.
    """
    moment = as_of or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)

    requested = list(horizons)
    unknown = [horizon for horizon in requested if horizon not in HORIZON_DURATIONS]
    if unknown:
        raise OutcomeRecordingError(
            f"unknown outcome horizon(s) {unknown}; expected one of {', '.join(OUTCOME_HORIZONS)}"
        )
    if "exit" in requested:
        raise OutcomeRecordingError(
            "the exit horizon is event-driven: the executor writes it when the position closes, so "
            "it is never part of a clock-driven outcome pass"
        )

    written: list[dict[str, Any]] = []
    by_horizon: dict[str, int] = {horizon: 0 for horizon in requested}
    for horizon in requested:
        for due_row in store.due_outcomes(horizon, as_of=moment):
            fields = outcome_fields(store, due_row, price_lookup=price_lookup, as_of=moment)
            record_id: str | None = None
            if not dry_run:
                record_id = store.write_outcome(
                    str(due_row["decision_card_id"]),
                    horizon,
                    cycle_id=str(due_row["cycle_id"]),
                    producer_role=producer_role,
                    **fields,
                )
            written.append(
                {
                    "record_id": record_id,
                    "decision_card_id": due_row["decision_card_id"],
                    "cycle_id": due_row["cycle_id"],
                    "coin": due_row.get("coin"),
                    "action": due_row.get("action"),
                    "horizon": horizon,
                    "due_at": due_row.get("due_at"),
                    "verdict": due_row.get("verdict"),
                    "human_command": due_row.get("human_command"),
                    **fields,
                }
            )
            by_horizon[horizon] += 1

    return {
        "status": "ok",
        "as_of": moment.isoformat(),
        "horizons": requested,
        "due_count": len(written),
        "written_count": 0 if dry_run else len(written),
        "by_horizon": by_horizon,
        "dry_run": dry_run,
        "producer_role": producer_role,
        "rows": written,
        "message": (
            f"outcome pass at {moment.isoformat()}: "
            f"{len(written)} row(s) due"
            + (" (dry run, nothing written)" if dry_run else ", written")
            + (
                "; "
                + ", ".join(f"{horizon}={count}" for horizon, count in by_horizon.items())
                if by_horizon
                else ""
            )
        ),
    }


def price_lookup_from(prices: Mapping[str, float] | None) -> PriceLookup | None:
    """A :data:`PriceLookup` over a fixed ``{coin: price}`` mapping, or ``None`` when there is none."""
    if not prices:
        return None
    fixed = {str(coin).upper(): float(price) for coin, price in prices.items()}

    def lookup(coin: str, at: datetime) -> float | None:
        del at
        return fixed.get(coin.upper())

    return lookup


def load_prices(path: Path | str) -> dict[str, float]:
    """Read a ``{coin: price}`` JSON object from ``path``."""
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OutcomeRecordingError(f"could not read prices from {path}: {exc}") from exc
    if not isinstance(body, dict):
        raise OutcomeRecordingError(f"prices file {path} must hold a JSON object of coin to price")
    return {str(coin): float(price) for coin, price in body.items()}


# ----------------------------------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------------------------------


def run(
    *,
    as_of: datetime | None = None,
    horizons: Sequence[str] = TIME_HORIZONS,
    prices: Mapping[str, float] | None = None,
    dry_run: bool = False,
    store: LedgerStore | None = None,
) -> dict[str, Any]:
    """The CLI-shaped entry point: an outcome pass over the default ledger."""
    return record_due_outcomes(
        store or LedgerStore(),
        as_of=as_of,
        horizons=horizons,
        price_lookup=price_lookup_from(prices),
        dry_run=dry_run,
    )


def _parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutcomeRecordingError(f"--as-of expects an ISO-8601 instant, got {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m agentoquant.ledger.outcomes``: one idempotent outcome pass."""
    parser = argparse.ArgumentParser(
        prog="python -m agentoquant.ledger.outcomes",
        description=(
            "Write the outcome rows whose horizon (+1h, +4h, +24h) has elapsed. Idempotent: "
            "horizons already recorded, and horizons not yet due, are left alone."
        ),
    )
    parser.add_argument("--as-of", default=None, help="ISO instant to treat as now (UTC)")
    parser.add_argument(
        "--horizon",
        action="append",
        default=[],
        help=f"limit the pass to one horizon (repeatable); default {', '.join(TIME_HORIZONS)}",
    )
    parser.add_argument(
        "--price", action="append", default=[], help="horizon price as COIN=VALUE (repeatable)"
    )
    parser.add_argument("--prices", default=None, help="JSON file of coin to price")
    parser.add_argument("--dry-run", action="store_true", help="report the due rows, write nothing")
    parser.add_argument("--json", dest="json_output", action="store_true", help="emit JSON")
    parser.add_argument("--ledger", default=None, help="ledger file (default: the configured one)")
    args = parser.parse_args(argv)

    prices: dict[str, float] = dict(load_prices(args.prices)) if args.prices else {}
    for item in args.price:
        coin, separator, value = item.partition("=")
        if not separator or not coin:
            print(f"error: --price expects COIN=VALUE, got {item!r}", file=sys.stderr)
            return 2
        try:
            prices[coin] = float(value)
        except ValueError:
            print(f"error: --price {item!r} is not a number", file=sys.stderr)
            return 2

    try:
        result = run(
            as_of=_parse_instant(args.as_of),
            horizons=tuple(args.horizon) or TIME_HORIZONS,
            prices=prices or None,
            dry_run=args.dry_run,
            store=LedgerStore(args.ledger) if args.ledger else None,
        )
    except OutcomeRecordingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(result["message"])
        for row in result["rows"]:
            print(
                f"  {row['horizon']:>3}  {row['decision_card_id']}  {row['coin']}  "
                f"pnl_pct={row['pnl_pct']}  realized_up={row['realized_up']}  "
                f"still_open={row['still_open']}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())


__all__ = [
    "CLOSING_ACTIONS",
    "OPENING_ACTIONS",
    "PRODUCER_ROLE",
    "TIME_HORIZONS",
    "OutcomeRecordingError",
    "PriceLookup",
    "latest_fill",
    "load_prices",
    "main",
    "outcome_fields",
    "price_lookup_from",
    "record_due_outcomes",
    "run",
    "still_open",
]
