"""AgentBridgeStrategy: the thin freqtrade bridge for AgentoQuant. Interface version 3.

The pipeline decides, this strategy executes. It reads one JSON document that the pipeline publishes
to its signal directory and turns it into force entries, force exits, ladder slices, stop changes and
take-profit exits. It imports the standard library and freqtrade only:

* it never imports the ``agentoquant`` package,
* it never calls a model,
* it never polls Telegram or any remote endpoint,
* it never places a real order, because freqtrade runs in dry-run.

The document it reads (``current.json`` in the signal directory) has this shape::

    {
      "schema_version": 1,
      "cycle_id": "2026-09-19T04Z-0001",
      "published_at": "2026-09-19T04:00:12+00:00",
      "action": "enter_laddered",
      "coin": "BTC",
      "pair": "BTC/USD",
      "sleeve": "A",
      "execution": {
        "approved": true, "side": "buy", "order_type": "post_only_limit",
        "size_pct": 3.0, "ladder_slices": 3, "stop_price": null,
        "target_price": null, "time_stop_minutes": null, "post_only": true
      }
    }

After it acts on a cycle it writes ``acks/<cycle_id>.json`` next to the document, which is how the
hourly loop learns what happened. A missing, stale, rejected or unparsable document means: do nothing.
"""

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from freqtrade.strategy import IStrategy

logger = logging.getLogger(__name__)

#: freqtrade strategy interface this bridge is written against.
INTERFACE_VERSION = 3

#: Document version this bridge understands. An unknown version is ignored.
SIGNAL_SCHEMA_VERSION = 1

SIGNAL_FILENAME = "current.json"
ACK_DIRNAME = "acks"

#: A document older than this is stale, and a stale document places nothing.
SIGNAL_MAX_AGE_SECONDS = 75 * 60

#: Environment override for the signal directory, shared with the pipeline.
SIGNAL_DIR_ENV = "AGENTOQUANT_SIGNAL_DIR"


