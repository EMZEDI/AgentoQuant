"""The kill switch: ``/flat``, the daily loss halt and the weekly drawdown halt.

Owned by Task 5. Deterministic state transitions with an **injectable clock**; no LLM, no network,
no exchange call of any kind. This module imports no exchange client and places no order: when a halt
fires it returns the *plans* the executor must carry out (:class:`CloseOrderPlan`, always
``dry_run=True``), and writes the human action to the ledger. Paper mode only.

Three states, and what each does
--------------------------------
``flat``           ``/flat``. Every open position is closed and new entries are blocked until a
                   ``resume``. Shahrad's panic button.
``daily_halt``     The daily loss halt (``halts.daily_loss_halt_pct``, 3 percent). Same effect as
                   ``/flat`` for the rest of the day: positions closed, entries blocked. It clears
                   itself at ``halts.daily_halt_resets_at`` (``00:00`` in ``settings.timezone``,
                   ``America/Toronto``), so the next trading day starts clean.
``weekly_halt``    The weekly drawdown halt (``halts.weekly_drawdown_halt_pct``, 8 percent).
                   ``halts.weekly_halt_requires_human_restart`` is true, so the gate must not clear
                   it: only :meth:`KillSwitch.resume` with ``human=True`` does.

:class:`HaltState` is the value the Risk Gate consumes through ``PortfolioContext.halts``; it blocks
new entries and never blocks a ``trim`` or an ``exit``, because a halt exists to close positions, not
to freeze them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agentoquant.config_loader import RiskLimits, load_risk_limits, load_settings, repo_root
from agentoquant.enums import Sleeve, Stage
from agentoquant.ledger.schema import HumanActionPayload
from agentoquant.ledger.store import LedgerStore

#: Halt reasons.
HALT_FLAT = "flat"
HALT_DAILY = "daily_halt"
HALT_WEEKLY = "weekly_halt"

#: Human-action commands this module writes (the addendum's vocabulary).
COMMAND_FLAT = "flat"
COMMAND_PAUSE = "pause"
COMMAND_RESUME = "resume"

#: The Risk Gate's producer role for the automatic halts.
RISK_GATE_ROLE = "risk_gate"

#: Paper mode. No exchange write endpoint is reachable from this module by construction.
VENUE_WRITES_ENABLED = False

#: The order type a halt uses to close a position: a stop-out must not wait for a maker fill.
CLOSE_ORDER_TYPE = "market"

#: Where the halt state survives between the hourly ticks. The paper loop is a **fresh process** every
#: hour, so a ``/flat`` that lived only in memory would be forgotten by the next tick: the panic
#: button would report success and leave every position open on the tick after it.
STATE_PATH_ENV = "AGENTOQUANT_KILL_SWITCH_STATE"
STATE_FILENAME = "kill_switch.json"


def default_state_path() -> Path:
    """``AGENTOQUANT_KILL_SWITCH_STATE`` if set, else ``<repo root>/data/kill_switch.json``."""
    override = os.environ.get(STATE_PATH_ENV)
    if override:
        return Path(override)
    return repo_root() / "data" / STATE_FILENAME


@dataclass(frozen=True)
class HaltState:
    """The halt flags the Risk Gate reads. ``entries_blocked`` is what stops a new position."""

    flat: bool = False
    daily_halted: bool = False
    weekly_halted: bool = False

    @property
    def entries_blocked(self) -> bool:
        return self.flat or self.daily_halted or self.weekly_halted

    @property
    def reasons(self) -> tuple[str, ...]:
        """Which halts are active, in priority order."""
        active: list[str] = []
        if self.flat:
            active.append(HALT_FLAT)
        if self.daily_halted:
            active.append(HALT_DAILY)
        if self.weekly_halted:
            active.append(HALT_WEEKLY)
        return tuple(active)

    def as_dict(self) -> dict[str, object]:
        return {
            "flat": self.flat,
            "daily_halted": self.daily_halted,
            "weekly_halted": self.weekly_halted,
            "entries_blocked": self.entries_blocked,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class CloseOrderPlan:
    """What the executor must do to flatten one position. This module never places it."""

    coin: str
    sleeve: Sleeve | None
    size_pct: float
    reason: str
    order_type: str = CLOSE_ORDER_TYPE
    venue: str = "kraken"
    dry_run: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "coin": self.coin,
            "sleeve": self.sleeve.value if isinstance(self.sleeve, Sleeve) else self.sleeve,
            "size_pct": self.size_pct,
            "order_type": self.order_type,
            "venue": self.venue,
            "reason": self.reason,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class KillSwitchStatus:
    """The kill switch's full state, as reported to the CLI, Telegram and the ledger."""

    state: HaltState
    entries_blocked: bool
    reasons: tuple[str, ...]
    positions_to_close: tuple[str, ...]
    daily_halt_expires_at: datetime | None
    weekly_halt_requires_human_restart: bool
    resumed_by_human: bool = False
    #: Whether the halt's own flat is still unproven. A halt that could not read the venue, or that
    #: still has positions to close, stays latched and says so: reporting a clean flat when nothing
    #: was closed is the worst failure this control has, because the operator's next action is
    #: predicated on it having worked.
    closes_unconfirmed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "flat": self.state.flat,
            "daily_halted": self.state.daily_halted,
            "weekly_halted": self.state.weekly_halted,
            "entries_blocked": self.entries_blocked,
            "reasons": list(self.reasons),
            "positions_to_close": list(self.positions_to_close),
            "daily_halt_expires_at": (
                self.daily_halt_expires_at.isoformat()
                if self.daily_halt_expires_at is not None
                else None
            ),
            "weekly_halt_requires_human_restart": self.weekly_halt_requires_human_restart,
            "resumed_by_human": self.resumed_by_human,
            "closes_unconfirmed": self.closes_unconfirmed,
        }


