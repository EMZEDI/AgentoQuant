"""Decision ledger: schema, DuckDB store, cost meter and the ``ledger query`` command.

Owned by Task 2. Fourteen stage tables, one per :class:`~agentoquant.enums.Stage` value, with the
envelope as real columns so cross-stage joins on ``cycle_id`` stay cheap.

Modules:

- :mod:`agentoquant.ledger.schema` -- the fourteen typed payload models, ``LedgerEnvelope``,
  ``MarketBrief`` (addendum section 5) and ``DecisionCard`` (addendum section 6), plus the in-house
  ULID generator. Tasks 5, 6, 17, 20 and 21 import their record types from here.
- :mod:`agentoquant.ledger.store` -- ``LedgerStore``: ``migrate``, generic ``write``, ``query``,
  ``cycle_records``, ``cost_by``, ``due_outcomes`` and ``write_outcome``.
- :mod:`agentoquant.ledger.cost_meter` -- ``CostMeter``: one ``llm_call`` row per model call, reading
  the exact ``usage.cost`` OpenRouter returns.
- :mod:`agentoquant.ledger.cli` -- the ``agentoquant ledger query`` target (also the MCP tool
  ``ledger_query``).
- :mod:`agentoquant.ledger.outcomes` -- the ``outcome`` stage's writer: ``record_due_outcomes`` writes
  the +1h / +4h / +24h row for every card whose horizon has elapsed and has no row yet, executed,
  rejected and vetoed alike, and is idempotent so it can run on every tick.

The ledger is a single-writer store: DuckDB holds the write lock per process, so the scheduler owns
the write path and readers open short-lived connections.
"""

from agentoquant.ledger.cost_meter import CostError, CostMeter
from agentoquant.ledger.outcomes import OutcomeRecordingError, record_due_outcomes
from agentoquant.ledger.schema import (
    STAGE_PAYLOAD_MODELS,
    DecisionCard,
    LedgerEnvelope,
    LedgerPayload,
    MarketBrief,
    new_ulid,
    payload_model_for,
)
from agentoquant.ledger.store import (
    HORIZON_DURATIONS,
    LedgerStore,
    LedgerWriteError,
    default_db_path,
)

__all__ = [
    "CostError",
    "CostMeter",
    "DecisionCard",
    "HORIZON_DURATIONS",
    "LedgerEnvelope",
    "LedgerPayload",
    "LedgerStore",
    "LedgerWriteError",
    "MarketBrief",
    "OutcomeRecordingError",
    "STAGE_PAYLOAD_MODELS",
    "default_db_path",
    "new_ulid",
    "payload_model_for",
    "record_due_outcomes",
]