class AgentBridgeStrategy(IStrategy):
    """Reads the published signal document and executes it in dry-run."""

    INTERFACE_VERSION = 3

    timeframe = "5m"
    can_short = False

    #: The fallback stop, used until the pipeline sends a stop level of its own.
    stoploss = -0.05
    trailing_stop = False

    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 20

    #: Laddered entries, DCA adds and partial exits all run through ``adjust_trade_position``, which
    #: freqtrade only calls when position adjustment is switched on (config
    #: ``position_adjustment_enable``) and this cap is non-zero. With the default 0 those three
    #: paths exist in the order manager's map but can never execute.
    max_entry_position_adjustment = 3

    #: Post-only entries. Stops are the only orders allowed to be taker orders.
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "emergency_exit": "market",
        "force_entry": "limit",
        "force_exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
        "stoploss_on_exchange_interval": 60,
        "stoploss_on_exchange_limit_ratio": 0.99,
    }

    minimal_roi = {"0": 0.04, "30": 0.02, "60": 0.01, "120": 0.0}

    #: Protections live on the strategy in freqtrade 2026.8 (the config key is refused). A cooldown
    #: after an exit, a drawdown brake, a stoploss guard and a low-profit-pair guard.
    protections = [
        {"method": "CooldownPeriod", "stop_duration_candles": 4},
        {
            "method": "MaxDrawdown",
            "lookback_period_candles": 48,
            "trade_limit": 20,
            "stop_duration_candles": 12,
            "max_allowed_drawdown": 0.1,
        },
        {
            "method": "StoplossGuard",
            "lookback_period_candles": 24,
            "trade_limit": 4,
            "stop_duration_candles": 6,
            "only_per_pair": False,
        },
        {
            "method": "LowProfitPairs",
            "lookback_period_candles": 24,
            "trade_limit": 4,
            "stop_duration_candles": 6,
        },
    ]

    #: One ladder slice per hourly cycle at most, so the ladder spreads across cycles.
    max_ladder_slices = 3

    def bot_start(self, **kwargs) -> None:
        """Resolve the signal directory once, from the config or the environment."""
        self.signal_dir = self._resolve_signal_dir()
        self.acked_cycles = {}
        self.ladder_slices = {}
        self.last_stop = {}
        self.event_entries = {}
        logger.info("AgentBridgeStrategy: reading signals from %s", self.signal_dir)

    # -- reading the published document ----------------------------------------------------------

    def _resolve_signal_dir(self) -> Path:
        config = self.config if isinstance(self.config, dict) else {}
        configured = config.get("agent_signal_dir")
        if configured:
            return Path(str(configured)).expanduser()
        from_env = os.environ.get(SIGNAL_DIR_ENV)
        if from_env:
            return Path(from_env).expanduser()
        user_data = config.get("user_data_dir")
        if user_data:
            return Path(str(user_data)).expanduser().parent / "data" / "signals"
        return Path(__file__).resolve().parents[2] / "data" / "signals"

    def signal_directory(self) -> Path:
        return getattr(self, "signal_dir", None) or self._resolve_signal_dir()

    def read_signal(self, now=None) -> dict:
        """The current document, or ``None`` when it is missing, stale or of an unknown version.

        Every failure path returns ``None``: an unreadable or stale document means the bridge places
        nothing, which is the safe direction.
        """
        path = self.signal_directory() / SIGNAL_FILENAME
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict):
            return None
        if document.get("schema_version") != SIGNAL_SCHEMA_VERSION:
            return None
        stamp = document.get("published_at")
        try:
            published = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            return None
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        if (moment - published).total_seconds() > SIGNAL_MAX_AGE_SECONDS:
            return None
        return document

    @staticmethod
    def execution_of(document: dict) -> dict:
        execution = document.get("execution")
        return execution if isinstance(execution, dict) else {}

    def _ack(self, document: dict, result: dict) -> None:
        """Write the ack for a cycle atomically, so the loop never reads a half-written file."""
        cycle_id = str(document.get("cycle_id") or "")
        if not cycle_id:
            return
        safe = "".join(ch if ch.isalnum() or ch in "-_.:" else "_" for ch in cycle_id)
        directory = self.signal_directory() / ACK_DIRNAME
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{safe}.json"
        temp = directory / f".{safe}.json.tmp"
        payload = {
            "cycle_id": cycle_id,
            "acked_at": datetime.now(UTC).isoformat(),
            "result": dict(result),
        }
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)

    # -- indicators: the signal is external, so nothing is computed here --------------------------

    def populate_indicators(self, dataframe, metadata):
        """No indicators: the decision comes from the published document, not from price alone."""
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        """No local entry signal: entries are confirmed against the published document."""
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        """No local exit signal: exits come from ``custom_exit`` and the stop."""
        return dataframe

    # -- the hooks that execute the document ------------------------------------------------------

    def confirm_trade_entry(
        self,
        pair,
        order_type,
        amount,
        rate,
        time_in_force,
        current_time,
        entry_tag,
        side,
        **kwargs,
    ) -> bool:
        """Allow an entry only when the published document asks for one on this pair."""
        document = self.read_signal(now=current_time)
        if not document:
            return False
        execution = self.execution_of(document)
        if not execution.get("approved"):
            self._ack(document, {"action": "skipped", "reason": "the risk gate rejected the card"})
            return False
        if execution.get("side") != "buy" or document.get("pair") != pair:
            self._ack(document, {"action": "skipped", "reason": "not an approved entry for this pair"})
            return False
        self._ack(
            document,
            {
                "action": "entry_confirmed",
                "pair": pair,
                "order_type": order_type,
                "amount": amount,
                "rate": rate,
                "post_only": bool(execution.get("post_only")),
            },
        )
        return True

    def custom_stake_amount(
        self,
        pair,
        current_time,
        current_rate,
        proposed_stake,
        min_stake,
        max_stake,
        leverage,
        entry_tag,
        side,
        **kwargs,
    ) -> float:
        """The stake is the Risk Gate's size percent of the wallet, clamped to freqtrade's limits."""
        document = self.read_signal(now=current_time)
        if not document:
            return proposed_stake
        size_pct = float(self.execution_of(document).get("size_pct") or 0.0)
        if size_pct <= 0:
            return proposed_stake
        total = 0.0
        try:
            total = float(self.wallets.get_total_stake_amount())
        except Exception:
            total = float(self.config.get("dry_run_wallet") or 0.0)
        if total <= 0:
            return proposed_stake
        stake = total * size_pct / 100.0
        if min_stake:
            stake = max(stake, float(min_stake))
        if max_stake:
            stake = min(stake, float(max_stake))
        return stake

    def adjust_trade_position(
        self,
        trade,
        current_time,
        current_rate,
        current_profit,
        min_stake,
        max_stake,
        current_entry_rate,
        current_exit_rate,
        current_entry_profit,
        current_exit_profit,
        **kwargs,
    ):
        """Ladder slices, adds and partial exits, one slice per call.

        A positive return adds to the position; a negative one reduces it. The slice count for a
        cycle is tracked so a ladder places at most ``max_ladder_slices`` slices, and the ladder is
        spread across the calls freqtrade makes rather than all at once.
        """
        document = self.read_signal(now=current_time)
        if not document:
            return None
        if document.get("pair") and document.get("pair") != trade.pair:
            return None
        execution = self.execution_of(document)
        if not execution.get("approved"):
            return None
        action = document.get("action")
        size_pct = float(execution.get("size_pct") or 0.0)
        if size_pct <= 0:
            return None
        stake = self._stake_for(current_rate, size_pct, min_stake, max_stake)

        if action == "add":
            self._ack(document, {"action": "add", "pair": trade.pair, "stake": stake})
            return stake, "dca_add"

        if action == "trim":
            fraction = float(self.config.get("agent_trim_fraction", 0.5) or 0.5)
            reduce_by = min(stake * fraction, float(trade.stake_amount or stake * fraction))
            self._ack(document, {"action": "trim", "pair": trade.pair, "stake": -reduce_by})
            return -reduce_by, "partial_exit"

        if action == "enter_laddered":
            cycle_id = str(document.get("cycle_id") or "")
            wanted = int(execution.get("ladder_slices") or 1)
            wanted = max(1, min(wanted, int(self.max_ladder_slices)))
            placed = int(self.ladder_slices.get(cycle_id, 0))
            if placed >= wanted:
                return None
            self.ladder_slices[cycle_id] = placed + 1
            slice_stake = self._stake_for(current_rate, size_pct / wanted, min_stake, max_stake)
            self._ack(
                document,
                {
                    "action": "ladder_slice",
                    "pair": trade.pair,
                    "slice": placed + 1,
                    "of": wanted,
                    "stake": slice_stake,
                },
            )
            return slice_stake, f"ladder_slice_{placed + 1}"

        return None

    def _stake_for(self, current_rate, size_pct, min_stake, max_stake) -> float:
        """Size a slice from a percent of the wallet, clamped to freqtrade's own limits."""
        total = 0.0
        try:
            total = float(self.wallets.get_total_stake_amount())
        except Exception:
            total = float(self.config.get("dry_run_wallet") or 0.0)
        if total <= 0:
            total = float(self.config.get("stake_amount") or 0.0)
        stake = total * float(size_pct) / 100.0
        if min_stake:
            stake = max(stake, float(min_stake))
        if max_stake:
            stake = min(stake, float(max_stake))
        return stake

    def custom_stoploss(
        self,
        pair,
        trade,
        current_time,
        current_rate,
        current_profit,
        after_fill,
        **kwargs,
    ):
        """Move the stop for a ``trail_stop`` or ``set_stop`` document, and never loosen it.

        Returns a ratio relative to ``current_rate``, as freqtrade's hook requires, and keeps the most
        protective level seen so far for the pair.
        """
        document = self.read_signal(now=current_time)
        if not document or document.get("action") not in {"trail_stop", "set_stop"}:
            return None
        execution = self.execution_of(document)
        stop_price = execution.get("stop_price")
        if stop_price is None:
            trail_pct = float(self.config.get("agent_trail_percent", 5.0) or 5.0)
            stop_price = float(current_rate) * (1.0 - trail_pct / 100.0)
        previous = self.last_stop.get(pair)
        if previous is not None:
            stop_price = max(float(previous), float(stop_price))
        self.last_stop[pair] = float(stop_price)
        ratio = float(stop_price) / float(current_rate) - 1.0
        self._ack(
            document,
            {
                "action": "trail_stop",
                "pair": pair,
                "previous_stop": previous,
                "stop_price": float(stop_price),
                "ratcheted": previous is None or float(stop_price) > float(previous),
            },
        )
        return min(ratio, -0.0001)

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        """Exit reasons the document asks for: the event time stop, the take-profit ladder, an exit."""
        document = self.read_signal(now=current_time)
        if not document:
            return None
        execution = self.execution_of(document)
        action = document.get("action")

        if action == "event_trade":
            minutes = execution.get("time_stop_minutes")
            opened = getattr(trade, "open_date_utc", None)
            if minutes and opened is not None:
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=UTC)
                if (current_time - opened) >= timedelta(minutes=float(minutes)):
                    self._ack(document, {"action": "time_stop", "pair": pair})
                    return "time_stop"

        if action == "take_profit_ladder":
            target = execution.get("target_price")
            if target and float(current_rate) >= float(target):
                self._ack(document, {"action": "take_profit", "pair": pair, "rate": current_rate})
                return "take_profit_ladder"

        if action == "exit" and document.get("pair") == pair:
            self._ack(document, {"action": "exit", "pair": pair})
            return "signal_exit"

        return None


__all__ = ["AgentBridgeStrategy", "INTERFACE_VERSION"]
