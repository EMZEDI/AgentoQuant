"""Canonical enums. Single source of truth, verbatim from
``tasks/schema_scaffold_addendum.md`` section 3 (frozen by ``docs/phase0_implementation_plan.md``
section 5.1).

Every task imports these; no task redeclares its own version of the action vocabulary, sleeve names,
confidence bands, source classes, model families, harnesses or ledger stages.
"""

from __future__ import annotations

from enum import Enum


class Action(str, Enum):
    ENTER_LADDERED = "enter_laddered"
    ADD = "add"
    TRIM = "trim"
    EXIT = "exit"
    ROTATE = "rotate"
    SET_STOP = "set_stop"
    TRAIL_STOP = "trail_stop"
    TAKE_PROFIT_LADDER = "take_profit_ladder"
    EVENT_TRADE = "event_trade"
    REBALANCE = "rebalance"
    HOLD = "hold"
    CANCEL_ORDER = "cancel_order"


# Rejected outright by the schema validator (Task 16) and tested against in the Risk Gate
# adversarial suite (Task 5): grid_trade, scalp, hourly_rebalance, market_chase_breakout.
# They are deliberately NOT members of Action.
REJECTED_ACTIONS = ("grid_trade", "scalp", "hourly_rebalance", "market_chase_breakout")


class Sleeve(str, Enum):
    A = "A"  # blue chip and stable
    B = "B"  # AI-serving
    C = "C"  # new / breakout, second exchange only


class ConfidenceBand(str, Enum):
    NO_TRADE = "no_trade"  # confidence < 55
    SMALL = "small"  # 55 to 70
    STANDARD = "standard"  # 70 to 85
    MAX = "max"  # > 85


class SourceClass(str, Enum):
    EXCHANGE_ANNOUNCEMENT = "exchange_announcement"
    ONCHAIN = "onchain"
    OFFICIAL_HANDLE = "official_handle"
    GITHUB_RELEASE = "github_release"
    HEADLINE = "headline"
    TELEGRAM_PREVIEW = "telegram_preview"
    CORROBORATED_TWO_SOURCE = "corroborated_two_source"


class ModelFamily(str, Enum):
    CLAUDE_SONNET = "claude_sonnet"
    DEEPSEEK = "deepseek"
    KIMI = "kimi"
    GLM = "glm"
    GEMINI_FLASH = "gemini_flash"
    NONE = "none"  # deterministic / non-LLM stage


class Harness(str, Enum):
    """Which harness tier runs a role.

    Records the role's model *tier*, not the transport: ``CLAUDE_NATIVE`` means the role runs on the
    Claude model family (currently served over OpenRouter with the project's own key),
    ``HERMES_OPENROUTER`` means a cheap OpenRouter model. See ``docs/HANDOFF.md`` decision 1.
    """

    CLAUDE_NATIVE = "claude_native"
    HERMES_OPENROUTER = "hermes_openrouter"
    NONE = "none"


class Stage(str, Enum):
    RAW_SNAPSHOT = "raw_snapshot"
    EARLY_SIGNAL = "early_signal"
    VERIFIED_EVENT = "verified_event"
    MODEL_OUTPUT = "model_output"
    ANALYST_REPORT = "analyst_report"
    PROPOSAL_SAMPLE = "proposal_sample"
    ADVERSARY_OBJECTION = "adversary_objection"
    DECISION_CARD = "decision_card"
    RISK_GATE_VERDICT = "risk_gate_verdict"
    HUMAN_ACTION = "human_action"
    EXECUTION = "execution"
    OUTCOME = "outcome"
    LLM_CALL = "llm_call"
    FUNDING_REQUEST = "funding_request"


__all__ = [
    "Action",
    "Sleeve",
    "ConfidenceBand",
    "SourceClass",
    "ModelFamily",
    "Harness",
    "Stage",
    "REJECTED_ACTIONS",
]
