"""The hourly paper loop. Owned by Task 6. Dry-run only: nothing here places a real order.

One cycle is:

1. the decision source produces a :class:`~agentoquant.ledger.schema.DecisionCard` (Phase 0 uses the
   placeholder source behind ``--placeholder``; Phase 2 replaces it with the agent cascade),
2. the card is written to the ledger as a ``decision_card`` record,
3. the deterministic Risk Gate evaluates it and its ``risk_gate_verdict`` record is written,
4. the card and verdict are published atomically to the signal store, which is all freqtrade reads,
5. the order manager turns the verdict into intents and submits them through a transport, writing one
   ``execution`` record per order with the cycle id,
6. the loop reads the bridge's ack, logs the cycle, and sends the card outbound to Telegram.

Unattended by construction: no prompt, no interactive input, one JSON line per cycle in
``logs/paper/cycles.jsonl``, and a failed cycle is recorded and logged without stopping the run. The
CLI entry point is ``agentoquant paper`` (``COMMAND_TARGETS['paper']``), and the systemd user timer in
``deploy/`` calls it once an hour.

Phase 0 has no ingest and no forecast, so the market and portfolio context handed to the Risk Gate is a
**placeholder**: a fixed, clearly labelled market snapshot. Phase 2 swaps in the real brief. Every
cycle summary records ``context_source: "placeholder"`` so a reader is never misled about it.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentoquant.config_loader import (
    load_fee_tiers,
    load_risk_limits,
    load_settings,
    repo_root,
)
from agentoquant.enums import Action, Harness, ModelFamily, Sleeve, Stage
from agentoquant.execution.freqtrade_strategy import placeholder_card, placeholder_universe
from agentoquant.execution.order_manager import (
    OrderManager,
    OrderTransport,
    freqtrade_dry_run_transport,
)
from agentoquant.execution.signal_store import SignalStore
from agentoquant.execution.telegram_bot import TelegramNotifier
from agentoquant.ledger.store import LedgerStore
from agentoquant.risk import MarketContext, PortfolioContext, PositionContext, RiskGate

#: Where the loop writes its own log, one JSON object per line.
LOG_SUBDIR = "paper"
CYCLE_LOG_NAME = "cycles.jsonl"

#: The placeholder market snapshot the Phase 0 gate is evaluated against. Phase 2 replaces this with
#: the real snapshot from the ingest stage; the numbers are fixed so cycles are reproducible.
PLACEHOLDER_MARKET_VOLUME_USD = 5_000_000.0
PLACEHOLDER_MARKET_SPREAD_PCT = 0.05

#: Placeholder sleeve weights and free cash, percent of book value.
PLACEHOLDER_SLEEVE_WEIGHTS: dict[Sleeve, float] = {Sleeve.A: 60.0, Sleeve.B: 0.0, Sleeve.C: 0.0}
PLACEHOLDER_FREE_CASH: dict[Sleeve, float] = {Sleeve.A: 35.0, Sleeve.B: 0.0, Sleeve.C: 0.0}

#: The reference price the placeholder uses when it has no market data of its own.
PLACEHOLDER_PRICES: dict[str, float] = {"BTC": 60000.0, "ETH": 3000.0, "SOL": 150.0}

#: Stop distance the placeholder asks the bridge to place, in percent below the reference price.
PLACEHOLDER_STOP_PCT = 5.0

#: How long a cycle waits for the bridge's ack before it logs the ack as pending.
DEFAULT_ACK_WAIT_S = 0.0


class PaperLoopError(RuntimeError):
    """A cycle could not run. Recorded against its cycle id; the loop keeps going."""


# ----------------------------------------------------------------------------------------------
# Small helpers: paths, the cycle log, the placeholder context
# ----------------------------------------------------------------------------------------------


def log_dir() -> Path:
    """``settings.log_dir`` under the repository root (gitignored)."""
    try:
        configured = load_settings().log_dir
    except Exception:  # pragma: no cover - a broken config fails closed at CLI startup
        configured = "logs"
    return repo_root() / configured


def cycle_log_path() -> Path:
    return log_dir() / LOG_SUBDIR / CYCLE_LOG_NAME


def append_cycle_log(record: dict[str, Any], *, path: Path | None = None) -> Path:
    """Append one cycle summary as a JSON line, creating the directory when needed."""
    target = path or cycle_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    return target


def placeholder_price(coin: str | None) -> float:
    """A fixed reference price for the placeholder source."""
    if not coin:
        return 0.0
    return PLACEHOLDER_PRICES.get(coin.upper(), 100.0)


def placeholder_context(
    card: Any,
    *,
    price: float,
    card_record_id: str | None,
    book_value_cad: float,
    now: datetime | None = None,
) -> PortfolioContext:
    """The Phase 0 context the Risk Gate evaluates against.

    Labelled placeholder data, not a market snapshot: it exists so the gate has something to evaluate
    while Task 3's ingest is unmerged. The card's own coin gets a tradable market with a fixed volume
    and spread, and a reduction action gets the open position it is reducing.
    """
    coin = (card.coin or "").upper()
    markets = {
        name: MarketContext(
            coin=name,
            volume_24h_usd=PLACEHOLDER_MARKET_VOLUME_USD,
            spread_pct=PLACEHOLDER_MARKET_SPREAD_PCT,
            tradable=True,
        )
        for name in {*placeholder_universe(), coin}
        if name
    }
    positions: tuple[PositionContext, ...] = ()
    if coin and Action(card.action) in {Action.TRIM, Action.EXIT, Action.TRAIL_STOP}:
        positions = (
            PositionContext(
                coin=coin,
                sleeve=card.sleeve or Sleeve.A,
                size_pct=float(card.size_pct or 0.0),
                stop_on_exchange=True,
                venue="kraken",
            ),
        )
    return PortfolioContext(
        book_value_cad=book_value_cad,
        positions=positions,
        sleeve_weights_pct=dict(PLACEHOLDER_SLEEVE_WEIGHTS),
        free_cash_pct_by_sleeve=dict(PLACEHOLDER_FREE_CASH),
        markets=markets,
        venue="kraken",
        venue_status="online",
        decision_card_id=card_record_id,
        order_type="post_only_limit",
        order_purpose="entry",
        stop_on_exchange=True,
        stop_price=round(price * (1 - PLACEHOLDER_STOP_PCT / 100.0), 8) if price else None,
        now=now,
    )


def cycle_cost_usd(ledger: LedgerStore, cycle_id: str) -> float:
    """The LLM cost the ledger recorded for this cycle. Phase 0 makes no model call, so it is 0.0."""
    try:
        rows = ledger.cycle_cost(cycle_id)
    except Exception:
        return 0.0
    total = 0.0
    for row in rows:
        value = row.get("cost_usd")
        if value is not None:
            total += float(value)
    return round(total, 10)


# ----------------------------------------------------------------------------------------------
# One cycle
# ----------------------------------------------------------------------------------------------


def run_cycle(
    *,
    cycle_id: str,
    ledger: LedgerStore,
    store: SignalStore,
    transport: OrderTransport,
    sequence: int = 0,
    placeholder: bool = True,
    manager: OrderManager | None = None,
    notifier: TelegramNotifier | None = None,
    price: float | None = None,
    now: datetime | None = None,
    ack_wait_s: float = DEFAULT_ACK_WAIT_S,
    log_path: Path | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    """Run one full cycle and return its summary. Raises :class:`PaperLoopError` on a hard failure."""
    moment = now or datetime.now(UTC)
    if not placeholder:
        raise PaperLoopError(
            "no decision source: Phase 0 has only the placeholder source, so run "
            "`agentoquant paper --placeholder`. The agent cascade arrives in Phase 2."
        )

    # 1. the decision source
    card = placeholder_card(cycle_id, sequence=sequence)
    card_id = ledger.write(
        Stage.DECISION_CARD,
        cycle_id,
        card,
        producer_role="placeholder_source",
        producer_model_family=ModelFamily.NONE,
        harness=Harness.NONE,
    )

    # 2. the Risk Gate, which can only shrink or reject
    limits = load_risk_limits()
    fee_tiers = load_fee_tiers()
    book_value_cad = float(load_settings().capital.starting_capital)
    reference_price = price if price is not None else placeholder_price(card.coin)
    context = placeholder_context(
        card, price=reference_price, card_record_id=card_id, book_value_cad=book_value_cad, now=moment
    )
    verdict = RiskGate(limits, ledger=ledger).evaluate(card, context)
    verdict_id = ledger.write(
        Stage.RISK_GATE_VERDICT,
        cycle_id,
        verdict,
        producer_role="risk_gate",
        producer_model_family=ModelFamily.NONE,
        harness=Harness.NONE,
    )

    # 3. publish for the bridge, atomically
    signal_path = store.publish(card, verdict, cycle_id=cycle_id)

    # 4. turn the verdict into intents and submit them, dry-run only
    orders = manager or OrderManager(
        ledger=ledger,
        transport=transport,
        limits=limits,
        fee_tiers=fee_tiers,
        book_value_cad=book_value_cad,
    )
    stop_price = context.stop_price
    plan = orders.plan(
        card,
        verdict,
        cycle_id=cycle_id,
        price=reference_price or None,
        stop_price=stop_price,
        target_price=None,
        trail_percent=PLACEHOLDER_STOP_PCT,
        current_stop=stop_price,
    )
    venue_available = bool(transport.available())
    venue_error: str | None = None
    reports: list[dict[str, Any]] = []
    if venue_available:
        try:
            reports = orders.execute(plan, cycle_id=cycle_id, card_id=card_id)
        except Exception as exc:
            # The order never reached the venue, so no execution record is written for it: the cycle
            # is reported as degraded instead, with the error named.
            venue_error = f"{type(exc).__name__}: {exc}"
            venue_available = False

    # 5. the bridge's ack, if it has answered yet
    ack = _await_ack(store, cycle_id, ack_wait_s)

    summary: dict[str, Any] = {
        "cycle_id": cycle_id,
        "sequence": sequence,
        "ran_at": moment.astimezone(UTC).isoformat(),
        "action": card.action.value,
        "coin": card.coin,
        "verdict": verdict.verdict,
        "rule_fired": verdict.rule_fired,
        "size_pct": verdict.final_size_pct,
        "path": plan.path,
        "intents": len(plan.intents),
        "orders": sum(1 for r in reports if r.get("submitted")),
        "strategy_paths": sum(1 for r in reports if r.get("handled_by") == "strategy"),
        "refused": sum(1 for r in reports if r.get("rule_fired")),
        "errors": sum(1 for r in reports if r.get("error")),
        "fills": sum(1 for r in reports if r.get("status") == "filled"),
        "fees_paid": round(sum(float(r.get("fee_paid") or 0.0) for r in reports), 10),
        "fee_tier": fee_tiers.current_tier,
        "cost_usd": cycle_cost_usd(ledger, cycle_id),
        "ack": "acked" if ack else "pending",
        "venue_available": venue_available,
        "venue_error": venue_error,
        "degraded": not venue_available and bool(plan.intents),
        "context_source": "placeholder",
        "signal_path": str(signal_path),
        "decision_card_id": card_id,
        "risk_gate_verdict_id": verdict_id,
        "execution_ids": [r.get("record_id") for r in reports if r.get("record_id")],
        "execution_rows": [
            {
                "action": r.get("action"),
                "path": r.get("path"),
                "freqtrade_call": r.get("freqtrade_call"),
                "submitted": bool(r.get("submitted")),
                "status": r.get("status"),
                "reason": r.get("reason"),
                "rule_fired": r.get("rule_fired"),
            }
            for r in reports
        ],
        "status": "ok",
    }
    append_cycle_log(summary, path=log_path)

    if notify and notifier is not None:
        notifier.send_card(card)
        notifier.send_paper_result(summary)
    return summary


def _await_ack(store: SignalStore, cycle_id: str, wait_s: float) -> dict[str, Any] | None:
    """Wait up to ``wait_s`` seconds for the bridge's ack, then report what is there."""
    ack = store.read_ack(cycle_id)
    deadline = time.monotonic() + max(0.0, float(wait_s))
    while ack is None and time.monotonic() < deadline:
        time.sleep(0.25)
        ack = store.read_ack(cycle_id)
    return ack


