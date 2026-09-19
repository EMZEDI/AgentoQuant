"""Cost meter: tokens, dollars, role and model family for every LLM call.

``CostMeter.record`` writes one ``llm_call`` row per call through the store's generic write path, so a
cycle's cost is a ledger fact rather than a log line. ``CostMeter.cost_from_response`` reads the exact
``usage.cost`` OpenRouter returns and **never estimates from token counts**
(``config/settings.yaml``: ``llm.read_exact_cost_from_response: true``); a response without an exact
cost raises :class:`CostError` instead of guessing.

Secrets are never involved here: the API key is read through ``config_loader.credentials()`` by the
caller that makes the HTTP request (Task 16), never printed, logged or stored.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from agentoquant.enums import Harness, ModelFamily, Stage
from agentoquant.ledger.schema import LLMCallPayload
from agentoquant.ledger.store import LedgerStore


class CostError(Exception):
    """A response carried no exact cost, or a cost was not a usable number."""


class CostMeter:
    """Writes ``llm_call`` records and rolls their cost up per cycle."""

    def __init__(self, store: LedgerStore | None = None) -> None:
        self.store = store if store is not None else LedgerStore()

    def record(
        self,
        *,
        cycle_id: str,
        role: str,
        model_family: ModelFamily,
        harness: Harness,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        latency_ms: int,
        schema_valid: bool,
        fallback_triggered: bool,
    ) -> str:
        """Write one metered call and return its ``record_id``.

        ``cost_usd`` is the exact ``usage.cost`` from the response (see
        :meth:`cost_from_response`), not an estimate.
        """
        payload = LLMCallPayload(
            role=role,
            model_family=model_family,
            harness=harness,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            schema_valid=schema_valid,
            fallback_triggered=fallback_triggered,
        )
        return self.store.write(
            Stage.LLM_CALL,
            cycle_id,
            payload,
            producer_role=role,
            producer_model_family=model_family,
            harness=harness,
        )

    @staticmethod
    def cost_from_response(response: dict) -> float:
        """The exact cost OpenRouter reports in ``usage.cost``.

        Never estimated from token counts: if the response has no exact cost, this raises
        :class:`CostError` so the caller fails closed rather than writing a guess into the ledger.
        """
        if not isinstance(response, dict):
            raise CostError(f"response must be a mapping, got {type(response).__name__}")
        usage = response.get("usage")
        if not isinstance(usage, dict):
            raise CostError("response has no usage block; refusing to estimate a cost")
        cost = usage.get("cost")
        if cost is None:
            raise CostError(
                "usage.cost is missing from the response; refusing to estimate from token counts"
            )
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise CostError(f"usage.cost is not a number (type {type(cost).__name__})")
        value = float(cost)
        if not math.isfinite(value) or value < 0:
            raise CostError("usage.cost is not a finite, non-negative number")
        return value

    def cost_per_cycle(self, cycle_id: str) -> float:
        """Total LLM cost recorded for one cycle, in USD, from the ``llm_call`` table."""
        rows = self.store.query(
            'SELECT COALESCE(SUM("cost_usd"), 0.0) AS cost_usd FROM "llm_call" '
            'WHERE "cycle_id" = ?',
            [cycle_id],
        )
        return float(rows[0]["cost_usd"]) if rows else 0.0

    def recorded_at(self, record_id: str) -> datetime:
        """The UTC timestamp of a metered call, for callers that need to re-derive a cost window."""
        rows = self.store.query(
            'SELECT "ts" FROM "llm_call" WHERE "record_id" = ?', [record_id]
        )
        if not rows:
            raise CostError(f"unknown llm_call record: {record_id!r}")
        stamp = rows[0]["ts"]
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


__all__ = ["CostError", "CostMeter"]
