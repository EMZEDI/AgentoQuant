"""Typed ledger records: the fourteen stage payload models, the shared envelope, the Market Brief and
the Decision Card.

Literal source of truth: ``tasks/schema_scaffold_addendum.md`` sections 4, 5 and 6, frozen by
``docs/phase0_implementation_plan.md`` section 5.4. Field names are verbatim from the addendum; no
field is renamed, added or dropped, and ``Optional[...]`` is used exactly where the addendum uses it.
Every later task imports its record types from here instead of declaring its own, which is what keeps
Task 17's brief builder, Task 18's proposal schema and Task 20's Decision Card agreeing on field names.

``MarketBrief`` and ``DecisionCard`` live here rather than in ``agents/`` because Tasks 5, 6, 17, 20
and 21 all import them, and putting them in a Phase 2 module would force Phase 0 to import Phase 2
code (``docs/phase0_implementation_plan.md`` section 5.4, recorded as the smallest deviation that
keeps the addendum's field names literal).

Record ids are ULIDs generated in-house at write time (Crockford base32, 48-bit millisecond timestamp
plus 80-bit randomness, monotonic within a millisecond). No ``ulid``/``uuid7`` dependency is used:
nothing outside the addendum's pinned list is added.

All timestamps are timezone-aware UTC. A naive datetime is rejected at validation time rather than
silently assumed to be UTC.
"""

from __future__ import annotations

import secrets
import threading
import time
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from agentoquant.enums import (
    Action,
    ConfidenceBand,
    Harness,
    ModelFamily,
    Sleeve,
    SourceClass,
    Stage,
)

# ----------------------------------------------------------------------------------------------
# ULID: 48-bit millisecond timestamp + 80-bit randomness, Crockford base32, monotonic per ms
# ----------------------------------------------------------------------------------------------

#: Crockford base32 alphabet: no I, L, O or U. Lexicographic order == numeric order.
_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LENGTH = 26
_TIMESTAMP_CHARS = 10  # 10 * 5 bits = 50 bits, holding the 48-bit millisecond timestamp
_RANDOM_CHARS = 16  # 16 * 5 bits = 80 bits of randomness


def _encode_base32(value: int, length: int) -> str:
    """Fixed-width Crockford base32 of a non-negative integer, most significant character first."""
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD32[value & 0x1F])
        value >>= 5
    chars.reverse()
    return "".join(chars)


class _UlidFactory:
    """Thread-safe ULID source that never repeats and never goes backwards within a millisecond."""

    __slots__ = ("_lock", "_last_ms", "_last_rand")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ms = 0
        self._last_rand = -1

    def new(self) -> str:
        with self._lock:
            ms = time.time_ns() // 1_000_000
            if ms <= self._last_ms:
                # Same millisecond (or a clock that stepped backwards): keep the last timestamp and
                # increment the random part, so ids stay strictly increasing.
                ms = self._last_ms
                rand = self._last_rand + 1
                if rand >> 80:  # 80-bit overflow: borrow from the next millisecond
                    ms += 1
                    rand = secrets.randbits(80)
            else:
                rand = secrets.randbits(80)
            self._last_ms = ms
            self._last_rand = rand
            return _encode_base32(ms, _TIMESTAMP_CHARS) + _encode_base32(rand, _RANDOM_CHARS)


_ULID_FACTORY = _UlidFactory()


def new_ulid() -> str:
    """Generate a fresh ULID (26 characters, Crockford base32)."""
    return _ULID_FACTORY.new()


def is_valid_ulid(value: object) -> bool:
    """True when ``value`` is a 26-character Crockford base32 ULID."""
    return (
        isinstance(value, str)
        and len(value) == _ULID_LENGTH
        and all(char in _CROCKFORD32 for char in value)
    )


def ulid_timestamp(ulid: str) -> datetime:
    """The UTC timestamp encoded in a ULID's first ten characters."""
    if not is_valid_ulid(ulid):
        raise ValueError("not a ULID")
    millis = 0
    for char in ulid[:_TIMESTAMP_CHARS]:
        millis = (millis << 5) | _CROCKFORD32.index(char)
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


# ----------------------------------------------------------------------------------------------
# Timezone-aware UTC timestamps
# ----------------------------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware UTC (a naive datetime was given)")
    return value.astimezone(UTC)


#: Every timestamp field in the ledger uses this: aware in, UTC out, naive rejected.
UtcDatetime = Annotated[datetime, AfterValidator(_utc)]


