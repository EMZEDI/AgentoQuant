"""Risk Gate, kill switch and funding floor. Deterministic code only: it can shrink or reject a
proposal, never enlarge it. Owned by Task 5.

The three modules:

``gate``            :class:`RiskGate` and :class:`PortfolioContext`: every hard limit, each with a
                    named ``rule_fired``, and the invariant ``final_size_pct <= original_size_pct``.
``kill_switch``     ``/flat``, the daily loss halt and the weekly drawdown halt, with an injectable
                    clock. Paper only: it plans closes, it never places one.
``funding_floor``   the free-cash floor and the Funding Request card (``agentoquant fund request``).
                    The agent never moves funds.

Import the names below rather than the submodules, so a rename stays a one-line change.
"""

from __future__ import annotations

from agentoquant.risk.funding_floor import (
    FUNDING_REASON_FLOOR_BREACH,
    FUNDING_REASON_UNSIZEABLE,
    STATUS_REQUESTED,
    build_funding_request,
    free_cash_floor_pct_of_book,
    raise_funding_request,
    render_card,
    requests_this_month,
)
from agentoquant.risk.gate import (
    ALL_RULES,
    INCREASING_ACTIONS,
    REJECT_RULES,
    SHRINK_RULES,
    VERDICT_APPROVED,
    VERDICT_REJECTED,
    VERDICT_SHRUNK,
    MarketContext,
    PortfolioContext,
    PositionContext,
    RiskGate,
    ontario_net_buys_cad_12m,
)
from agentoquant.risk.kill_switch import (
    HALT_DAILY,
    HALT_FLAT,
    HALT_WEEKLY,
    HaltState,
    KillSwitch,
    KillSwitchStatus,
    next_reset,
)

__all__ = [
    "ALL_RULES",
    "FUNDING_REASON_FLOOR_BREACH",
    "FUNDING_REASON_UNSIZEABLE",
    "HALT_DAILY",
    "HALT_FLAT",
    "HALT_WEEKLY",
    "HaltState",
    "INCREASING_ACTIONS",
    "KillSwitch",
    "KillSwitchStatus",
    "MarketContext",
    "PortfolioContext",
    "PositionContext",
    "REJECT_RULES",
    "SHRINK_RULES",
    "STATUS_REQUESTED",
    "VERDICT_APPROVED",
    "VERDICT_REJECTED",
    "VERDICT_SHRUNK",
    "RiskGate",
    "build_funding_request",
    "free_cash_floor_pct_of_book",
    "next_reset",
    "ontario_net_buys_cad_12m",
    "raise_funding_request",
    "render_card",
    "requests_this_month",
]
