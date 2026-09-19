"""The Risk Gate: deterministic limits that no agent can bypass.

Owned by Task 5. Frozen interface (``docs/phase0_implementation_plan.md`` section 5.7)::

    class RiskGate:
        def __init__(self, limits: RiskLimits, ledger: LedgerStore | None = None): ...
        def evaluate(self, card: DecisionCard, context: PortfolioContext) -> RiskGateVerdict

:class:`~agentoquant.ledger.schema.RiskGateVerdict` **is** the addendum's ``risk_gate_verdict``
payload (section 4). The gate returns it; it never mutates the card. ``final_size_pct <=
original_size_pct`` always, for every rule and every input.

Determinism
-----------
Pure functions over their inputs plus ledger reads and writes. No LLM, no network, no wall-clock
dependence: the only clock is ``PortfolioContext.now`` (defaulting to ``datetime.now(UTC)`` at call
time). Every limit is read from ``config/risk_limits.yaml`` and ``config/sleeves.yaml`` at
construction time; nothing is hard-coded here.

PortfolioContext
----------------
``PortfolioContext`` is not in the addendum. It is the smallest struct that carries what the rules
need, and every field is documented below. It is a frozen dataclass: the gate never mutates its
inputs.

Book
    ``book_value_cad``      total book value in CAD, used to turn a ``size_pct`` into money.
                            Defaults to ``settings.capital.starting_capital`` from
                            ``config/settings.yaml``.
    ``positions``           the open positions: coin, sleeve, size (percent of book value) and
                            whether each already carries a stop on the exchange.
    ``sleeve_weights_pct``  current weight of each sleeve, percent of book value.
    ``free_cash_pct_by_sleeve``
                            free cash held in each sleeve, **percent of book value**. The config's
                            ``funding.free_cash_floor_pct`` is a percent of that sleeve's *target*,
                            so the gate converts it: ``floor_pct_of_book = floor_pct * cap_pct /
                            100`` using the sleeve's ``cap_pct`` from ``config/sleeves.yaml``.
    ``markets``             per-coin market state: 24h quote volume, spread, whether the pair is
                            tradable at all, and how fresh the snapshot is (``as_of``, ``is_stale``).
                            A coin that is absent fails closed (``min_liquidity``); a snapshot that
                            missed a whole cadence fails closed (``stale_market_data``). Staleness
                            stops new exposure only: it never blocks a reduction.

Risk budget used today
    ``daily_turnover_used_pct``, ``daily_loss_used_pct``, ``drawdown_used_pct``,
    ``consecutive_losses``, ``last_loss_at``.

Venue
    ``venue`` and ``venue_status`` (``online`` | ``maintenance`` | ``degraded`` | ``offline``).

Regulatory
    ``ontario_net_buys_cad_12m`` (coin -> cumulative net buys in CAD over the rolling 12-month
    window) and ``usd_cad_rate``. When the mapping is empty and a ledger is attached, the gate
    computes it from the ledger with :func:`ontario_net_buys_cad_12m`.

Execution intent
    ``decision_card_id``    the stored card's ledger ``record_id``. Falls back to a deterministic
                            id derived from the card when the card has not been written yet.
    ``order_type``          what the executor will place: ``post_only_limit`` | ``market`` |
                            ``stop_loss`` | ``stop_loss_limit``. ``DecisionCard`` has no order-type
                            field, so the intent travels here.
    ``order_purpose``       ``entry`` | ``stop_loss`` | ``emergency_exit`` | ``take_profit`` |
                            ``rebalance``; only the purposes listed in
                            ``risk_limits.execution.market_orders_allowed_for`` may use a taker
                            order.
    ``stop_on_exchange``, ``stop_price``
                            the stop the card will place on the exchange. A card that opens a new
                            position without one is rejected.

Halts and clock
    ``halts``               :class:`~agentoquant.risk.kill_switch.HaltState` from the kill switch:
                            ``/flat``, the daily loss halt and the weekly drawdown halt all block new
                            entries here while leaving reductions (``trim``, ``exit``) free to run.
    ``now``                 injectable clock; ``None`` means ``datetime.now(UTC)``.

Only shrink or reject
---------------------
:meth:`RiskGate.evaluate` clamps the final size into ``[0, original_size_pct]`` before returning, so
no combination of inputs can enlarge a proposal.

All twelve vocabulary actions are evaluated by at least one rule that can shrink or reject them;
none of them is a pass-through. Which rules apply to which action:

``INCREASING_ACTIONS`` (``enter_laddered``, ``add``, ``event_trade``, ``rotate``, ``rebalance``)
    every reject rule in ``REJECT_RULES``, then every shrink cap in ``SHRINK_RULES``.
``REDUCING_ACTIONS`` (``trim``, ``exit``)
    never blocked by a halt or by stale market data - a halt closes positions and freezing one is
    the opposite of what it is for - but they must name a coin (``incomplete_card``), must ask for
    a positive size (``missing_size``), and can never reduce more than the position the context
    reports (``reduction_cap``).
``Action.HOLD``
    a no-op, approved at size 0.0 whatever size the card carries (``hold_size_zero``), so a
    nonsensical size on a hold is never published as an approved size.
stop and order management (``set_stop``, ``trail_stop``, ``take_profit_ladder``, ``cancel_order``)
    also never blocked by a halt, but they must name a coin (``incomplete_card``), need a venue that
    is ``online`` (``venue_status``), and a stop-management card must carry the stop it manages
    (``missing_stop``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agentoquant.config_loader import RiskLimits, Sleeves, load_settings, load_sleeves
from agentoquant.enums import Action, ConfidenceBand, Sleeve, Stage
from agentoquant.ledger.schema import DecisionCard, RiskGateVerdict
from agentoquant.ledger.store import LedgerStore
from agentoquant.risk.funding_floor import (
    FUNDING_REASON_FLOOR_BREACH,
    free_cash_floor_pct_of_book,
    raise_funding_request,
)
from agentoquant.risk.kill_switch import HaltState

# ----------------------------------------------------------------------------------------------
# Rule names. Every verdict carries the one rule that bound it.
# ----------------------------------------------------------------------------------------------

#: Rejections.
RULE_UNSUPPORTED_ACTION = "unsupported_action"
RULE_MISSING_SIZE = "missing_size"
RULE_INCOMPLETE_CARD = "incomplete_card"
RULE_UNRESOLVED_SEVERITY5 = "unresolved_severity5_objection"
RULE_KILL_SWITCH_FLAT = "kill_switch_flat"
RULE_DAILY_LOSS_HALT = "daily_loss_halt"
RULE_WEEKLY_DRAWDOWN_HALT = "weekly_drawdown_halt"
RULE_COOLDOWN = "cooldown_after_consecutive_losses"
RULE_VENUE_STATUS = "venue_status"
RULE_SLEEVE_DISABLED = "sleeve_disabled"
RULE_MAX_CONCURRENT_POSITIONS = "max_concurrent_positions"
RULE_CONFIDENCE_BAND_NO_TRADE = "confidence_band_no_trade"
RULE_ENTRY_CONFIDENCE = "entry_confidence_below_sleeve_threshold"
RULE_MISSING_STOP = "missing_stop"
RULE_POST_ONLY_REQUIRED = "post_only_required"
RULE_STALE_MARKET_DATA = "stale_market_data"
RULE_NOT_TRADABLE = "not_tradable"
RULE_MIN_LIQUIDITY = "min_liquidity"
RULE_MAX_SPREAD = "max_spread"
RULE_DAILY_TURNOVER_CAP = "daily_turnover_cap"
RULE_ONTARIO_NET_BUY_CAP = "ontario_net_buy_cap"
RULE_FREE_CASH_FLOOR = "free_cash_floor"

#: Shrinks.
RULE_POSITION_CAP = "position_cap"
RULE_SLEEVE_CAP = "sleeve_cap"
RULE_FREE_CASH = "free_cash"
RULE_REDUCTION_CAP = "reduction_cap"
RULE_HOLD_SIZE_ZERO = "hold_size_zero"

#: Every rule name this module can fire, in evaluation priority order.
REJECT_RULES: tuple[str, ...] = (
    RULE_UNSUPPORTED_ACTION,
    RULE_MISSING_SIZE,
    RULE_INCOMPLETE_CARD,
    RULE_UNRESOLVED_SEVERITY5,
    RULE_KILL_SWITCH_FLAT,
    RULE_DAILY_LOSS_HALT,
    RULE_WEEKLY_DRAWDOWN_HALT,
    RULE_COOLDOWN,
    RULE_VENUE_STATUS,
    RULE_SLEEVE_DISABLED,
    RULE_MAX_CONCURRENT_POSITIONS,
    RULE_CONFIDENCE_BAND_NO_TRADE,
    RULE_ENTRY_CONFIDENCE,
    RULE_MISSING_STOP,
    RULE_POST_ONLY_REQUIRED,
    RULE_STALE_MARKET_DATA,
    RULE_NOT_TRADABLE,
    RULE_MIN_LIQUIDITY,
    RULE_MAX_SPREAD,
    RULE_DAILY_TURNOVER_CAP,
    RULE_ONTARIO_NET_BUY_CAP,
    RULE_FREE_CASH_FLOOR,
)

SHRINK_RULES: tuple[str, ...] = (
    RULE_POSITION_CAP,
    RULE_SLEEVE_CAP,
    RULE_FREE_CASH,
    RULE_REDUCTION_CAP,
    RULE_HOLD_SIZE_ZERO,
)

ALL_RULES: tuple[str, ...] = REJECT_RULES + SHRINK_RULES

#: Verdict strings, verbatim from the addendum's ``risk_gate_verdict``.
VERDICT_APPROVED = "approved"
VERDICT_SHRUNK = "shrunk"
VERDICT_REJECTED = "rejected"

#: Actions that increase risk and therefore face every entry rule. ``rotate`` and ``rebalance`` move
#: value into a sleeve, so they are checked like an entry.
INCREASING_ACTIONS: frozenset[Action] = frozenset(
    {Action.ENTER_LADDERED, Action.ADD, Action.EVENT_TRADE, Action.ROTATE, Action.REBALANCE}
)
#: ``trim`` and ``exit`` only reduce exposure. They are never blocked by a halt (a halt closes
#: positions, it does not freeze them) and never blocked by stale data, but they are bounded by the
#: position they reduce and must name a coin and a size. See :meth:`RiskGate._evaluate_reducing`.
REDUCING_ACTIONS: frozenset[Action] = frozenset({Action.TRIM, Action.EXIT})
#: A no-op and the size-free order management. Neither is a pass-through: ``hold`` is approved at
#: size 0.0, and the management actions must name a coin, need an online venue and (for the stop
#: actions) carry the stop they manage. See :meth:`RiskGate._evaluate_management`.
NEUTRAL_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.HOLD,
        Action.CANCEL_ORDER,
        Action.SET_STOP,
        Action.TRAIL_STOP,
        Action.TAKE_PROFIT_LADDER,
    }
)
#: The management actions that manage a stop, and therefore must carry one.
STOP_MANAGEMENT_ACTIONS: frozenset[Action] = frozenset(
    {Action.SET_STOP, Action.TRAIL_STOP, Action.TAKE_PROFIT_LADDER}
)

#: Assets exempt from Ontario's net-buy cap (CSA rules exclude BTC, ETH, LTC and BCH).
ONTARIO_EXEMPT_COINS: frozenset[str] = frozenset({"BTC", "ETH", "LTC", "BCH"})

#: The only venue status that permits a new entry.
VENUE_STATUS_OK = "online"
VENUE_STATUSES: tuple[str, ...] = ("online", "maintenance", "degraded", "offline")

#: ``post_only_limit`` is the only order type an entry may use (Tier 1 taker round trip is 1.6%).
POST_ONLY_ORDER_TYPE = "post_only_limit"
ORDER_TYPES: tuple[str, ...] = ("post_only_limit", "market", "stop_loss", "stop_loss_limit")

#: DecisionCard has no size field for these actions, so a missing size is not a failure.
ACTIONS_WITHOUT_SIZE: frozenset[Action] = NEUTRAL_ACTIONS


# ----------------------------------------------------------------------------------------------
# PortfolioContext: the smallest struct that carries what the rules need
# ----------------------------------------------------------------------------------------------


def _default_book_value_cad() -> float:
    """``settings.capital.starting_capital`` (CAD) from ``config/settings.yaml``.

    Config-driven rather than invented, and overridable per call. Sizing in production always reads
    live balances from Kraken; this is only the denominator for percent-to-money conversions.
    """
    try:
        return float(load_settings().capital.starting_capital)
    except Exception:  # pragma: no cover - a broken config is a startup failure elsewhere
        return 0.0


@dataclass(frozen=True)
class PositionContext:
    """One open position. ``size_pct`` is percent of book value, the same unit as the card's size."""

    coin: str
    sleeve: Sleeve
    size_pct: float
    stop_on_exchange: bool = False
    venue: str = "kraken"