def _settings_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(load_settings().timezone)
    except Exception:  # pragma: no cover - a broken config is a startup failure elsewhere
        return ZoneInfo("UTC")


def next_reset(moment: datetime, reset_at: str = "00:00") -> datetime:
    """The next occurrence of ``reset_at`` (``HH:MM``) in the settings timezone, as aware UTC."""
    tz = _settings_timezone()
    local = moment.astimezone(tz)
    hour, minute = (int(part) for part in reset_at.split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(UTC)


class KillSwitch:
    """The kill switch state machine. Inject the clock with ``now=`` for deterministic tests."""

    def __init__(
        self,
        limits: RiskLimits,
        ledger: LedgerStore | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        state_path: Path | str | None = None,
        persist: bool = True,
    ) -> None:
        self.limits = limits
        self.ledger = ledger
        self._clock: Callable[[], datetime] = now or (lambda: datetime.now(UTC))
        self._flat_at: datetime | None = None
        self._daily_halt_at: datetime | None = None
        self._weekly_halt_at: datetime | None = None
        self._pending_closes: tuple[str, ...] = ()
        self._last_reason: str | None = None
        self._resumed_by_human = False
        self._closes_unconfirmed = False
        self.state_path: Path = Path(state_path) if state_path is not None else default_state_path()
        self.persist = bool(persist)
        self.restore()

    # -- persistence ---------------------------------------------------------------------------

    def as_state(self) -> dict[str, object]:
        """The halt state as plain JSON, for the next tick to pick up."""
        return {
            "flat_at": self._flat_at.isoformat() if self._flat_at else None,
            "daily_halt_at": self._daily_halt_at.isoformat() if self._daily_halt_at else None,
            "weekly_halt_at": self._weekly_halt_at.isoformat() if self._weekly_halt_at else None,
            "pending_closes": list(self._pending_closes),
            "last_reason": self._last_reason,
            "resumed_by_human": self._resumed_by_human,
            "closes_unconfirmed": self._closes_unconfirmed,
        }

    def restore(self) -> HaltState:
        """Load the persisted state. A missing or unreadable file means "nothing is halted"."""
        if not self.persist:
            return self.halt_state()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self.halt_state()
        if not isinstance(payload, dict):
            return self.halt_state()

        def moment(key: str) -> datetime | None:
            value = payload.get(key)
            if not isinstance(value, str):
                return None
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

        self._flat_at = moment("flat_at")
        self._daily_halt_at = moment("daily_halt_at")
        self._weekly_halt_at = moment("weekly_halt_at")
        closes = payload.get("pending_closes")
        self._pending_closes = tuple(str(c) for c in closes) if isinstance(closes, list) else ()
        reason = payload.get("last_reason")
        self._last_reason = str(reason) if reason else None
        self._resumed_by_human = bool(payload.get("resumed_by_human"))
        self._closes_unconfirmed = bool(payload.get("closes_unconfirmed"))
        return self.halt_state()

    def save(self) -> None:
        """Write the state atomically, so a reader never sees half a document."""
        if not self.persist:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.parent / f".{self.state_path.name}.tmp-{os.getpid()}"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(self.as_state(), handle, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.state_path)
        except OSError:  # pragma: no cover - an unwritable state dir must not crash the loop
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    # -- clock and state -----------------------------------------------------------------------

    def now(self) -> datetime:
        """The injected instant, always aware and in UTC."""
        moment = self._clock()
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)

    def _daily_halt_active(self) -> bool:
        if self._daily_halt_at is None:
            return False
        expires = next_reset(self._daily_halt_at, self.limits.halts.daily_halt_resets_at)
        return self.now() < expires

    def daily_halt_expires_at(self) -> datetime | None:
        """When the daily halt clears itself, or ``None`` when it is not active."""
        if not self._daily_halt_active():
            return None
        assert self._daily_halt_at is not None
        return next_reset(self._daily_halt_at, self.limits.halts.daily_halt_resets_at)

    def halt_state(self) -> HaltState:
        """The flags the Risk Gate consumes."""
        return HaltState(
            flat=self._flat_at is not None,
            daily_halted=self._daily_halt_active(),
            weekly_halted=self._weekly_halt_at is not None,
        )

    def status(self) -> KillSwitchStatus:
        state = self.halt_state()
        return KillSwitchStatus(
            state=state,
            entries_blocked=state.entries_blocked,
            reasons=state.reasons,
            positions_to_close=self._pending_closes,
            daily_halt_expires_at=self.daily_halt_expires_at(),
            weekly_halt_requires_human_restart=self.limits.halts.weekly_halt_requires_human_restart,
            resumed_by_human=self._resumed_by_human,
            closes_unconfirmed=self._closes_unconfirmed,
        )

    def is_entry_blocked(self) -> bool:
        return self.halt_state().entries_blocked

    # -- triggers ------------------------------------------------------------------------------

    def trigger_flat(
        self,
        *,
        actor: str = "human",
        reason: str = "human /flat",
        positions: Sequence[object] = (),
        cycle_id: str | None = None,
    ) -> KillSwitchStatus:
        """``/flat``: close every position and block new entries until a resume."""
        self._flat_at = self.now()
        self._last_reason = reason
        self._resumed_by_human = False
        self._pending_closes = self._coins(positions)
        # Raising a halt does not confirm it. It is confirmed by a close cycle that read the venue
        # successfully and found nothing left to close, so a halt with positions to close starts
        # unconfirmed and stays that way until that cycle happens.
        self._closes_unconfirmed = bool(self._pending_closes)
        self._record_human_action(COMMAND_FLAT, actor, cycle_id)
        self.save()
        return self.status()

    def trigger_daily_halt(
        self,
        *,
        actor: str = RISK_GATE_ROLE,
        reason: str = "daily loss halt",
        positions: Sequence[object] = (),
        cycle_id: str | None = None,
    ) -> KillSwitchStatus:
        """The daily loss halt: close every position and block entries for the rest of the day."""
        if self._daily_halt_at is None:
            self._daily_halt_at = self.now()
        self._last_reason = reason
        self._pending_closes = self._coins(positions)
        # Raising a halt does not confirm it. It is confirmed by a close cycle that read the venue
        # successfully and found nothing left to close, so a halt with positions to close starts
        # unconfirmed and stays that way until that cycle happens.
        self._closes_unconfirmed = bool(self._pending_closes)
        self._record_human_action(COMMAND_PAUSE, actor, cycle_id)
        self.save()
        return self.status()

    def trigger_weekly_halt(
        self,
        *,
        actor: str = RISK_GATE_ROLE,
        reason: str = "weekly drawdown halt",
        positions: Sequence[object] = (),
        cycle_id: str | None = None,
    ) -> KillSwitchStatus:
        """The weekly drawdown halt: blocked until a **human** restart, never cleared by code."""
        if self._weekly_halt_at is None:
            self._weekly_halt_at = self.now()
        self._last_reason = reason
        self._pending_closes = self._coins(positions)
        # Raising a halt does not confirm it. It is confirmed by a close cycle that read the venue
        # successfully and found nothing left to close, so a halt with positions to close starts
        # unconfirmed and stays that way until that cycle happens.
        self._closes_unconfirmed = bool(self._pending_closes)
        self._record_human_action(COMMAND_PAUSE, actor, cycle_id)
        self.save()
        return self.status()

    def resume(
        self,
        *,
        actor: str = "human",
        human: bool = True,
        cycle_id: str | None = None,
    ) -> KillSwitchStatus:
        """Clear the halts. The weekly drawdown halt clears **only** on a human restart.

        ``human=False`` is the automatic path (a scheduled job, an agent); it clears ``/flat`` and the
        daily halt but leaves the weekly halt standing, which is the point of the rule.
        """
        self._flat_at = None
        self._daily_halt_at = None
        cleared_weekly = False
        if human or not self.limits.halts.weekly_halt_requires_human_restart:
            cleared_weekly = self._weekly_halt_at is not None
            self._weekly_halt_at = None
        self._pending_closes = ()
        self._closes_unconfirmed = False
        self._resumed_by_human = bool(human and cleared_weekly)
        self._record_human_action(COMMAND_RESUME, actor, cycle_id)
        self.save()
        return self.status()

    # -- automatic triggers --------------------------------------------------------------------

    def evaluate(
        self,
        *,
        daily_loss_used_pct: float = 0.0,
        drawdown_used_pct: float = 0.0,
        positions: Sequence[object] = (),
        cycle_id: str | None = None,
    ) -> KillSwitchStatus:
        """Fire the halts the numbers demand. Called once per cycle before the Risk Gate runs."""
        if daily_loss_used_pct >= self.limits.halts.daily_loss_halt_pct:
            self.trigger_daily_halt(
                reason=(
                    f"daily loss {daily_loss_used_pct:.2f}% >= "
                    f"{self.limits.halts.daily_loss_halt_pct:.2f}%"
                ),
                positions=positions,
                cycle_id=cycle_id,
            )
        if drawdown_used_pct >= self.limits.halts.weekly_drawdown_halt_pct:
            self.trigger_weekly_halt(
                reason=(
                    f"drawdown {drawdown_used_pct:.2f}% >= "
                    f"{self.limits.halts.weekly_drawdown_halt_pct:.2f}%"
                ),
                positions=positions,
                cycle_id=cycle_id,
            )
        return self.status()

    # -- closing positions ---------------------------------------------------------------------

    def note_closes_unconfirmed(
        self,
        *,
        reason: str,
        pending: Sequence[object] = (),
        cycle_id: str | None = None,
    ) -> KillSwitchStatus:
        """Record that a halt's flat is **not** proven, and keep it latched.

        Called when the close cycle could not read the venue, or when it still had positions left to
        close. The halt stays raised (entries blocked) and its status says ``closes_unconfirmed``, so
        neither the operator nor the next tick can mistake a failed close for a completed one.
        """
        if pending:
            coins = self._coins(pending)
            # Keep what the previous tick recorded as well: a position the venue could not confirm
            # must not be forgotten just because this tick could not read it either.
            merged = list(dict.fromkeys([*self._pending_closes, *coins]))
            self._pending_closes = tuple(merged)
        self._closes_unconfirmed = True
        self._last_reason = reason
        self.save()
        return self.status()

    def confirm_closes(self, *, cycle_id: str | None = None) -> KillSwitchStatus:
        """Record that a halt's flat is proven: the venue was read and nothing is left open.

        Only this clears ``closes_unconfirmed`` and the pending list. It never clears the halt
        itself - that is ``resume``'s job, and for the weekly drawdown halt only a human may do it.
        """
        self._closes_unconfirmed = False
        self._pending_closes = ()
        self.save()
        return self.status()

    def close_all_orders(
        self,
        positions: Sequence[object],
        *,
        reason: str | None = None,
    ) -> list[CloseOrderPlan]:
        """The plans that flatten every position. Paper only: nothing here is ever placed.

        The module has no exchange client and ``dry_run`` is always ``True``, so a halt can close a
        paper book and nothing else.
        """
        why = reason or self._last_reason or "kill switch"
        return [
            CloseOrderPlan(
                coin=self._coin(position),
                sleeve=self._sleeve(position),
                size_pct=self._size(position),
                reason=why,
            )
            for position in positions
        ]

    # -- helpers -------------------------------------------------------------------------------

    @staticmethod
    def _coin(position: object) -> str:
        for attribute in ("coin", "pair", "symbol"):
            value = getattr(position, attribute, None)
            if value:
                return str(value)
        if isinstance(position, dict):
            for key in ("coin", "pair", "symbol"):
                if position.get(key):
                    return str(position[key])
        return str(position)

    @staticmethod
    def _sleeve(position: object) -> Sleeve | None:
        value = getattr(position, "sleeve", None)
        if value is None and isinstance(position, dict):
            value = position.get("sleeve")
        if isinstance(value, Sleeve):
            return value
        try:
            return Sleeve(str(value)) if value is not None else None
        except ValueError:
            return None

    @staticmethod
    def _size(position: object) -> float:
        value = getattr(position, "size_pct", None)
        if value is None and isinstance(position, dict):
            value = position.get("size_pct")
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _coins(self, positions: Sequence[object]) -> tuple[str, ...]:
        return tuple(self._coin(position) for position in positions)

    def _record_human_action(self, command: str, actor: str, cycle_id: str | None) -> str | None:
        """Write the human action to the ledger. A kill switch command is always in time."""
        if self.ledger is None:
            return None
        moment = self.now()
        payload = HumanActionPayload(
            decision_card_id=None,
            command=command,
            actor=actor,
            responded_at=moment,
            within_window=True,
        )
        resolved_cycle = cycle_id or f"kill-switch-{moment.strftime('%Y-%m-%dT%H:%MZ')}"
        return self.ledger.write(
            Stage.HUMAN_ACTION, resolved_cycle, payload, producer_role=actor
        )


