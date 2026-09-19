"""The signal store: the only thing the freqtrade bridge reads.

Frozen interface (``docs/phase0_implementation_plan.md`` section 5.6)::

    class SignalStore:
        def publish(self, card: DecisionCard, verdict: RiskGateVerdict, *, cycle_id: str) -> Path
        def latest(self) -> dict | None
        def ack(self, cycle_id: str, result: dict) -> None

One directory holds the current card, a per-cycle history and the strategy's acks::

    <root>/current.json            the document the strategy polls; replaced atomically
    <root>/history/<cycle>.json    every published cycle, kept so a cycle can be replayed
    <root>/acks/<cycle>.json       what the strategy did with that cycle

The root defaults to ``data/signals`` under the repository root (runtime state, gitignored through
``/data/``) and is overridable with ``AGENTOQUANT_SIGNAL_DIR``.

Every write goes to a temporary file in the same directory, is flushed and fsynced, and is then moved
into place with :func:`os.replace`, which is atomic on POSIX. A reader therefore never observes a
partial document: it sees either the previous version or the new one.

The document is plain JSON built from the addendum's shapes, so
``freqtrade_user_data/strategies/AgentBridgeStrategy.py`` can read it with the standard library alone.
The strategy never imports this package and never calls a model.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentoquant.config_loader import load_settings, repo_root
from agentoquant.enums import Action
from agentoquant.ledger.schema import DecisionCard, RiskGateVerdict

#: Directory override, so tests and the systemd unit can point at their own state directory.
SIGNAL_DIR_ENV = "AGENTOQUANT_SIGNAL_DIR"

#: Bumped when the document shape changes; the strategy refuses an unknown version.
DOCUMENT_SCHEMA_VERSION = 1

#: The file the bridge polls.
CURRENT_NAME = "current.json"
HISTORY_DIR = "history"
ACK_DIR = "acks"

#: Actions the bridge may execute. The rejected vocabulary (grid, scalp, hourly rebalance, market
#: chase) is not part of :class:`~agentoquant.enums.Action` at all; ``order_manager`` names the rule
#: that rejects it.
NEUTRAL_ACTIONS: frozenset[Action] = frozenset({Action.HOLD})

#: Actions whose execution path reduces or closes exposure. They are allowed to use a taker order.
REDUCING_ACTIONS: frozenset[Action] = frozenset({Action.TRIM, Action.EXIT, Action.ROTATE})

#: Quote currency for the pair symbol the bridge trades (config/settings.yaml, venue.quote_currency).
def quote_currency() -> str:
    """The configured quote currency, defaulting to USD when the config cannot be read."""
    try:
        return load_settings().venue.quote_currency
    except Exception:  # pragma: no cover - a broken config fails closed at CLI startup
        return "USD"


def pair_for(coin: str, quote: str | None = None) -> str:
    """``BTC`` -> ``BTC/USD``. The bridge trades spot pairs only."""
    return f"{coin.upper()}/{quote or quote_currency()}"


def default_signal_dir() -> Path:
    """``AGENTOQUANT_SIGNAL_DIR`` if set, else ``<repo root>/data/signals``."""
    override = os.environ.get(SIGNAL_DIR_ENV)
    if override:
        return Path(override)
    return repo_root() / "data" / "signals"


class SignalStoreError(RuntimeError):
    """A signal document could not be built, written or read."""


#: How many post-only slices a laddered entry uses by default (plan: two or three).
DEFAULT_LADDER_SLICES = 3

#: Hard time stop for an event trade, in minutes (plan: event trades carry one).
EVENT_TIME_STOP_MINUTES = 24 * 60

#: Actions that open or increase exposure: they always enter post-only.
ENTRY_ACTIONS: frozenset[Action] = frozenset({Action.ENTER_LADDERED, Action.ADD, Action.EVENT_TRADE})


def execution_intent(card: DecisionCard, verdict: RiskGateVerdict) -> dict[str, Any]:
    """The default execution intent the bridge acts on, derived from the card and the verdict.

    ``DecisionCard`` carries no order type, no stop and no target, so this block states what the
    bridge should do with the action and leaves the price levels to the strategy's own config. The
    Risk Gate's ``final_size_pct`` is the only size the bridge may use: a rejected card publishes
    ``size_pct`` 0 and ``approved`` false, and the strategy then does nothing.
    """
    approved = verdict.verdict != "rejected"
    action = Action(card.action)
    if action in ENTRY_ACTIONS:
        side = "buy"
    elif action in REDUCING_ACTIONS:
        side = "sell"
    else:
        side = "none"
    if action in {Action.SET_STOP, Action.TRAIL_STOP}:
        order_type = "stop_loss_limit"
    else:
        order_type = "post_only_limit"
    slices = DEFAULT_LADDER_SLICES if action == Action.ENTER_LADDERED else 2 if action == Action.ADD else 1
    return {
        "approved": approved,
        "side": side,
        "order_type": order_type,
        "size_pct": float(verdict.final_size_pct) if approved else 0.0,
        "original_size_pct": float(verdict.original_size_pct),
        "ladder_slices": slices,
        "stop_price": None,
        "target_price": None,
        "time_stop_minutes": EVENT_TIME_STOP_MINUTES if action == Action.EVENT_TRADE else None,
        "post_only": order_type == "post_only_limit",
        "rule_fired": verdict.rule_fired,
    }


def signal_document(
    card: DecisionCard,
    verdict: RiskGateVerdict,
    *,
    cycle_id: str,
    published_at: datetime | None = None,
) -> dict[str, Any]:
    """The plain-JSON document the bridge polls. Built from the two frozen ledger shapes."""
    if not cycle_id or not str(cycle_id).strip():
        raise SignalStoreError("cycle_id must be a non-empty string")
    if card.cycle_id and card.cycle_id != cycle_id:
        raise SignalStoreError(
            f"card.cycle_id {card.cycle_id!r} does not match the published cycle_id {cycle_id!r}"
        )
    coin = (card.coin or "").upper()
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "published_at": (published_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "action": card.action.value if isinstance(card.action, Action) else str(card.action),
        "coin": coin or None,
        "pair": pair_for(coin) if coin else None,
        "sleeve": card.sleeve.value if card.sleeve is not None else None,
        "confidence": card.confidence,
        "confidence_band": (
            card.confidence_band.value
            if hasattr(card.confidence_band, "value")
            else str(card.confidence_band)
        ),
        "size_pct": card.size_pct,
        "verdict": {
            "verdict": verdict.verdict,
            "rule_fired": verdict.rule_fired,
            "original_size_pct": verdict.original_size_pct,
            "final_size_pct": verdict.final_size_pct,
            "funding_floor_breach": verdict.funding_floor_breach,
        },
        "execution": execution_intent(card, verdict),
        "card": card.model_dump(mode="json"),
    }


def is_fresh(document: dict[str, Any], *, max_age_s: float, now: datetime | None = None) -> bool:
    """Whether a published document is recent enough to act on. A missing or unparsable timestamp is
    treated as stale, which fails closed."""
    stamp = document.get("published_at")
    if not isinstance(stamp, str):
        return False
    try:
        published = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (moment - published).total_seconds() <= max_age_s


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON through a temp file in the same directory, then rename.

    ``os.replace`` is atomic on POSIX, so a concurrent reader sees the whole previous document or the
    whole new one, never a truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.tmp-{os.getpid()}"
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


class SignalStore:
    """The published card, its history and the strategy's acks, in one directory.

    ``publish`` is atomic, ``latest`` returns the current document (or ``None`` before the first
    publish), and ``ack`` records what the strategy did with a cycle. The pipeline and the bridge are
    the only two writers: the pipeline writes ``current.json`` and ``history/``, the bridge writes
    ``acks/``.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else default_signal_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths ----------------------------------------------------------------------------------

    @property
    def current_path(self) -> Path:
        return self.root / CURRENT_NAME

    def history_path(self, cycle_id: str) -> Path:
        return self.root / HISTORY_DIR / f"{_safe_cycle_id(cycle_id)}.json"

    def ack_path(self, cycle_id: str) -> Path:
        return self.root / ACK_DIR / f"{_safe_cycle_id(cycle_id)}.json"

    # -- frozen interface -----------------------------------------------------------------------

    def publish(self, card: DecisionCard, verdict: RiskGateVerdict, *, cycle_id: str) -> Path:
        """Publish ``card`` and ``verdict`` for ``cycle_id`` and return the current document path.

        The history copy is written first, then the current document is replaced atomically, so the
        bridge either keeps reading the previous cycle or picks up the whole new one.
        """
        document = signal_document(card, verdict, cycle_id=cycle_id)
        _atomic_write_json(self.history_path(cycle_id), document)
        _atomic_write_json(self.current_path, document)
        return self.current_path

    def latest(self, *, max_age_s: float | None = None, now: datetime | None = None) -> dict | None:
        """The current document, or ``None`` when nothing has been published yet.

        With ``max_age_s`` set, a document older than that is reported as ``None``, so a stalled
        pipeline cannot leave a stale card live for the bridge.
        """
        document = self._read(self.current_path)
        if document is None:
            return None
        if max_age_s is not None and not is_fresh(document, max_age_s=max_age_s, now=now):
            return None
        return document

    def ack(self, cycle_id: str, result: dict) -> None:
        """Record the strategy's result for ``cycle_id`` (written by the bridge, read by the loop)."""
        if not cycle_id or not str(cycle_id).strip():
            raise SignalStoreError("cycle_id must be a non-empty string")
        if not isinstance(result, dict):
            raise SignalStoreError("result must be a mapping")
        payload = {
            "cycle_id": cycle_id,
            "acked_at": datetime.now(UTC).isoformat(),
            "result": dict(result),
        }
        _atomic_write_json(self.ack_path(cycle_id), payload)

    # -- helpers the loop and the tests use -----------------------------------------------------

    def read_ack(self, cycle_id: str) -> dict | None:
        """The ack for ``cycle_id``, or ``None`` when the bridge has not answered yet."""
        return self._read(self.ack_path(cycle_id))

    def read_history(self, cycle_id: str) -> dict | None:
        """The document published for ``cycle_id``, independent of what is current now."""
        return self._read(self.history_path(cycle_id))

    def acked_cycles(self) -> list[str]:
        """Cycle ids the bridge has acknowledged, newest last by file name."""
        directory = self.root / ACK_DIR
        if not directory.is_dir():
            return []
        return sorted(path.stem for path in directory.glob("*.json"))

    def published_cycles(self) -> list[str]:
        """Cycle ids with a history document, newest last by file name."""
        directory = self.root / HISTORY_DIR
        if not directory.is_dir():
            return []
        return sorted(path.stem for path in directory.glob("*.json"))

    @staticmethod
    def _read(path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise SignalStoreError(f"{path}: could not be read as JSON") from exc
        if not isinstance(payload, dict):
            raise SignalStoreError(f"{path}: expected a JSON object")
        return payload


def _safe_cycle_id(cycle_id: str) -> str:
    """A cycle id is used as a file name, so keep it to characters that cannot escape the directory."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_.:" else "_" for ch in str(cycle_id))
    if not cleaned or cleaned in {".", ".."}:
        raise SignalStoreError(f"unusable cycle_id for a file name: {cycle_id!r}")
    return cleaned


__all__ = [
    "ACK_DIR",
    "CURRENT_NAME",
    "DEFAULT_LADDER_SLICES",
    "DOCUMENT_SCHEMA_VERSION",
    "EVENT_TIME_STOP_MINUTES",
    "HISTORY_DIR",
    "SIGNAL_DIR_ENV",
    "SignalStore",
    "SignalStoreError",
    "default_signal_dir",
    "execution_intent",
    "is_fresh",
    "pair_for",
    "quote_currency",
    "signal_document",
]