@dataclass(frozen=True)
class MarketContext:
    """Per-coin market state the liquidity and spread rules need.

    ``tradable`` mirrors Kraken's AssetPairs: a pair that is not listed or not tradeable fails closed.
    """

    coin: str
    volume_24h_usd: float = 0.0
    spread_pct: float = 0.0
    tradable: bool = True
    #: When the snapshot behind these numbers was taken. ``None`` means the caller did not say, and
    #: the gate will not invent a timestamp: an age it cannot know is not called stale.
    as_of: datetime | None = None
    #: The raw snapshot's own staleness flag (``RawSnapshotPayload.is_stale``), carried through so
    #: the gate sees what the ingest stage already knew.
    is_stale: bool = False


@dataclass(frozen=True)
class PortfolioContext:
    """Everything the gate needs beyond the card. See the module docstring for the field meanings."""

    # Book
    book_value_cad: float = field(default_factory=_default_book_value_cad)
    positions: tuple[PositionContext, ...] = ()
    sleeve_weights_pct: Mapping[Sleeve, float] = field(default_factory=dict)
    free_cash_pct_by_sleeve: Mapping[Sleeve, float] = field(default_factory=dict)
    markets: Mapping[str, MarketContext] = field(default_factory=dict)
    #: Override for how old a market snapshot may be before the gate refuses to act on it; ``None``
    #: uses one cadence plus a margin, derived from ``settings.cadence_minutes``.
    market_data_max_age_s: float | None = None

    # Risk budget used today
    daily_turnover_used_pct: float = 0.0
    daily_loss_used_pct: float = 0.0
    drawdown_used_pct: float = 0.0
    consecutive_losses: int = 0
    last_loss_at: datetime | None = None

    # Venue
    venue: str = "kraken"
    venue_status: str = VENUE_STATUS_OK

    # Regulatory
    ontario_net_buys_cad_12m: Mapping[str, float] = field(default_factory=dict)
    usd_cad_rate: float = 1.0

    # Execution intent (DecisionCard carries no order type and no stop)
    decision_card_id: str | None = None
    order_type: str = POST_ONLY_ORDER_TYPE
    order_purpose: str = "entry"
    stop_on_exchange: bool = False
    stop_price: float | None = None

    # Halts (owned by the kill switch) and the clock
    halts: HaltState = field(default_factory=HaltState)
    now: datetime | None = None

    # -- convenience -------------------------------------------------------------------------

    def position_for(self, coin: str | None) -> PositionContext | None:
        """The open position on ``coin``, or ``None``."""
        if coin is None:
            return None
        for position in self.positions:
            if position.coin == coin:
                return position
        return None

    def market_for(self, coin: str | None) -> MarketContext | None:
        """The market state for ``coin``, or ``None`` when it is unknown (which fails closed)."""
        if coin is None:
            return None
        return self.markets.get(coin)

    def clock(self) -> datetime:
        """The evaluation instant: the injected ``now``, or the wall clock in UTC."""
        moment = self.now or datetime.now(UTC)
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)

    def sleeve_weight(self, sleeve: Sleeve) -> float:
        return float(self.sleeve_weights_pct.get(sleeve, 0.0))

    def free_cash(self, sleeve: Sleeve) -> float:
        return float(self.free_cash_pct_by_sleeve.get(sleeve, 0.0))


