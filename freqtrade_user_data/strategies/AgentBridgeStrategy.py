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

#: Ladder offsets below the reference price, as published by the pipeline. The fallback keeps the
#: bridge self-contained when an older document carries none.
DEFAULT_LADDER_OFFSETS_PCT = (0.10, 0.25, 0.40)


def roi_step(ladder: dict, trade, current_time) -> float | None:
    """The minimal-ROI ratio that applies to ``trade`` now, or ``None`` when the ladder is empty.

    The ladder is freqtrade's minimal-ROI form: minutes after entry -> the profit ratio the trade
    must reach from that point on. The applicable step is the one with the largest threshold the
    trade's age has passed, exactly as freqtrade's own ``min_roi_reached`` reads it.
    """
    if not ladder:
        return None
    opened = getattr(trade, "open_date_utc", None)
    if opened is None:
        return None
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=UTC)
    moment = current_time
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    age_minutes = (moment - opened).total_seconds() / 60.0
    steps = []
    for minutes, ratio in ladder.items():
        try:
            steps.append((float(minutes), float(ratio)))
        except (TypeError, ValueError):
            continue
    if not steps:
        return None
    applicable = [ratio for minutes, ratio in sorted(steps) if age_minutes >= minutes]
    if not applicable:
        return None
    return applicable[-1]


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
        #: The limit price the next order for a pair should use, set by the hook that decided the
        #: slice and consumed by ``custom_entry_price``. freqtrade does not take a price from
        #: ``adjust_trade_position``, so without this every slice of a ladder fills at one price.
        self.pending_entry_price = {}
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
        """The stake is the Risk Gate's approved notional, clamped to freqtrade's own limits."""
        document = self.read_signal(now=current_time)
        if not document:
            return proposed_stake
        execution = self.execution_of(document)
        size_pct = float(execution.get("size_pct") or 0.0)
        if size_pct <= 0:
            return proposed_stake
        approved = self._approved_stake(execution, max_stake)
        if approved is not None:
            return approved
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
        stake = self._approved_stake(execution, max_stake)
        if stake is None:
            # No approved notional in the document: fall back to the wallet percentage. A document
            # published before this field existed still places something rather than nothing.
            stake = self._stake_for(current_rate, size_pct, min_stake, max_stake)

        if action == "add":
            floor = float(min_stake or 0.0)
            if floor and stake < floor:
                # Never lift an add to the venue minimum: the Risk Gate approved this size and the
                # execution layer may only shrink or refuse, never enlarge.
                self._ack(
                    document,
                    {
                        "action": "add_refused",
                        "pair": trade.pair,
                        "reason": (
                            f"add of {stake:.8f} is below the venue minimum of {floor:.8f} "
                            "(rule below_min_order_size)"
                        ),
                    },
                )
                return None
            self.pending_entry_price[trade.pair] = self._ladder_price(execution, current_rate, 0)
            self._ack(document, {"action": "add", "pair": trade.pair, "stake": stake})
            return stake, "dca_add"

        if action == "trim":
            fraction = float(self.config.get("agent_trim_fraction", 0.5) or 0.5)
            position_stake = float(trade.stake_amount or 0.0)
            position_amount = float(trade.amount or 0.0)
            if position_stake <= 0 or position_amount <= 0:
                # An empty trade has nothing to reduce, and a negative stake against one is the call
                # freqtrade answers with "Wanted to exit of ... amount, but exit amount is now 0.0
                # due to exchange limits - not exiting": a no-op that used to be acked as a trim.
                self._ack(
                    document,
                    {
                        "action": "trim_refused",
                        "pair": trade.pair,
                        "reason": "the trade holds no position to reduce",
                    },
                )
                return None
            floor = float(min_stake or 0.0)
            reduce_by = min(stake * fraction, position_stake)
            if floor and reduce_by < floor:
                if position_stake > floor:
                    # Shrink by the smallest order the venue accepts rather than asking for one it
                    # will round away to zero.
                    reduce_by = floor
                else:
                    self._ack(
                        document,
                        {
                            "action": "trim_refused",
                            "pair": trade.pair,
                            "reason": (
                                f"exit of {reduce_by:.8f} is below the venue minimum of {floor:.8f} "
                                "and the position cannot carry the minimum"
                            ),
                        },
                    )
                    return None
            self._ack(
                document,
                {"action": "trim", "pair": trade.pair, "stake": -reduce_by, "min_stake": floor},
            )
            return -reduce_by, "partial_exit"

        if action == "enter_laddered":
            cycle_id = str(document.get("cycle_id") or "")
            wanted = int(execution.get("ladder_slices") or 1)
            wanted = max(1, min(wanted, int(self.max_ladder_slices)))
            placed = int(self.ladder_slices.get(cycle_id, 0))
            if placed >= wanted:
                return None
            self.ladder_slices[cycle_id] = placed + 1
            slice_stake = self._slice_stake(execution, size_pct, wanted, min_stake, max_stake)
            limit_price = self._ladder_price(execution, current_rate, placed)
            self.pending_entry_price[trade.pair] = limit_price
            self._ack(
                document,
                {
                    "action": "ladder_slice",
                    "pair": trade.pair,
                    "slice": placed + 1,
                    "of": wanted,
                    "stake": slice_stake,
                    "limit_price": limit_price,
                },
            )
            return slice_stake, f"ladder_slice_{placed + 1}"

        return None

    @staticmethod
    def _ladder_price(execution: dict, current_rate, index: int) -> float:
        """This slice's limit price: its own offset below the rate the venue is trading at.

        The offsets come from the published document, so the pipeline and the bridge cannot disagree
        about what a ladder is. Slice N sits below slice N-1, which is what makes the fills distinct
        instead of three slices at one price.
        """
        offsets = execution.get("ladder_offsets_pct") or list(DEFAULT_LADDER_OFFSETS_PCT)
        try:
            chosen = float(offsets[min(int(index), len(offsets) - 1)])
        except (TypeError, ValueError, IndexError):
            chosen = float(DEFAULT_LADDER_OFFSETS_PCT[0])
        return round(float(current_rate) * (1.0 - chosen / 100.0), 8)

    def custom_entry_price(
        self,
        pair,
        trade,
        current_time,
        proposed_rate,
        entry_tag,
        side,
        **kwargs,
    ):
        """The limit price for a ladder slice, when the slice hook decided one.

        Returns ``None`` (freqtrade's own pricing stands) for every entry that is not a slice this
        strategy sized: the pipeline's force entry carries its own price.
        """
        stashed = self.pending_entry_price.pop(pair, None)
        return stashed

    def _stake_for(self, current_rate, size_pct, min_stake, max_stake) -> float:
        """Size a slice from a percent of the wallet, clamped to freqtrade's own limits.

        Kept as the **fallback** for a document that carries no approved notional. It is not the
        primary path: sizing from the venue's wallet re-derives a number the Risk Gate already
        decided, from a different base.
        """
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

    def _approved_stake(self, execution, max_stake):
        """The notional the Risk Gate approved, from the published document, or ``None``.

        Only ever clamped **down**. The execution layer may shrink or refuse a size, never enlarge it,
        so ``min_stake`` is deliberately not applied here: the caller refuses a below-minimum size
        rather than lifting it to the venue's floor.
        """
        value = execution.get("stake_amount")
        try:
            stake = float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return None
        if stake <= 0:
            return None
        if max_stake:
            stake = min(stake, float(max_stake))
        return stake

    def _slice_stake(self, execution, size_pct, wanted, min_stake, max_stake) -> float:
        """One ladder slice's stake: the approved *money* split evenly, not a re-derived percentage.

        Splitting the money is what makes the slices sum to exactly what the gate approved. Splitting
        the percentage instead and re-deriving each slice from the wallet is how a 3 percent card
        placed 4.999 percent less than it was allowed to.
        """
        approved = self._approved_stake(execution, max_stake)
        if approved is not None:
            return approved / max(1, int(wanted))
        return self._stake_for(None, size_pct / wanted, min_stake, max_stake)

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
            # No target price: the pipeline publishes the ladder itself (minimal-ROI form, minutes
            # after entry -> ratio). Without this the branch could never fire, because a card carries
            # no target price and ``target_price`` was always null.
            step = roi_step(execution.get("minimal_roi") or {}, trade, current_time)
            if step is not None and float(current_profit or 0.0) >= step:
                self._ack(
                    document,
                    {
                        "action": "take_profit_ladder",
                        "pair": pair,
                        "step": step,
                        "current_profit": current_profit,
                        "rate": current_rate,
                    },
                )
                return "take_profit_ladder"

        if action == "exit" and document.get("pair") == pair:
            self._ack(document, {"action": "exit", "pair": pair})
            return "signal_exit"

        return None


__all__ = ["AgentBridgeStrategy", "INTERFACE_VERSION"]