class LedgerPayload(BaseModel):
    """Base for every ledger payload: an unknown key is an error, not something to ignore."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)


# ----------------------------------------------------------------------------------------------
# The envelope (addendum section 4)
# ----------------------------------------------------------------------------------------------


class LedgerEnvelope(LedgerPayload):
    """Shared columns on every ledger table. Real columns, not a nested blob."""

    record_id: str  # ULID, generated at write time
    cycle_id: str  # e.g. "2026-09-18T14Z-0001"
    stage: Stage
    ts: UtcDatetime  # UTC
    producer_role: str  # e.g. "analyst_technical", "judge", "risk_gate", "human"
    producer_model_family: ModelFamily
    harness: Harness
    schema_version: int = 1


# ----------------------------------------------------------------------------------------------
# The fourteen stage payloads (addendum section 4)
# ----------------------------------------------------------------------------------------------


class RawSnapshotPayload(LedgerPayload):
    source: str  # kraken | coingecko | twelve_data | alpha_vantage | gold_api | gdelt |
    #              defillama | cryptorank | grok_x_search
    coin_or_series: str
    fields: dict
    quota_remaining: int
    is_stale: bool


class EarlySignalPayload(LedgerPayload):
    source_class: SourceClass
    ticker: Optional[str]
    event_type: str  # listing | unlock | large_transfer | release | headline | policy
    raw_text_or_ref: str
    detected_at: UtcDatetime
    latency_seconds_vs_first_article: Optional[int]


class VerifiedEventPayload(LedgerPayload):
    source_event_ids: list[str]
    evidence_quality: float = Field(ge=0, le=1)  # 0 to 1
    source_class: SourceClass
    corroboration_count: int
    is_primary: bool
    notes: str


class ModelOutputPayload(LedgerPayload):
    model_name: str  # lightgbm_primary | logistic_baseline | autoarima | garch | bayesian_hier |
    #                  river_drift
    coin: str
    horizon: str  # 4h | 24h
    p_up: float
    interval_low: float
    interval_high: float
    interval_method: str  # conformal_enbpi | credible_90 | none
    regime_label: Optional[str]
    model_version: str
    training_window: str


class AnalystReportPayload(LedgerPayload):
    analyst_role: str  # technical | news_policy | social_hype | macro_geopolitics | onchain_usage
    coin: str
    summary: str
    stance: str  # bullish | bearish | neutral
    key_evidence_ids: list[str]
    confidence: float
    schema_valid: bool


class ProposalSamplePayload(LedgerPayload):
    family: ModelFamily
    coin: str
    action: Action
    size_pct: float
    entry_type: str  # post_only_limit | market
    stop: Optional[float]
    target: Optional[float]
    horizon: str  # 4h | 24h | event
    thesis_bullets: list[str]  # exactly 3
    confidence: float
    invalidation_condition: str
    cited_ids: list[str]


class AdversaryObjectionPayload(LedgerPayload):
    against_proposal_id: str
    severity: int = Field(ge=1, le=5)  # 1 to 5
    objection_text: str
    own_p_up: float
    recommended_change: str  # shrink | wait | reject | none
    tool_calls_used: list[str]


class RiskGateVerdict(LedgerPayload):
    """The Risk Gate's verdict (addendum section 4, ``risk_gate_verdict``).

    Task 5's ``agentoquant/risk/gate.py`` returns this model; it imports it from here rather than
    declaring a second shape with the same name.
    """

    decision_card_id: str
    verdict: str  # approved | shrunk | rejected
    rule_fired: Optional[str]
    original_size_pct: float
    final_size_pct: float
    funding_floor_breach: bool


class HumanActionPayload(LedgerPayload):
    decision_card_id: Optional[str]
    command: str  # veto | pause | resume | status | why | flat | fund_approve | fund_decline |
    #               auto_execute_timeout
    actor: str
    responded_at: UtcDatetime
    within_window: bool


class ExecutionPayload(LedgerPayload):
    decision_card_id: str
    order_type: str  # post_only_limit | market | stop_loss | stop_loss_limit
    venue: str  # kraken | bybit | okx
    fill_price: Optional[float]
    fill_qty: Optional[float]
    fee_paid: Optional[float]
    reprice_count: int
    status: str  # filled | partial | unfilled_timeout | cancelled


class OutcomePayload(LedgerPayload):
    decision_card_id: str
    horizon: str  # 1h | 4h | 24h | exit
    pnl_pct: Optional[float]
    pnl_abs: Optional[float]
    realized_up: Optional[bool]
    still_open: bool


class LLMCallPayload(LedgerPayload):
    role: str
    model_family: ModelFamily
    harness: Harness
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    schema_valid: bool
    fallback_triggered: bool


class FundingRequestPayload(LedgerPayload):
    sleeve: Sleeve
    amount: float
    currency: str
    reason: str  # funding_floor_breach | unsizeable_high_confidence_card
    deadline: UtcDatetime
    status: str  # requested | approved | declined | arrived | expired
    requested_at: UtcDatetime
    responded_at: Optional[UtcDatetime]
    arrived_at: Optional[UtcDatetime]


# ----------------------------------------------------------------------------------------------
# Market Brief (addendum section 5)
# ----------------------------------------------------------------------------------------------


class MarketBrief(LedgerPayload):
    """What every analyst and both proposers read. Built fresh each cycle by ``brief_builder.py``."""

    cycle_id: str
    generated_at: UtcDatetime
    token_budget_max: int
    portfolio: dict  # positions, free_cash_by_sleeve: {A, B, C}, sleeve_weights
    risk_budget: dict  # daily_turnover_used, daily_loss_used_pct, drawdown_used_pct,
    #                    positions_open, max_positions
    fee_tier: dict  # {tier_name, maker_pct, taker_pct}
    model_outputs: list[dict]  # one entry per coin/horizon, same fields as ledger.model_output
    early_signals: list[dict]  # verified_event summaries from the last N hours
    verified_events: list[dict]
    regime: dict  # {label, rule_fired}
    worldview_excerpt: dict  # {version, relevant_priors: list[str]}


# ----------------------------------------------------------------------------------------------
# Decision Card (addendum section 6)
# ----------------------------------------------------------------------------------------------


class DecisionCard(LedgerPayload):
    """The Judge's output (Task 20) and what gets posted to Telegram (Task 21).

    The ledger record and the rendered message share this shape, so ``/why`` re-renders the stored
    record. A null ``selected_proposal_id`` means HOLD.
    """

    cycle_id: str
    selected_proposal_id: Optional[str]
    action: Action
    coin: Optional[str]
    sleeve: Optional[Sleeve]
    size_pct: Optional[float]
    confidence: int = Field(ge=0, le=100)  # 0 to 100
    confidence_band: ConfidenceBand
    p_up: float
    interval_low: float
    interval_high: float
    ev_after_fees: float
    fee_tier_assumed: str
    evidence_split: dict  # {primary: int, verified: int, unverified: int}
    strongest_objection: Optional[dict]  # {severity: int, text: str}
    flip_condition: str


# ----------------------------------------------------------------------------------------------
# Stage -> payload model
# ----------------------------------------------------------------------------------------------

#: One payload model per :class:`Stage` value, and therefore one table per stage.
STAGE_PAYLOAD_MODELS: dict[Stage, type[LedgerPayload]] = {
    Stage.RAW_SNAPSHOT: RawSnapshotPayload,
    Stage.EARLY_SIGNAL: EarlySignalPayload,
    Stage.VERIFIED_EVENT: VerifiedEventPayload,
    Stage.MODEL_OUTPUT: ModelOutputPayload,
    Stage.ANALYST_REPORT: AnalystReportPayload,
    Stage.PROPOSAL_SAMPLE: ProposalSamplePayload,
    Stage.ADVERSARY_OBJECTION: AdversaryObjectionPayload,
    Stage.DECISION_CARD: DecisionCard,
    Stage.RISK_GATE_VERDICT: RiskGateVerdict,
    Stage.HUMAN_ACTION: HumanActionPayload,
    Stage.EXECUTION: ExecutionPayload,
    Stage.OUTCOME: OutcomePayload,
    Stage.LLM_CALL: LLMCallPayload,
    Stage.FUNDING_REQUEST: FundingRequestPayload,
}


def payload_model_for(stage: Stage) -> type[LedgerPayload]:
    """The payload model for a stage. Raises ``KeyError`` for anything that is not a Stage."""
    try:
        return STAGE_PAYLOAD_MODELS[stage]
    except KeyError as exc:  # pragma: no cover - Stage is exhaustive by construction
        raise KeyError(f"no payload model for stage {stage!r}") from exc


def is_payload_for(stage: Stage, payload: object) -> bool:
    """True when ``payload`` is the payload model registered for ``stage``."""
    model = STAGE_PAYLOAD_MODELS.get(stage)
    return model is not None and isinstance(payload, model)


def enum_value(value: object) -> object:
    """The stored form of an enum member (its ``value``); anything else passes through."""
    return value.value if isinstance(value, Enum) else value


__all__ = [
    "AdversaryObjectionPayload",
    "AnalystReportPayload",
    "DecisionCard",
    "EarlySignalPayload",
    "ExecutionPayload",
    "FundingRequestPayload",
    "HumanActionPayload",
    "LLMCallPayload",
    "LedgerEnvelope",
    "LedgerPayload",
    "MarketBrief",
    "ModelOutputPayload",
    "OutcomePayload",
    "ProposalSamplePayload",
    "RawSnapshotPayload",
    "RiskGateVerdict",
    "STAGE_PAYLOAD_MODELS",
    "UtcDatetime",
    "VerifiedEventPayload",
    "enum_value",
    "is_payload_for",
    "is_valid_ulid",
    "new_ulid",
    "payload_model_for",
    "ulid_timestamp",
]