# ----------------------------------------------------------------------------------------------
# Freshness: the gate will not reason about an old snapshot as if it were a new one
# ----------------------------------------------------------------------------------------------

#: How many decision cadences a market snapshot stays usable for. One cadence plus a full margin: a
#: snapshot that missed a whole cycle is history, not a market.
STALE_MARKET_DATA_CADENCES = 2.0


def default_market_data_max_age_s() -> float:
    """``STALE_MARKET_DATA_CADENCES`` x ``settings.cadence_minutes``, in seconds.

    Read from ``config/settings.yaml`` (60 minutes) rather than invented here, so the staleness
    limit follows the cadence: a faster cadence makes the limit tighter, never looser.
    """
    try:
        minutes = float(load_settings().cadence_minutes)
    except Exception:  # pragma: no cover - a broken config is a startup failure elsewhere
        minutes = 60.0
    return max(1.0, minutes) * 60.0 * STALE_MARKET_DATA_CADENCES


def _as_utc(moment: datetime) -> datetime:
    """A datetime with a timezone, assuming UTC when the caller did not say."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def market_data_is_stale(market: MarketContext, *, now: datetime, max_age_s: float) -> bool:
    """Whether the market state is too old to act on.

    The raw snapshot's own ``is_stale`` flag wins outright. With no ``as_of`` the age is unknown,
    and the gate reports what it cannot know rather than inventing a timestamp: an entry is refused
    on staleness the caller actually declared. Phase 0's placeholder loop does not populate it yet;
    the ingest snapshot carries it, and Phase 2's brief is the caller that passes it in.
    """
    if market.is_stale:
        return True
    if market.as_of is None:
        return False
    age_s = (_as_utc(now) - _as_utc(market.as_of)).total_seconds()
    return age_s > float(max_age_s)


# ----------------------------------------------------------------------------------------------
# Ledger-derived Ontario net buys
# ----------------------------------------------------------------------------------------------

#: Actions whose execution is a buy (adds to the Ontario net-buy total). ``trim``/``exit`` are sells
#: and subtract from it, which is what makes the window a *net* buy window.
BUY_ACTIONS: tuple[str, ...] = (Action.ENTER_LADDERED.value, Action.ADD.value, Action.EVENT_TRADE.value)
SELL_ACTIONS: tuple[str, ...] = (Action.TRIM.value, Action.EXIT.value)


def ontario_net_buys_cad_12m(
    ledger: LedgerStore,
    *,
    as_of: datetime,
    months: int = 12,
    usd_cad_rate: float = 1.0,
) -> dict[str, float]:
    """Cumulative net buys in CAD per coin over the rolling window, computed from the ledger.

    Buys (``enter_laddered``, ``add``, ``event_trade``) add ``fill_price * fill_qty``; sells (``trim``,
    ``exit``) subtract it. The ledger records the quote notional in USD, so ``usd_cad_rate`` converts
    it to CAD; it is a caller-supplied constant, never a network call. Exempt assets (BTC, ETH, LTC,
    BCH) are returned too, so the caller can audit them, but the gate never counts them against the
    cap.
    """
    moment = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    start = moment - timedelta(days=int(months * 30.44))
    rows = ledger.query(
        'SELECT c."coin" AS coin, c."action" AS action, '
        'e."fill_price" AS fill_price, e."fill_qty" AS fill_qty '
        'FROM "execution" e JOIN "decision_card" c ON c."record_id" = e."decision_card_id" '
        'WHERE e."ts" >= ? AND e."fill_price" IS NOT NULL AND e."fill_qty" IS NOT NULL',
        [start],
    )
    totals: dict[str, float] = {}
    for row in rows:
        coin = row.get("coin")
        action = row.get("action")
        if not coin:
            continue
        notional = float(row["fill_price"]) * float(row["fill_qty"]) * float(usd_cad_rate)
        if action in BUY_ACTIONS:
            totals[coin] = totals.get(coin, 0.0) + notional
        elif action in SELL_ACTIONS:
            totals[coin] = totals.get(coin, 0.0) - notional
    return totals


def _action_of(card: DecisionCard) -> Any:
    """The card's action. A ``DecisionCard`` normally carries an :class:`Action` member already."""
    action = card.action
    if isinstance(action, Action):
        return action
    try:
        return Action(action)
    except ValueError:
        return action