def build_kill_switch(
    limits: RiskLimits | None = None,
    ledger: LedgerStore | None = None,
    *,
    now: Callable[[], datetime] | None = None,
    state_path: Path | str | None = None,
    persist: bool = True,
) -> KillSwitch:
    """The kill switch the production paths use: persisted, so the next tick still knows.

    Nothing in ``agentoquant`` used to construct one, which is how a ``/flat`` could report success
    and leave every position open. The hourly loop builds it here, so the halt state it restores is
    the one a human set on a previous tick.
    """
    resolved = limits if limits is not None else load_risk_limits()
    return KillSwitch(
        resolved,
        ledger=ledger,
        now=now,
        state_path=state_path,
        persist=persist,
    )


__all__ = [
    "CLOSE_ORDER_TYPE",
    "COMMAND_FLAT",
    "COMMAND_PAUSE",
    "COMMAND_RESUME",
    "CloseOrderPlan",
    "HALT_DAILY",
    "HALT_FLAT",
    "HALT_WEEKLY",
    "HaltState",
    "KillSwitch",
    "KillSwitchStatus",
    "RISK_GATE_ROLE",
    "STATE_FILENAME",
    "STATE_PATH_ENV",
    "VENUE_WRITES_ENABLED",
    "build_kill_switch",
    "default_state_path",
    "next_reset",
]
