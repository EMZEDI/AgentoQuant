"""Execution layer: signal store, freqtrade bridge, order manager, Telegram bot and the paper loop.
Owned by Task 6. Paper and dry-run only: nothing in this package places, modifies or cancels a real
order, and nothing moves funds.

The pieces:

``signal_store``       the frozen :class:`SignalStore` (atomic publish, ``latest``, ``ack``) that the
                    freqtrade strategy polls. The strategy never imports this package.
``freqtrade_strategy`` the Python side of the bridge: the Phase 0 placeholder decision source, the
                    strategy paths and the dry-run assertion on the freqtrade config.
``order_manager``      one execution path per action in the vocabulary, with the rejected vocabulary
                    (grid, scalp, hourly rebalance, market chase) refused by a named rule.
``telegram_bot``       outbound-only notifications. Inbound belongs to the Hermes gateway.
``paper``              the hourly loop behind ``agentoquant paper``.

The freqtrade-side half lives at ``freqtrade_user_data/strategies/AgentBridgeStrategy.py`` and is
deliberately outside this package so it cannot import it.
"""

from __future__ import annotations

from agentoquant.execution.freqtrade_strategy import (
    BRIDGE_INTERFACE_VERSION,
    BridgeConfigError,
    assert_dry_run_config,
    check_strategy_hygiene,
    placeholder_card,
    poll_signal,
    strategy_path,
)
from agentoquant.execution.order_manager import (
    EXECUTION_PATHS,
    FREQTRADE_CALLS,
    RULE_REJECTED_ACTION,
    OrderIntent,
    OrderManager,
    UnsupportedActionError,
    coerce_action,
    is_rejected_action,
    ratchet_stop,
)
from agentoquant.execution.paper import run_cycle, run_loop
from agentoquant.execution.signal_store import SignalStore, SignalStoreError, signal_document
from agentoquant.execution.telegram_bot import (
    TelegramNotifier,
    render_decision_card,
    render_paper_result,
)

__all__ = [
    "BRIDGE_INTERFACE_VERSION",
    "EXECUTION_PATHS",
    "FREQTRADE_CALLS",
    "RULE_REJECTED_ACTION",
    "BridgeConfigError",
    "OrderIntent",
    "OrderManager",
    "SignalStore",
    "SignalStoreError",
    "TelegramNotifier",
    "UnsupportedActionError",
    "assert_dry_run_config",
    "check_strategy_hygiene",
    "coerce_action",
    "is_rejected_action",
    "placeholder_card",
    "poll_signal",
    "ratchet_stop",
    "render_decision_card",
    "render_paper_result",
    "run_cycle",
    "run_loop",
    "signal_document",
    "strategy_path",
]