def _non_negative(value: float | None) -> float:
    """A size in percent of book value, floored at zero. ``None`` (HOLD, stop management) is 0.0."""
    if value is None:
        return 0.0
    return max(0.0, float(value))


#: The severity at which an adversary objection blocks a new trade outright. ``plan.md``:
#: "act only if ... and no unresolved severity-5 objection remains", and Task 20's acceptance
#: criterion repeats it ("no trade when EV after fees is not positive by margin or a severity-5
#: objection is unresolved").
UNRESOLVED_OBJECTION_SEVERITY = 5


def objection_severity(card: DecisionCard) -> int:
    """The card's strongest objection severity, or 0 when the card carries none.

    A card whose objection is present but carries no usable ``severity`` fails closed: it is
    reported at the blocking severity, because the gate cannot tell a broken objection from a
    serious one, and "unresolved" is the safe reading of an objection it cannot parse.
    """
    objection = card.strongest_objection
    if objection is None:
        return 0
    if isinstance(objection, Mapping):
        raw = objection.get("severity")
    else:
        raw = getattr(objection, "severity", None)
    if raw is None:
        return UNRESOLVED_OBJECTION_SEVERITY
    try:
        return int(raw)
    except (TypeError, ValueError):
        return UNRESOLVED_OBJECTION_SEVERITY