# ----------------------------------------------------------------------------------------------
# The loop and the CLI entry point
# ----------------------------------------------------------------------------------------------


def sequence_for(moment: datetime) -> int:
    """The placeholder pattern step that belongs to ``moment``'s hour.

    The pattern advances one step per cycle, and the soak runs a *fresh process* every hourly tick.
    With a per-invocation counter each tick restarted at the first action, so a 3-day soak would
    have exercised ``enter_laddered`` and nothing else. Anchoring the step to the hour it belongs to
    makes the rotation stateless: consecutive ticks are one hour apart, so each lands on the next
    action, and a single multi-hour invocation still walks forward from there.
    """
    return int(moment.timestamp() // 3600)


def run_loop(
    *,
    hours: int = 1,
    placeholder: bool = True,
    cycle_id: str | None = None,
    sleep: bool = True,
    interval_s: float | None = None,
    ledger: LedgerStore | None = None,
    store: SignalStore | None = None,
    transport: OrderTransport | None = None,
    notifier: TelegramNotifier | None = None,
    now: datetime | None = None,
    ack_wait_s: float = DEFAULT_ACK_WAIT_S,
    log_path: Path | None = None,
) -> dict[str, Any]:
    """Run ``hours`` cycles back to back, unattended.

    A cycle that fails is recorded and the loop continues; the run is reported as ``error`` only when
    every cycle failed. ``sleep`` waits one cadence between cycles, which is what makes a multi-hour
    invocation a real soak rather than a burst.
    """
    from agentoquant import scheduler

    cycles = max(1, int(hours))
    ledger = ledger or LedgerStore()
    ledger.migrate()
    store = store or SignalStore()
    transport = transport if transport is not None else freqtrade_dry_run_transport()
    notifier = notifier if notifier is not None else TelegramNotifier()
    moment = now or datetime.now(UTC)
    gap = float(interval_s) if interval_s is not None else float(scheduler.cadence_minutes() * 60)

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    base_sequence = sequence_for(moment)
    for index in range(cycles):
        this_cycle = cycle_id or scheduler.cycle_id_for(moment, sequence=index + 1)
        try:
            results.append(
                run_cycle(
                    cycle_id=this_cycle,
                    ledger=ledger,
                    store=store,
                    transport=transport,
                    sequence=base_sequence + index,
                    placeholder=placeholder,
                    notifier=notifier,
                    now=moment,
                    ack_wait_s=ack_wait_s,
                    log_path=log_path,
                )
            )
        except Exception as exc:
            failure = {
                "cycle_id": this_cycle,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "ran_at": moment.astimezone(UTC).isoformat(),
            }
            append_cycle_log(failure, path=log_path)
            failures.append(failure)
        if sleep and index < cycles - 1:
            time.sleep(gap)

    status = "error" if failures and not results else "ok"
    return {
        "cycles": len(results) + len(failures),
        "completed": len(results),
        "failed": len(failures),
        "status": status,
        "results": results,
        "failures": failures,
        "ledger": str(ledger.db_path),
        "signal_dir": str(store.root),
        "log_path": str(log_path or cycle_log_path()),
    }


def main(cycle_id: str | None = None, placeholder: bool = False, hours: int = 1) -> dict[str, Any]:
    """``agentoquant paper``. Runs the hourly loop in paper mode and returns a CLI-shaped result.

    Dry-run only. The command never prompts, never needs a terminal and never places a real order.

    Without ``--placeholder`` there is no decision source to run: Phase 0 ships only the placeholder
    source and Phase 2 adds the agent cascade. That case reports ``not_implemented`` (the CLI's exit
    code 3) and names the flag that does work, rather than reporting an empty cycle as a success.
    """
    if not placeholder:
        return {
            "message": (
                "not implemented yet (owned by Phase 2): the decision cascade behind `agentoquant "
                "paper`. Phase 0 runs the same loop with a placeholder decision source: "
                "`agentoquant paper --placeholder --hours 1`."
            ),
            "status": "not_implemented",
            "command": "paper",
            "placeholder_available": True,
            "usage": "agentoquant paper --placeholder --hours 1",
        }
    result = run_loop(hours=hours, placeholder=placeholder, cycle_id=cycle_id, sleep=True)
    completed, failed = int(result["completed"]), int(result["failed"])
    message = (
        f"paper loop: {completed} cycle(s) completed, {failed} failed "
        f"(ledger {result['ledger']}, signals {result['signal_dir']})"
    )
    if result["status"] == "error":
        message = f"paper loop: every cycle failed ({failed}); see {result['log_path']}"
    return {
        "message": message,
        "status": "error" if result["status"] == "error" else "ok",
        "cycles": result["cycles"],
        "completed": completed,
        "failed": failed,
        "ledger": result["ledger"],
        "signal_dir": result["signal_dir"],
        "log_path": result["log_path"],
        "results": [
            {
                "cycle_id": row.get("cycle_id"),
                "action": row.get("action"),
                "verdict": row.get("verdict"),
                "path": row.get("path"),
                "intents": row.get("intents"),
                "orders": row.get("orders"),
                "refused": row.get("refused"),
                "errors": row.get("errors"),
                "ack": row.get("ack"),
                "status": row.get("status"),
            }
            for row in result["results"]
        ],
        "failures": result["failures"],
    }


__all__ = [
    "CYCLE_LOG_NAME",
    "DEFAULT_ACK_WAIT_S",
    "LOG_SUBDIR",
    "PLACEHOLDER_PRICES",
    "PLACEHOLDER_STOP_PCT",
    "PaperLoopError",
    "append_cycle_log",
    "cycle_cost_usd",
    "cycle_log_path",
    "log_dir",
    "main",
    "placeholder_context",
    "placeholder_price",
    "run_cycle",
    "run_loop",
]



