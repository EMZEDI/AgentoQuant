"""Task 6 acceptance tests: the execution bridge, the order manager and the signal store.

Offline and deterministic by construction: a real DuckDB ledger under ``tmp_path``, a fake transport
that records intents and returns canned fills, and no network call anywhere. Telegram is exercised
through an injected transport, and the freqtrade strategy is loaded from its real file.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentoquant.enums import REJECTED_ACTIONS, Action, ConfidenceBand, Sleeve, Stage
from agentoquant.execution.freqtrade_strategy import (
    assert_dry_run_config,
    check_strategy_hygiene,
    placeholder_card,
    poll_signal,
    strategy_interface_version,
    strategy_path,
)
from agentoquant.execution.order_manager import (
    EXECUTION_PATHS,
    FREQTRADE_CALLS,
    LADDER_OFFSETS_PCT,
    ORDER_TYPES,
    RULE_MARKET_ORDER_NOT_ALLOWED,
    RULE_REJECTED_ACTION,
    NullTransport,
    OrderManager,
    UnsupportedActionError,
    assert_order_type_allowed,
    coerce_action,
    is_rejected_action,
    ratchet_stop,
)
from agentoquant.execution.signal_store import (
    SignalStore,
    SignalStoreError,
    execution_intent,
    signal_document,
)
from agentoquant.execution.telegram_bot import (
    TelegramNotifier,
    render_decision_card,
    resolve_telegram_credentials,
)
from agentoquant.ledger.schema import DecisionCard, RiskGateVerdict
from agentoquant.ledger.store import LedgerStore

CYCLE = "2026-09-19T05Z-0001"
PRICE = 60_000.0
STOP = 57_000.0

# __SENTINEL__