class RiskGate:
    """Deterministic limits. It can shrink or reject a proposal, never enlarge it.

    ``limits`` comes from ``config_risk_limits.yaml`` via ``load_risk_limits()``; the sleeve caps and
    entry-confidence thresholds come from ``config/sleeves.yaml`` via ``load_sleeves()``, loaded once
    at construction. ``ledger`` is optional: with it, every verdict and every funding request is
    written; without it the gate still returns the same verdict and writes nothing.
    """

    def __init__(self, limits: RiskLimits, ledger: LedgerStore | None = None) -> None:
        self.limits = limits
        self.ledger = ledger
        self.sleeves: Sleeves = load_sleeves()
        self.max_market_data_age_s = default_market_data_max_age_s()

    # -- public API ----------------------------------------------------------------------------

    def evaluate(self, card: DecisionCard, context: PortfolioContext) -> RiskGateVerdict:
        """Evaluate one card and return the verdict. The card is never mutated."""
        if not isinstance(card, DecisionCard):
            raise TypeError(f"card must be a DecisionCard, got {type(card).__name__}")
        if not isinstance(context, PortfolioContext):
            raise TypeError(f"context must be a PortfolioContext, got {type(context).__name__}")

        original = _non_negative(card.size_pct)
        now = context.clock()
        action = _action_of(card)

        if not isinstance(action, Action):
            return self._verdict(
                card, context, VERDICT_REJECTED, RULE_UNSUPPORTED_ACTION, original, 0.0, False
            )

        if action in INCREASING_ACTIONS:
            return self._evaluate_increasing(card, context, action, original, now)
        if action in REDUCING_ACTIONS:
            return self._evaluate_reducing(card, context, action, original)
        if action is Action.HOLD:
            return self._evaluate_hold(card, context, original)
        # Everything left is stop or order management. It carries no size the executor uses, so it
        # is never shrunk for size; it is checked against the card, the venue and its own stop.
        return self._evaluate_management(card, context, action, original)

    # -- one path per action class -------------------------------------------------------------

    def _evaluate_increasing(
        self,
        card: DecisionCard,
        context: PortfolioContext,
        action: Action,
        original: float,
        now: datetime,
    ) -> RiskGateVerdict:
        """An entry, add, event trade, rotate or rebalance: every entry rule applies."""
        rejection = self._first_rejection(card, context, action, original, now)
        if rejection is not None:
            rule, breach, shortfall_pct = rejection
            if breach:
                self._raise_request(card, context, shortfall_pct, now)
            return self._verdict(card, context, VERDICT_REJECTED, rule, original, 0.0, breach)

        size = original
        rule_fired: str | None = None
        for rule_name, cap in self._shrink_caps(card, context, action, original):
            if cap < size:
                size = cap
                rule_fired = rule_name

        headroom = self._free_cash_headroom(card, context)
        breach = original > headroom
        if breach:
            self._raise_request(card, context, original - headroom, now)

        if size <= 0.0:
            return self._verdict(
                card,
                context,
                VERDICT_REJECTED,
                rule_fired or RULE_POSITION_CAP,
                original,
                0.0,
                breach,
            )
        verdict = VERDICT_SHRUNK if size < original else VERDICT_APPROVED
        return self._verdict(card, context, verdict, rule_fired, original, size, breach)

    def _evaluate_reducing(
        self,
        card: DecisionCard,
        context: PortfolioContext,
        action: Action,
        original: float,
    ) -> RiskGateVerdict:
        """``trim``/``exit``: a reduction is never blocked, but it is still bounded.

        A halt closes positions and a stale snapshot does not make a position safer to hold, so
        neither one blocks a reduction. What a reduction may not do is name no coin, ask for no
        size, or ask to reduce more than the position the context reports. That position is the only
        bound the gate can verify: when the context does not name the coin at all the size is left
        alone, because the gate will not guess at a position it cannot see, and refusing to reduce
        is the one failure mode worth avoiding.
        """
        del action
        if card.coin is None:
            return self._verdict(
                card, context, VERDICT_REJECTED, RULE_INCOMPLETE_CARD, original, 0.0, False
            )
        if original <= 0.0:
            return self._verdict(
                card, context, VERDICT_REJECTED, RULE_MISSING_SIZE, original, 0.0, False
            )
        held = context.position_for(card.coin)
        if held is None:
            return self._verdict(card, context, VERDICT_APPROVED, None, original, original, False)
        cap = max(0.0, float(held.size_pct))
        if cap <= 0.0:
            return self._verdict(
                card, context, VERDICT_REJECTED, RULE_REDUCTION_CAP, original, 0.0, False
            )
        if cap < original:
            return self._verdict(
                card, context, VERDICT_SHRUNK, RULE_REDUCTION_CAP, original, cap, False
            )
        return self._verdict(card, context, VERDICT_APPROVED, None, original, original, False)

    def _evaluate_hold(
        self, card: DecisionCard, context: PortfolioContext, original: float
    ) -> RiskGateVerdict:
        """``hold``: a no-op, approved at size 0.0 whatever size the card carries.

        A hold places nothing, so echoing the card's size back as an approved ``final_size_pct``
        publishes a size for an action that has none - and the executor takes its size from the
        verdict. A hold that carries a size is therefore shrunk to zero.
        """
        if original <= 0.0:
            return self._verdict(card, context, VERDICT_APPROVED, None, original, 0.0, False)
        return self._verdict(
            card, context, VERDICT_SHRUNK, RULE_HOLD_SIZE_ZERO, original, 0.0, False
        )

    def _evaluate_management(
        self,
        card: DecisionCard,
        context: PortfolioContext,
        action: Action,
        original: float,
    ) -> RiskGateVerdict:
        """``set_stop``, ``trail_stop``, ``take_profit_ladder``, ``cancel_order``.

        Stop and order management is part of closing a position, so a halt does not block it. It
        must still name the coin it manages, and it needs a venue that is ``online``: an order
        handed to a venue in maintenance is an order that silently does nothing.
        """
        if card.coin is None:
            return self._verdict(
                card, context, VERDICT_REJECTED, RULE_INCOMPLETE_CARD, original, 0.0, False
            )
        if context.venue_status != VENUE_STATUS_OK:
            return self._verdict(
                card, context, VERDICT_REJECTED, RULE_VENUE_STATUS, original, 0.0, False
            )
        if (
            action in STOP_MANAGEMENT_ACTIONS
            and not context.stop_on_exchange
            and context.stop_price is None
        ):
            return self._verdict(
                card, context, VERDICT_REJECTED, RULE_MISSING_STOP, original, 0.0, False
            )
        return self._verdict(card, context, VERDICT_APPROVED, None, original, original, False)

    # -- rejections ----------------------------------------------------------------------------

    def _first_rejection(
        self,
        card: DecisionCard,
        context: PortfolioContext,
        action: Action,
        original: float,
        now: datetime,
    ) -> tuple[str, bool, float] | None:
        """The first reject rule that fires, as ``(rule, funding_floor_breach, shortfall_pct)``."""
        limits = self.limits
        sleeve = card.sleeve
        coin = card.coin

        if original <= 0.0:
            return RULE_MISSING_SIZE, False, 0.0
        if coin is None or sleeve is None:
            return RULE_INCOMPLETE_CARD, False, 0.0

        if objection_severity(card) >= UNRESOLVED_OBJECTION_SEVERITY:
            # The adversary's own strongest objection is on the card, and Phase 0 has no Judge
            # stage to resolve it. plan.md lets an entry act only while no unresolved severity-5
            # objection remains, so the gate refuses it here rather than trusting a stage that
            # does not exist yet. It blocks new exposure only: a reduction is never blocked.
            return RULE_UNRESOLVED_SEVERITY5, False, 0.0

        halts = context.halts
        if halts.flat:
            return RULE_KILL_SWITCH_FLAT, False, 0.0
        if halts.daily_halted or context.daily_loss_used_pct >= limits.halts.daily_loss_halt_pct:
            return RULE_DAILY_LOSS_HALT, False, 0.0
        if halts.weekly_halted or context.drawdown_used_pct >= limits.halts.weekly_drawdown_halt_pct:
            return RULE_WEEKLY_DRAWDOWN_HALT, False, 0.0

        if self._cooldown_active(context, now):
            return RULE_COOLDOWN, False, 0.0

        if context.venue_status != VENUE_STATUS_OK:
            return RULE_VENUE_STATUS, False, 0.0

        sleeve_spec = self.sleeves.get(sleeve)
        if not sleeve_spec.enabled:
            return RULE_SLEEVE_DISABLED, False, 0.0

        opening_new_coin = context.position_for(coin) is None
        if opening_new_coin and len(context.positions) >= limits.positions.max_concurrent:
            return RULE_MAX_CONCURRENT_POSITIONS, False, 0.0

        if card.confidence_band == ConfidenceBand.NO_TRADE:
            return RULE_CONFIDENCE_BAND_NO_TRADE, False, 0.0
        if card.confidence < sleeve_spec.entry_confidence:
            return RULE_ENTRY_CONFIDENCE, False, 0.0

        existing = context.position_for(coin)
        if not context.stop_on_exchange and not (existing and existing.stop_on_exchange):
            return RULE_MISSING_STOP, False, 0.0

        allowed_taker_purposes = set(limits.execution.market_orders_allowed_for)
        if limits.execution.post_only_default:
            if context.order_type != POST_ONLY_ORDER_TYPE:
                if context.order_purpose not in allowed_taker_purposes:
                    return RULE_POST_ONLY_REQUIRED, False, 0.0

        market = context.market_for(coin)
        if market is None:
            # No market state means no way to check liquidity or spread: fail closed.
            return RULE_MIN_LIQUIDITY, False, 0.0
        if market_data_is_stale(
            market, now=now, max_age_s=self._market_data_max_age_s(context)
        ):
            # A snapshot that missed a whole cadence is history, not a market, and good numbers from
            # an old snapshot are exactly the stale-data attack this rule exists to stop.
            return RULE_STALE_MARKET_DATA, False, 0.0
        if not market.tradable:
            return RULE_NOT_TRADABLE, False, 0.0
        if market.volume_24h_usd < limits.liquidity.min_24h_volume_usd:
            return RULE_MIN_LIQUIDITY, False, 0.0
        if market.spread_pct > limits.liquidity.max_spread_pct:
            return RULE_MAX_SPREAD, False, 0.0

        if context.daily_turnover_used_pct + original > limits.turnover.daily_turnover_cap_pct:
            # Config note: "Reached the cap means the next entry waits, not that the cap moves."
            return RULE_DAILY_TURNOVER_CAP, False, 0.0

        if self._ontario_rejection(card, context, coin) is not None:
            return RULE_ONTARIO_NET_BUY_CAP, False, 0.0

        headroom = self._free_cash_headroom(card, context)
        if headroom <= 0.0:
            shortfall = max(0.0, original - headroom)
            return RULE_FREE_CASH_FLOOR, True, shortfall

        return None

    def _market_data_max_age_s(self, context: PortfolioContext) -> float:
        """The staleness limit: the context's override, else one cadence plus a margin."""
        if context.market_data_max_age_s is not None:
            return max(0.0, float(context.market_data_max_age_s))
        return self.max_market_data_age_s

    def _cooldown_active(self, context: PortfolioContext, now: datetime) -> bool:
        """Two consecutive losses start a cooldown. An unknown ``last_loss_at`` stays active."""
        limits = self.limits.cooldown
        if context.consecutive_losses < limits.consecutive_losses:
            return False
        if context.last_loss_at is None:
            return True
        last = context.last_loss_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return now - last < timedelta(hours=limits.cooldown_hours)

    def _ontario_rejection(
        self, card: DecisionCard, context: PortfolioContext, coin: str
    ) -> str | None:
        """Non-``None`` when the Ontario net-buy cap blocks this buy outright."""
        if coin in ONTARIO_EXEMPT_COINS:
            return None
        cumulative = self._ontario_cumulative_cad(context, coin)
        if cumulative >= self.limits.regulatory.ontario_net_buy_cap_cad:
            return RULE_ONTARIO_NET_BUY_CAP
        return None

    def _ontario_cumulative_cad(self, context: PortfolioContext, coin: str) -> float:
        """Cumulative 12-month net buys in CAD for ``coin``: the context value, else the ledger's."""
        supplied = context.ontario_net_buys_cad_12m
        if coin in supplied:
            return float(supplied[coin])
        if not supplied and self.ledger is not None:
            computed = ontario_net_buys_cad_12m(
                self.ledger,
                as_of=context.clock(),
                months=self.limits.regulatory.ontario_net_buy_window_months,
                usd_cad_rate=context.usd_cad_rate,
            )
            return float(computed.get(coin, 0.0))
        return 0.0

    # -- shrink caps ---------------------------------------------------------------------------

    def _shrink_caps(
        self,
        card: DecisionCard,
        context: PortfolioContext,
        action: Action,
        original: float,
    ) -> list[tuple[str, float]]:
        """The shrink rules, each as ``(rule_name, largest size it permits)``, in priority order."""
        del action
        sleeve = card.sleeve
        coin = card.coin or ""
        caps: list[tuple[str, float]] = []

        sleeve_spec = self.sleeves.get(sleeve) if sleeve is not None else None
        per_position_cap = self.limits.positions.max_position_pct
        if sleeve_spec is not None:
            per_position_cap = min(per_position_cap, sleeve_spec.position_cap_pct)
        existing = context.position_for(coin)
        held = existing.size_pct if existing is not None else 0.0
        caps.append((RULE_POSITION_CAP, per_position_cap - held))

        if sleeve is not None and sleeve_spec is not None:
            caps.append((RULE_SLEEVE_CAP, sleeve_spec.cap_pct - context.sleeve_weight(sleeve)))

        caps.append((RULE_ONTARIO_NET_BUY_CAP, self._ontario_headroom_pct(context, coin, original)))
        caps.append((RULE_FREE_CASH, self._free_cash_headroom(card, context)))
        return caps

    def _ontario_headroom_pct(
        self, context: PortfolioContext, coin: str, original: float
    ) -> float:
        """How much of ``original`` the remaining Ontario net-buy headroom permits, in percent of book.

        The cap binds only when a book value is known; without one the outright check in
        :meth:`_ontario_rejection` still applies to the already-accumulated total.
        """
        if coin in ONTARIO_EXEMPT_COINS or context.book_value_cad <= 0.0:
            return original
        remaining_cad = (
            self.limits.regulatory.ontario_net_buy_cap_cad - self._ontario_cumulative_cad(context, coin)
        )
        if remaining_cad <= 0.0:
            return 0.0
        return remaining_cad / context.book_value_cad * 100.0

    def _free_cash_headroom(self, card: DecisionCard, context: PortfolioContext) -> float:
        """Free cash above the sleeve's floor, in percent of book value."""
        if card.sleeve is None:
            return 0.0
        floor = free_cash_floor_pct_of_book(card.sleeve, self.limits, self.sleeves)
        return context.free_cash(card.sleeve) - floor

    # -- verdict and ledger --------------------------------------------------------------------

    def _card_id(self, card: DecisionCard, context: PortfolioContext) -> str:
        """The stored card's ``record_id``, or a deterministic id when the card is not stored yet."""
        if context.decision_card_id:
            return context.decision_card_id
        if card.selected_proposal_id:
            return card.selected_proposal_id
        action = _action_of(card)
        name = action.value if isinstance(action, Action) else str(action)
        return f"{card.cycle_id}:{name}:{card.coin or '-'}"

    def _verdict(
        self,
        card: DecisionCard,
        context: PortfolioContext,
        verdict: str,
        rule_fired: str | None,
        original: float,
        final: float,
        funding_floor_breach: bool,
    ) -> RiskGateVerdict:
        """Build the verdict, clamp it so it can never enlarge, and write it to the ledger."""
        original = max(0.0, float(original))
        final = max(0.0, min(float(final), original))
        assert final <= original, "the Risk Gate must never enlarge a proposal"
        if verdict == VERDICT_APPROVED and final < original:  # pragma: no cover - defensive
            verdict = VERDICT_SHRUNK
        payload = RiskGateVerdict(
            decision_card_id=self._card_id(card, context),
            verdict=verdict,
            rule_fired=rule_fired,
            original_size_pct=original,
            final_size_pct=final,
            funding_floor_breach=bool(funding_floor_breach),
        )
        if self.ledger is not None:
            self.ledger.write(
                Stage.RISK_GATE_VERDICT, card.cycle_id, payload, producer_role="risk_gate"
            )
        return payload

    def _raise_request(
        self,
        card: DecisionCard,
        context: PortfolioContext,
        shortfall_pct: float,
        now: datetime,
    ) -> str | None:
        """Raise a Funding Request instead of silently shrinking. Never moves funds."""
        if self.ledger is None or card.sleeve is None:
            return None
        amount_cad = max(0.0, shortfall_pct) / 100.0 * max(0.0, context.book_value_cad)
        return raise_funding_request(
            self.ledger,
            sleeve=card.sleeve,
            amount=round(amount_cad, 2),
            reason=FUNDING_REASON_FLOOR_BREACH,
            cycle_id=card.cycle_id,
            now=now,
            monthly_cap=self.limits.funding.monthly_request_cap,
        )


__all__ = [
    "ACTIONS_WITHOUT_SIZE",
    "ALL_RULES",
    "BUY_ACTIONS",
    "INCREASING_ACTIONS",
    "MarketContext",
    "NEUTRAL_ACTIONS",
    "ONTARIO_EXEMPT_COINS",
    "POST_ONLY_ORDER_TYPE",
    "PortfolioContext",
    "PositionContext",
    "REDUCING_ACTIONS",
    "REJECT_RULES",
    "RULE_CONFIDENCE_BAND_NO_TRADE",
    "RULE_COOLDOWN",
    "RULE_DAILY_LOSS_HALT",
    "RULE_DAILY_TURNOVER_CAP",
    "RULE_ENTRY_CONFIDENCE",
    "RULE_FREE_CASH",
    "RULE_HOLD_SIZE_ZERO",
    "RULE_FREE_CASH_FLOOR",
    "RULE_INCOMPLETE_CARD",
    "RULE_KILL_SWITCH_FLAT",
    "RULE_MAX_CONCURRENT_POSITIONS",
    "RULE_MAX_SPREAD",
    "RULE_MIN_LIQUIDITY",
    "RULE_MISSING_SIZE",
    "RULE_MISSING_STOP",
    "RULE_NOT_TRADABLE",
    "RULE_ONTARIO_NET_BUY_CAP",
    "RULE_POSITION_CAP",
    "RULE_REDUCTION_CAP",
    "RULE_POST_ONLY_REQUIRED",
    "RULE_SLEEVE_CAP",
    "RULE_SLEEVE_DISABLED",
    "RULE_STALE_MARKET_DATA",
    "RULE_UNSUPPORTED_ACTION",
    "RULE_VENUE_STATUS",
    "RULE_WEEKLY_DRAWDOWN_HALT",
    "RiskGate",
    "SELL_ACTIONS",
    "SHRINK_RULES",
    "STALE_MARKET_DATA_CADENCES",
    "STOP_MANAGEMENT_ACTIONS",
    "UNRESOLVED_OBJECTION_SEVERITY",
    "VENUE_STATUSES",
    "VENUE_STATUS_OK",
    "VERDICT_APPROVED",
    "VERDICT_REJECTED",
    "VERDICT_SHRUNK",
    "default_market_data_max_age_s",
    "market_data_is_stale",
    "objection_severity",
    "ontario_net_buys_cad_12m",
]
