"""Ledger round-trip: a synthetic day replayed through all fourteen stages, the queries the plan
asks for, and the store's structural guarantees.

Task 2 acceptance criteria (``tasks/todo.md``):
  - every stage has a typed record with a shared cycle id and a producer field (role, model family,
    harness)
  - outcome fields fill automatically, including for rejected and vetoed proposals
  - the ledger can be queried by sleeve, regime, signal source, confidence band, agent, model family
    and cost

Task 2 verification steps:
  - replay a synthetic day and confirm every record is present and linked
  - query returns hit rate by confidence band, and cost per decision by model family, on the
    synthetic data
  - one cycle's full story, including what it cost, is readable end to end

Named test file required by the addendum (``tests/test_ledger_roundtrip.py``).

The synthetic day is a fixture, not a forecast: four cycles in which every one of the fourteen stages
appears, one cycle carries all fourteen, and the gate/human paths (approved, shrunk, rejected, vetoed)
each get their own card so the outcome path is exercised for all four.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agentoquant import cli as top_cli
from agentoquant import config_loader
from agentoquant.config_loader import ConfigError
from agentoquant.enums import (
    Action,
    ConfidenceBand,
    Harness,
    ModelFamily,
    Sleeve,
    SourceClass,
    Stage,
)
from agentoquant.ledger import cli as ledger_cli
from agentoquant.ledger import store as ledger_store
from agentoquant.ledger.cost_meter import CostError, CostMeter
from agentoquant.ledger.schema import (
    STAGE_PAYLOAD_MODELS,
    AdversaryObjectionPayload,
    AnalystReportPayload,
    DecisionCard,
    EarlySignalPayload,
    ExecutionPayload,
    FundingRequestPayload,
    HumanActionPayload,
    LLMCallPayload,
    MarketBrief,
    ModelOutputPayload,
    ProposalSamplePayload,
    RawSnapshotPayload,
    RiskGateVerdict,
    VerifiedEventPayload,
    is_valid_ulid,
    new_ulid,
    payload_model_for,
    ulid_timestamp,
)
from agentoquant.ledger.store import (
    ENVELOPE_COLUMNS,
    HORIZON_DURATIONS,
    TABLE_COLUMNS,
    LedgerSchemaError,
    LedgerStore,
    LedgerWriteError,
)

# ----------------------------------------------------------------------------------------------
# The synthetic day
# ----------------------------------------------------------------------------------------------

DAY_START = datetime(2026, 9, 18, 0, 0, tzinfo=UTC)
CYCLES: tuple[str, ...] = (
    "2026-09-18T00Z-0001",
    "2026-09-18T01Z-0001",
    "2026-09-18T02Z-0001",
    "2026-09-18T03Z-0001",
)

#: One decision card per cycle, with the gate and human paths the outcome records must survive.
CARD_SPECS: dict[str, dict] = {
    CYCLES[0]: {
        "coin": "BTC",
        "sleeve": Sleeve.A,
        "action": Action.ENTER_LADDERED,
        "size_pct": 8.0,
        "confidence": 78,
        "band": ConfidenceBand.STANDARD,
        "verdict": "approved",
        "rule_fired": None,
        "human": "auto_execute_timeout",
        "within_window": True,
        "execution": "filled",
        "outcomes": {"1h": True, "4h": True, "24h": False},
    },
    CYCLES[1]: {
        "coin": "TAO",
        "sleeve": Sleeve.B,
        "action": Action.ENTER_LADDERED,
        "size_pct": 12.0,
        "confidence": 92,
        "band": ConfidenceBand.MAX,
        "verdict": "rejected",
        "rule_fired": "max_position_pct",
        "human": None,
        "within_window": False,
        "execution": None,
        "outcomes": {"1h": True, "4h": False, "24h": False},
    },
    CYCLES[2]: {
        "coin": "AKT",
        "sleeve": Sleeve.B,
        "action": Action.EVENT_TRADE,
        "size_pct": 5.0,
        "confidence": 62,
        "band": ConfidenceBand.SMALL,
        "verdict": "approved",
        "rule_fired": None,
        "human": "veto",
        "within_window": True,
        "execution": None,
        "outcomes": {"1h": False, "4h": True, "24h": True},
    },
    CYCLES[3]: {
        "coin": "RENDER",
        "sleeve": Sleeve.B,
        "action": Action.ENTER_LADDERED,
        "size_pct": 6.0,
        "confidence": 84,
        "band": ConfidenceBand.STANDARD,
        "verdict": "shrunk",
        "rule_fired": "daily_turnover_cap_pct",
        "human": "auto_execute_timeout",
        "within_window": True,
        "execution": "partial",
        "outcomes": {"1h": True, "4h": True, "24h": True},
    },
}

#: (cycle_id, role, model_family, harness, tokens_in, tokens_out, cost_usd, latency_ms)
CALL_SPECS: tuple[tuple[str, str, ModelFamily, Harness, int, int, float, int], ...] = (
    (CYCLES[0], "analyst_technical", ModelFamily.DEEPSEEK, Harness.HERMES_OPENROUTER, 1200, 300, 0.0021, 820),
    (CYCLES[0], "judge", ModelFamily.CLAUDE_SONNET, Harness.CLAUDE_NATIVE, 3000, 400, 0.0410, 2400),
    (CYCLES[1], "proposer_deepseek", ModelFamily.DEEPSEEK, Harness.HERMES_OPENROUTER, 900, 250, 0.0018, 700),
    (CYCLES[1], "adversary", ModelFamily.KIMI, Harness.HERMES_OPENROUTER, 1500, 220, 0.0026, 950),
    (
        CYCLES[2], "analyst_social_hype", ModelFamily.DEEPSEEK,
        Harness.HERMES_OPENROUTER, 800, 180, 0.0015, 640,
    ),
    (CYCLES[2], "reviewer_draft", ModelFamily.GLM, Harness.HERMES_OPENROUTER, 1100, 260, 0.0009, 500),
    (CYCLES[3], "proposer_claude", ModelFamily.CLAUDE_SONNET, Harness.CLAUDE_NATIVE, 2600, 380, 0.0352, 2100),
    (
        CYCLES[3], "analyst_macro_geopolitics", ModelFamily.GEMINI_FLASH,
        Harness.HERMES_OPENROUTER, 700, 150, 0.0004, 430,
    ),
)


def _at(cycle_index: int, minutes: int = 0) -> datetime:
    return DAY_START + timedelta(hours=cycle_index, minutes=minutes)


def replay_synthetic_day(store: LedgerStore) -> dict:
    """Write a whole synthetic day and return what was written, so tests can check it.

    Every one of the fourteen stages is written; cycle 0 carries all fourteen (the end-to-end story),
    the other cycles carry the gate/human variants (rejected, vetoed, shrunk).
    """
    meter = CostMeter(store)
    written: dict[str, dict] = {"cycles": {}, "costs": {}}

    for index, cycle_id in enumerate(CYCLES):
        spec = CARD_SPECS[cycle_id]
        cycle: dict = {"card_spec": spec, "records": {}, "outcomes": {}}

        snapshot = store.write(
            Stage.RAW_SNAPSHOT,
            cycle_id,
            RawSnapshotPayload(
                source="kraken",
                coin_or_series="XBTUSD",
                fields={"last": 61000.0 + index, "spread_pct": 0.02},
                quota_remaining=118,
                is_stale=False,
            ),
            producer_role="ingest",
        )
        stale_snapshot = store.write(
            Stage.RAW_SNAPSHOT,
            cycle_id,
            RawSnapshotPayload(
                source="gdelt",
                coin_or_series="conflict_index",
                fields={"events": 3},
                quota_remaining=0,
                is_stale=True,
            ),
            producer_role="ingest",
        )

        signal = store.write(
            Stage.EARLY_SIGNAL,
            cycle_id,
            EarlySignalPayload(
                source_class=SourceClass.EXCHANGE_ANNOUNCEMENT,
                ticker=spec["coin"],
                event_type="listing",
                raw_text_or_ref=f"https://example.invalid/{cycle_id}/listing",
                detected_at=_at(index, 4),
                latency_seconds_vs_first_article=41,
            ),
            producer_role="early_signals",
        )

        event = store.write(
            Stage.VERIFIED_EVENT,
            cycle_id,
            VerifiedEventPayload(
                source_event_ids=[signal],
                evidence_quality=0.95,
                source_class=SourceClass.EXCHANGE_ANNOUNCEMENT,
                corroboration_count=2,
                is_primary=True,
                notes="exchange announcement plus a volume reaction",
            ),
            producer_role="verifier",
        )

        model_output = store.write(
            Stage.MODEL_OUTPUT,
            cycle_id,
            ModelOutputPayload(
                model_name="lightgbm_primary",
                coin=spec["coin"],
                horizon="4h",
                p_up=0.58 + index / 100,
                interval_low=0.49,
                interval_high=0.67,
                interval_method="conformal_enbpi",
                regime_label="risk_on",
                model_version="2026.09.1",
                training_window="2024-01-01..2026-08-31",
            ),
            producer_role="forecast",
            producer_model_family=ModelFamily.NONE,
        )

        analyst = store.write(
            Stage.ANALYST_REPORT,
            cycle_id,
            AnalystReportPayload(
                analyst_role="technical",
                coin=spec["coin"],
                summary=f"{spec['coin']} is above its 20h mean with rising volume",
                stance="bullish",
                key_evidence_ids=[event],
                confidence=0.66,
                schema_valid=True,
            ),
            producer_role="analyst_technical",
            producer_model_family=ModelFamily.DEEPSEEK,
            harness=Harness.HERMES_OPENROUTER,
        )

        proposal = store.write(
            Stage.PROPOSAL_SAMPLE,
            cycle_id,
            ProposalSamplePayload(
                family=ModelFamily.DEEPSEEK,
                coin=spec["coin"],
                action=spec["action"],
                size_pct=spec["size_pct"],
                entry_type="post_only_limit",
                stop=59000.0,
                target=66000.0,
                horizon="24h",
                thesis_bullets=["listing catalyst", "volume expanding", "spread tight"],
                confidence=spec["confidence"] / 100,
                invalidation_condition="a close below the 20h mean on rising sell volume",
                cited_ids=[analyst, model_output],
            ),
            producer_role="proposer_deepseek",
            producer_model_family=ModelFamily.DEEPSEEK,
            harness=Harness.HERMES_OPENROUTER,
        )

        objection = store.write(
            Stage.ADVERSARY_OBJECTION,
            cycle_id,
            AdversaryObjectionPayload(
                against_proposal_id=proposal,
                severity=3,
                objection_text="base rate for listing-day entries is negative after fees",
                own_p_up=0.47,
                recommended_change="shrink",
                tool_calls_used=["ledger_base_rates", "fee_calculator"],
            ),
            producer_role="adversary",
            producer_model_family=ModelFamily.KIMI,
            harness=Harness.HERMES_OPENROUTER,
        )

        card = store.write(
            Stage.DECISION_CARD,
            cycle_id,
            DecisionCard(
                cycle_id=cycle_id,
                selected_proposal_id=proposal,
                action=spec["action"],
                coin=spec["coin"],
                sleeve=spec["sleeve"],
                size_pct=spec["size_pct"],
                confidence=spec["confidence"],
                confidence_band=spec["band"],
                p_up=0.58 + index / 100,
                interval_low=0.49,
                interval_high=0.67,
                ev_after_fees=0.012,
                fee_tier_assumed="tier1",
                evidence_split={"primary": 1, "verified": 1, "unverified": 0},
                strongest_objection={"severity": 3, "text": "base rate is negative after fees"},
                flip_condition="a verified exchange denial or a close below the 20h mean",
            ),
            producer_role="judge",
            producer_model_family=ModelFamily.CLAUDE_SONNET,
            harness=Harness.CLAUDE_NATIVE,
        )

        verdict = store.write(
            Stage.RISK_GATE_VERDICT,
            cycle_id,
            RiskGateVerdict(
                decision_card_id=card,
                verdict=spec["verdict"],
                rule_fired=spec["rule_fired"],
                original_size_pct=spec["size_pct"],
                final_size_pct=0.0 if spec["verdict"] == "rejected" else spec["size_pct"] - 1.0,
                funding_floor_breach=index == 0,
            ),
            producer_role="risk_gate",
        )

        human = None
        if spec["human"] is not None:
            human = store.write(
                Stage.HUMAN_ACTION,
                cycle_id,
                HumanActionPayload(
                    decision_card_id=card,
                    command=spec["human"],
                    actor="shahrad",
                    responded_at=_at(index, 8),
                    within_window=spec["within_window"],
                ),
                producer_role="human",
            )

        execution = None
        if spec["execution"] is not None:
            filled = spec["execution"] == "filled"
            execution = store.write(
                Stage.EXECUTION,
                cycle_id,
                ExecutionPayload(
                    decision_card_id=card,
                    order_type="post_only_limit",
                    venue="kraken",
                    fill_price=60990.0 if filled else 61010.0,
                    fill_qty=0.0012 if filled else 0.0005,
                    fee_paid=0.19 if filled else 0.07,
                    reprice_count=1,
                    status="filled" if filled else "partial",
                ),
                producer_role="order_manager",
            )

        funding = store.write(
            Stage.FUNDING_REQUEST,
            cycle_id,
            FundingRequestPayload(
                sleeve=spec["sleeve"],
                amount=250.0,
                currency="CAD",
                reason="funding_floor_breach",
                deadline=_at(index, 48 * 60 // 60),
                status="requested",
                requested_at=_at(index, 9),
                responded_at=None,
                arrived_at=None,
            ),
            producer_role="risk_gate",
        )

        cycle["records"] = {
            "raw_snapshot": snapshot,
            "raw_snapshot_stale": stale_snapshot,
            "early_signal": signal,
            "verified_event": event,
            "model_output": model_output,
            "analyst_report": analyst,
            "proposal_sample": proposal,
            "adversary_objection": objection,
            "decision_card": card,
            "risk_gate_verdict": verdict,
            "human_action": human,
            "execution": execution,
            "funding_request": funding,
        }

        # Outcomes for every card that has a coin and an action: executed, rejected and vetoed alike.
        for horizon, realized_up in spec["outcomes"].items():
            record_id = store.write_outcome(
                card,
                horizon,
                pnl_pct=0.01 if realized_up else -0.008,
                pnl_abs=6.1 if realized_up else -4.9,
                realized_up=realized_up,
                still_open=False,
            )
            cycle["outcomes"][horizon] = record_id

        written["cycles"][cycle_id] = cycle

    for cycle_id, role, family, harness, tokens_in, tokens_out, cost, latency in CALL_SPECS:
        written.setdefault("calls", {})
        written["calls"][f"{cycle_id}:{role}"] = meter.record(
            cycle_id=cycle_id,
            role=role,
            model_family=family,
            harness=harness,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            latency_ms=latency,
            schema_valid=True,
            fallback_triggered=False,
        )

    return written


@pytest.fixture
def store(tmp_path: Path) -> LedgerStore:
    """A ledger in a temp directory: tests never touch ``data/ledger.duckdb``."""
    return LedgerStore(db_path=tmp_path / "ledger.duckdb")


@pytest.fixture
def replayed(store: LedgerStore) -> dict:
    return replay_synthetic_day(store)


def _count(store: LedgerStore, stage: Stage, where: str = "", params: list | None = None) -> int:
    sql = f'SELECT COUNT(*) AS n FROM "{stage.value}"'
    if where:
        sql += f" WHERE {where}"
    return int(store.query(sql, params or [])[0]["n"])


# ----------------------------------------------------------------------------------------------
# Structure: fourteen tables, real envelope columns, idempotent migration
# ----------------------------------------------------------------------------------------------


def test_fourteen_tables_exist(store: LedgerStore) -> None:
    tables = store.tables()
    assert len(tables) == 14, tables
    assert set(tables) == {stage.value for stage in Stage}


@pytest.mark.parametrize("stage", list(Stage))
def test_envelope_columns_are_real_columns(store: LedgerStore, stage: Stage) -> None:
    columns = dict(store.table_columns(stage))
    for name in ENVELOPE_COLUMNS:
        assert name in columns, f"{stage.value} is missing envelope column {name}"
    assert columns["record_id"] == "VARCHAR"
    assert columns["cycle_id"] == "VARCHAR"
    assert columns["stage"] == "VARCHAR"
    assert columns["ts"].startswith("TIMESTAMP")
    assert "payload" not in columns and "data" not in columns and "body" not in columns
    # The payload's own columns are real columns too.
    for name in payload_model_for(stage).model_fields:
        assert name in columns, f"{stage.value} is missing payload column {name}"


def test_envelope_columns_are_queryable_and_joinable(store: LedgerStore, replayed: dict) -> None:
    """The point of real columns: cross-stage joins on cycle_id stay cheap."""
    rows = store.query(
        'SELECT c."cycle_id" AS cycle_id, c."confidence_band" AS band, v."verdict" AS verdict, '
        'h."command" AS human_command, e."status" AS status '
        'FROM "decision_card" c '
        'LEFT JOIN "risk_gate_verdict" v ON v."decision_card_id" = c."record_id" '
        'LEFT JOIN "human_action" h ON h."decision_card_id" = c."record_id" '
        'LEFT JOIN "execution" e ON e."decision_card_id" = c."record_id" '
        'ORDER BY c."cycle_id"'
    )
    assert [row["cycle_id"] for row in rows] == list(CYCLES)
    assert [row["verdict"] for row in rows] == ["approved", "rejected", "approved", "shrunk"]
    assert rows[2]["human_command"] == "veto"


def test_migrate_is_idempotent(store: LedgerStore, replayed: dict) -> None:
    before = store.tables()
    counts_before = {stage.value: _count(store, stage) for stage in Stage}
    widths_before = {stage.value: len(store.table_columns(stage)) for stage in Stage}

    for _ in range(3):
        store.migrate()

    assert store.tables() == before
    assert {stage.value: _count(store, stage) for stage in Stage} == counts_before
    assert {stage.value: len(store.table_columns(stage)) for stage in Stage} == widths_before


def test_default_db_path_comes_from_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert config_loader.load_settings().ledger_path == "data/ledger.duckdb"

    target = tmp_path / "configured" / "ledger.duckdb"
    settings = config_loader.load_settings().model_copy(update={"ledger_path": str(target)})
    monkeypatch.delenv(ledger_store.DB_PATH_ENV, raising=False)
    monkeypatch.setattr(ledger_store, "load_settings", lambda: settings)

    store = LedgerStore()
    assert store.db_path == target
    assert target.exists()
    assert len(store.tables()) == 14


def test_env_override_wins_over_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "override" / "ledger.duckdb"
    monkeypatch.setenv(ledger_store.DB_PATH_ENV, str(target))
    assert ledger_store.default_db_path() == target
    assert LedgerStore().db_path == target


# ----------------------------------------------------------------------------------------------
# ULIDs
# ----------------------------------------------------------------------------------------------


def test_ulids_are_unique_monotonic_and_time_ordered() -> None:
    ids = [new_ulid() for _ in range(2000)]
    assert len(set(ids)) == 2000
    assert all(is_valid_ulid(value) for value in ids)
    assert ids == sorted(ids)
    assert all(left < right for left, right in zip(ids, ids[1:], strict=False))
    stamp = ulid_timestamp(ids[-1])
    assert stamp.tzinfo is not None
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 10


def test_ulid_rejects_a_bad_value() -> None:
    assert not is_valid_ulid("not-a-ulid")
    assert not is_valid_ulid("I" * 26)  # Crockford base32 has no I, L, O or U
    with pytest.raises(ValueError):
        ulid_timestamp("nope")


def test_record_ids_are_unique_ulids_across_stages(store: LedgerStore, replayed: dict) -> None:
    rows = store.query(
        " UNION ALL ".join(
            f'SELECT "record_id" AS record_id, \'{stage.value}\' AS stage FROM "{stage.value}"'
            for stage in Stage
        )
    )
    ids = [row["record_id"] for row in rows]
    assert len(ids) == len(set(ids))
    assert all(is_valid_ulid(value) for value in ids)


# ----------------------------------------------------------------------------------------------
# The synthetic day: every record present, every record linked
# ----------------------------------------------------------------------------------------------


def test_every_stage_has_records_in_the_synthetic_day(store: LedgerStore, replayed: dict) -> None:
    counts = {stage.value: _count(store, stage) for stage in Stage}
    missing = [name for name, count in counts.items() if count == 0]
    assert missing == [], f"stages with no records: {missing}"
    assert counts["raw_snapshot"] == 8
    assert counts["decision_card"] == 4
    assert counts["outcome"] == 12
    assert counts["llm_call"] == len(CALL_SPECS)


def test_every_record_is_linked_to_a_cycle(store: LedgerStore, replayed: dict) -> None:
    """Zero orphans: every record in the day carries one of the day's cycle ids."""
    for stage in Stage:
        cycle_ids = {
            row["cycle_id"]
            for row in store.query(f'SELECT DISTINCT "cycle_id" AS cycle_id FROM "{stage.value}"')
        }
        assert cycle_ids, stage.value
        assert cycle_ids <= set(CYCLES), f"{stage.value} has orphan cycle ids: {cycle_ids - set(CYCLES)}"


def test_cycle_zero_carries_all_fourteen_stages(store: LedgerStore, replayed: dict) -> None:
    stages = {
        row["stage"]
        for row in store.query(
            " UNION ALL ".join(
                f'SELECT \'{stage.value}\' AS stage, "cycle_id" AS cycle_id FROM "{stage.value}"'
                for stage in Stage
            )
            + " WHERE cycle_id = ?",
            [CYCLES[0]],
        )
    }
    assert stages == {stage.value for stage in Stage}


def test_orphan_query_reports_the_incomplete_cycles(store: LedgerStore, replayed: dict) -> None:
    """The named query that answers "is every record linked" is itself tested."""
    rows = store.query(ledger_cli.NAMED_QUERIES["orphans"].sql)
    # Cycles 1 and 2 are the deliberately incomplete ones: 1 is rejected by the gate before
    # execution, 2 is vetoed before execution. Cycle 3 is shrunk but still executed, so it
    # carries all fourteen stages and must NOT be reported as an orphan.
    assert [row["cycle_id"] for row in rows] == [CYCLES[1], CYCLES[2]]
    assert all(row["stages_present"] < 14 for row in rows)
    assert all(row["stages_missing"] == 14 - row["stages_present"] for row in rows)


def test_every_reference_resolves_inside_its_cycle(store: LedgerStore, replayed: dict) -> None:
    for cycle_id, cycle in replayed["cycles"].items():
        records = cycle["records"]
        card = records["decision_card"]
        verdict = store.query(
            'SELECT "decision_card_id" FROM "risk_gate_verdict" WHERE "cycle_id" = ?', [cycle_id]
        )[0]
        assert verdict["decision_card_id"] == card

        execution = store.query(
            'SELECT "decision_card_id" FROM "execution" WHERE "cycle_id" = ?', [cycle_id]
        )
        assert [row["decision_card_id"] for row in execution] == (
            [card] if records["execution"] else []
        )

        human = store.query(
            'SELECT "decision_card_id", "command" FROM "human_action" WHERE "cycle_id" = ?',
            [cycle_id],
        )
        assert [row["decision_card_id"] for row in human] == (
            [card] if records["human_action"] else []
        )

        objection = store.query(
            'SELECT "against_proposal_id" FROM "adversary_objection" WHERE "cycle_id" = ?',
            [cycle_id],
        )[0]
        assert objection["against_proposal_id"] == records["proposal_sample"]

        event = store.query(
            'SELECT "source_event_ids" FROM "verified_event" WHERE "cycle_id" = ?', [cycle_id]
        )[0]
        assert event["source_event_ids"] == [records["early_signal"]]

        outcomes = store.query(
            'SELECT "decision_card_id", "horizon", "realized_up" FROM "outcome" '
            'WHERE "cycle_id" = ? ORDER BY horizon',
            [cycle_id],
        )
        assert [row["decision_card_id"] for row in outcomes] == [card] * 3
        assert all(row["realized_up"] is not None for row in outcomes)


def test_every_record_carries_a_producer(store: LedgerStore, replayed: dict) -> None:
    """Acceptance criterion: a shared cycle id and a producer field (role, family, harness)."""
    for stage in Stage:
        rows = store.query(
            f'SELECT "producer_role" AS role, "producer_model_family" AS family, '
            f'"harness" AS harness, "schema_version" AS schema_version FROM "{stage.value}"'
        )
        assert rows
        for row in rows:
            assert row["role"], stage.value
            assert row["family"] in {member.value for member in ModelFamily}
            assert row["harness"] in {member.value for member in Harness}
            assert row["schema_version"] == 1


def test_llm_call_records_carry_role_family_and_harness(store: LedgerStore, replayed: dict) -> None:
    rows = store.query(
        'SELECT "role" AS role, "model_family" AS model_family, "harness" AS harness, '
        '"producer_role" AS producer_role, "producer_model_family" AS producer_family, '
        '"cost_usd" AS cost_usd, "tokens_in" AS tokens_in, "tokens_out" AS tokens_out, '
        '"latency_ms" AS latency_ms, "schema_valid" AS schema_valid, '
        '"fallback_triggered" AS fallback_triggered FROM "llm_call" ORDER BY role'
    )
    assert len(rows) == len(CALL_SPECS)
    for row in rows:
        assert row["producer_role"] == row["role"]
        assert row["producer_family"] == row["model_family"]
        assert row["harness"] in {member.value for member in Harness}
        assert row["cost_usd"] > 0
        assert row["tokens_in"] > 0 and row["tokens_out"] > 0
        assert row["latency_ms"] > 0
        assert row["schema_valid"] is True
        assert row["fallback_triggered"] is False


# ----------------------------------------------------------------------------------------------
# Outcomes: fill automatically, including for rejected and vetoed proposals
# ----------------------------------------------------------------------------------------------


def test_outcomes_recorded_for_executed_rejected_vetoed_and_shrunk_cards(
    store: LedgerStore, replayed: dict
) -> None:
    for cycle_id, cycle in replayed["cycles"].items():
        card = cycle["records"]["decision_card"]
        verdict = cycle["card_spec"]["verdict"]
        rows = store.query(
            'SELECT "horizon" AS horizon, "pnl_pct" AS pnl_pct, "pnl_abs" AS pnl_abs, '
            '"realized_up" AS realized_up, "still_open" AS still_open FROM "outcome" '
            'WHERE "decision_card_id" = ? ORDER BY horizon',
            [card],
        )
        assert {row["horizon"] for row in rows} == {"1h", "24h", "4h"}, (cycle_id, verdict)
        for row in rows:
            assert row["still_open"] is False
            assert row["pnl_pct"] is not None and row["pnl_abs"] is not None
            assert row["realized_up"] is (cycle["card_spec"]["outcomes"][row["horizon"]])


def test_rejected_and_vetoed_cards_get_counterfactual_outcomes(store: LedgerStore, replayed: dict) -> None:
    rejected = replayed["cycles"][CYCLES[1]]["records"]["decision_card"]
    vetoed = replayed["cycles"][CYCLES[2]]["records"]["decision_card"]
    rows = store.query(
        'SELECT o."decision_card_id" AS card, COUNT(*) AS horizons, '
        'SUM(CASE WHEN o."realized_up" THEN 1 ELSE 0 END) AS hits '
        'FROM "outcome" o WHERE o."decision_card_id" IN (?, ?) GROUP BY 1 ORDER BY 1',
        [rejected, vetoed],
    )
    assert {row["card"] for row in rows} == {rejected, vetoed}
    assert all(row["horizons"] == 3 for row in rows)
    # The vetoed card was never executed, so its outcome is counterfactual by construction.
    assert _count(store, Stage.EXECUTION, '"decision_card_id" = ?', [vetoed]) == 0
    assert _count(store, Stage.HUMAN_ACTION, '"command" = ?', ["veto"]) == 1


def test_due_outcomes_lists_a_card_once_its_horizon_elapses(store: LedgerStore) -> None:
    cycle_id = "2026-09-19T00Z-0001"
    card = store.write(
        Stage.DECISION_CARD,
        cycle_id,
        DecisionCard(
            cycle_id=cycle_id,
            selected_proposal_id=None,
            action=Action.ENTER_LADDERED,
            coin="BTC",
            sleeve=Sleeve.A,
            size_pct=5.0,
            confidence=70,
            confidence_band=ConfidenceBand.STANDARD,
            p_up=0.6,
            interval_low=0.5,
            interval_high=0.7,
            ev_after_fees=0.01,
            fee_tier_assumed="tier1",
            evidence_split={"primary": 1, "verified": 0, "unverified": 0},
            strongest_objection=None,
            flip_condition="a close below the 20h mean",
        ),
        producer_role="judge",
        producer_model_family=ModelFamily.CLAUDE_SONNET,
        harness=Harness.CLAUDE_NATIVE,
    )
    now = datetime.now(UTC)
    assert store.due_outcomes("1h", as_of=now + timedelta(minutes=30)) == []

    due = store.due_outcomes("1h", as_of=now + timedelta(minutes=90))
    assert [row["decision_card_id"] for row in due] == [card]
    assert due[0]["cycle_id"] == cycle_id
    assert due[0]["coin"] == "BTC"
    assert due[0]["horizon"] == "1h"
    assert due[0]["due_at"] > due[0]["ts"]

    store.write_outcome(card, "1h", pnl_pct=0.004, pnl_abs=2.4, realized_up=True, still_open=False)
    assert store.due_outcomes("1h", as_of=now + timedelta(hours=3)) == []
    due_4h = [
        row["decision_card_id"] for row in store.due_outcomes("4h", as_of=now + timedelta(hours=5))
    ]
    assert due_4h == [card]
    assert store.due_outcomes("exit", as_of=now + timedelta(days=3)) == []
    with pytest.raises(ValueError):
        store.due_outcomes("48h")


def test_outcome_writes_are_linked_and_duplicates_are_refused(store: LedgerStore, replayed: dict) -> None:
    card = replayed["cycles"][CYCLES[0]]["records"]["decision_card"]
    with pytest.raises(LedgerWriteError):
        store.write_outcome(card, "1h", realized_up=True)

    record_id = store.write_outcome(card, "1h", pnl_pct=0.02, realized_up=False, replace=True)
    rows = store.query(
        'SELECT "record_id" AS record_id, "pnl_pct" AS pnl_pct, "realized_up" AS realized_up, '
        '"cycle_id" AS cycle_id FROM "outcome" WHERE "decision_card_id" = ? AND "horizon" = ?',
        [card, "1h"],
    )
    assert len(rows) == 1
    assert rows[0]["record_id"] == record_id
    assert rows[0]["pnl_pct"] == 0.02
    assert rows[0]["realized_up"] is False
    assert rows[0]["cycle_id"] == CYCLES[0]

    with pytest.raises(LedgerWriteError):
        store.write_outcome("01HZZZZZZZZZZZZZZZZZZZZZZZ", "1h")
    with pytest.raises(ValueError):
        store.write_outcome(card, "48h")


# ----------------------------------------------------------------------------------------------
# Queries the plan asks for: hit rate by band, cost per decision by family
# ----------------------------------------------------------------------------------------------


def test_hit_rate_by_confidence_band(store: LedgerStore, replayed: dict) -> None:
    rows = store.query(ledger_cli.NAMED_QUERIES["hit_rate_by_band"].sql)
    by_band_horizon = {(row["confidence_band"], row["horizon"]): row for row in rows}

    expected: dict[tuple[str, str], list[bool]] = {}
    for cycle in replayed["cycles"].values():
        band = cycle["card_spec"]["band"].value
        for horizon, realized_up in cycle["card_spec"]["outcomes"].items():
            expected.setdefault((band, horizon), []).append(realized_up)

    assert set(by_band_horizon) == set(expected)
    for key, outcomes in expected.items():
        row = by_band_horizon[key]
        assert row["n"] == len(outcomes), key
        assert row["hits"] == sum(1 for value in outcomes if value), key
        assert row["hit_rate"] == pytest.approx(sum(outcomes) / len(outcomes), abs=1e-4)

    standard_24h = by_band_horizon[("standard", "24h")]
    assert standard_24h["n"] == 2 and standard_24h["hits"] == 1
    assert by_band_horizon[("max", "1h")]["hit_rate"] == pytest.approx(1.0)


def test_cost_per_decision_by_model_family(store: LedgerStore, replayed: dict) -> None:
    expected: dict[str, dict[str, float]] = {}
    for cycle_id, _role, family, _harness, tokens_in, _tokens_out, cost, _latency in CALL_SPECS:
        bucket = expected.setdefault(
            family.value, {"calls": 0, "cost_usd": 0.0, "cycles": set(), "tokens_in": 0}
        )
        bucket["calls"] += 1
        bucket["cost_usd"] += cost
        bucket["cycles"].add(cycle_id)
        bucket["tokens_in"] += tokens_in

    rows = store.query(ledger_cli.NAMED_QUERIES["cost_per_family"].sql)
    assert {row["model_family"] for row in rows} == set(expected)
    for row in rows:
        bucket = expected[row["model_family"]]
        assert row["calls"] == bucket["calls"]
        assert row["decisions"] == len(bucket["cycles"])
        assert row["cost_usd"] == pytest.approx(round(bucket["cost_usd"], 6), abs=1e-9)
        assert row["cost_per_decision_usd"] == pytest.approx(
            round(bucket["cost_usd"] / len(bucket["cycles"]), 6), abs=1e-6
        )

    deepseek = next(row for row in rows if row["model_family"] == "deepseek")
    assert deepseek["cost_usd"] == pytest.approx(0.0054)
    assert deepseek["cost_per_decision_usd"] == pytest.approx(0.0018)
    claude = next(row for row in rows if row["model_family"] == "claude_sonnet")
    assert claude["cost_usd"] == pytest.approx(0.0762)
    assert claude["decisions"] == 2


def test_cost_by_every_dimension(store: LedgerStore, replayed: dict) -> None:
    by_family = {row["model_family"]: row for row in store.cost_by("family")}
    assert by_family["deepseek"]["calls"] == 3
    assert by_family["deepseek"]["cost_usd"] == pytest.approx(0.0054)
    assert by_family["gemini_flash"]["cost_usd"] == pytest.approx(0.0004)

    by_cycle = {row["cycle_id"]: row for row in store.cost_by("cycle")}
    assert by_cycle[CYCLES[0]]["cost_usd"] == pytest.approx(0.0431)
    assert by_cycle[CYCLES[3]]["cost_usd"] == pytest.approx(0.0356)

    by_role = {row["role"]: row for row in store.cost_by("role")}
    assert by_role["judge"]["cost_usd"] == pytest.approx(0.0410)

    by_day = store.cost_by("day")
    assert len(by_day) == 1
    assert by_day[0]["calls"] == len(CALL_SPECS)

    with pytest.raises(ValueError):
        store.cost_by("coin")  # type: ignore[arg-type]


def test_query_by_sleeve_regime_signal_source_and_agent(store: LedgerStore, replayed: dict) -> None:
    """The acceptance criterion's query axes, beyond band and cost."""
    by_sleeve = store.query(
        'SELECT "sleeve" AS sleeve, COUNT(*) AS n FROM "decision_card" GROUP BY 1 ORDER BY 1'
    )
    assert {row["sleeve"]: row["n"] for row in by_sleeve} == {"A": 1, "B": 3}

    by_regime = store.query(
        'SELECT "regime_label" AS regime_label, COUNT(*) AS n FROM "model_output" GROUP BY 1'
    )
    assert by_regime == [{"regime_label": "risk_on", "n": 4}]

    by_source = store.query(
        'SELECT "source_class" AS source_class, COUNT(*) AS n FROM "early_signal" GROUP BY 1'
    )
    assert by_source == [{"source_class": "exchange_announcement", "n": 4}]

    by_agent = store.query(
        'SELECT "producer_role" AS producer_role, COUNT(*) AS n FROM "analyst_report" '
        'GROUP BY 1 ORDER BY 1'
    )
    assert by_agent == [{"producer_role": "analyst_technical", "n": 4}]

    by_family = store.query(
        'SELECT "producer_model_family" AS family, COUNT(*) AS n FROM "proposal_sample" GROUP BY 1'
    )
    assert by_family == [{"family": "deepseek", "n": 4}]


# ----------------------------------------------------------------------------------------------
# One cycle's full story, end to end, including what it cost
# ----------------------------------------------------------------------------------------------


def test_cycle_records_read_one_cycle_end_to_end(store: LedgerStore, replayed: dict) -> None:
    records = store.cycle_records(CYCLES[0])
    assert {record["stage"] for record in records} == {stage.value for stage in Stage}
    assert records == sorted(records, key=lambda record: (record["ts"], record["record_id"]))
    for record in records:
        assert record["cycle_id"] == CYCLES[0]
        assert is_valid_ulid(record["record_id"])
        assert record["ts"].tzinfo is not None
        assert record["ts"].utcoffset() == timedelta(0)

    cost_rows = store.cycle_cost(CYCLES[0])
    assert [row["role"] for row in cost_rows] == ["judge", "analyst_technical"]
    assert sum(row["cost_usd"] for row in cost_rows) == pytest.approx(0.0431)

    story = ledger_cli.render_table(
        ["stage", "record_id", "producer_role", "producer_model_family", "harness"],
        [
            {
                "stage": record["stage"],
                "record_id": record["record_id"],
                "producer_role": record["producer_role"],
                "producer_model_family": record["producer_model_family"],
                "harness": record["harness"],
            }
            for record in records
        ],
    )
    assert story.count("\n") >= len(records)
    print(f"\nfull story for {CYCLES[0]} ({len(records)} records, "
          f"${sum(row['cost_usd'] for row in cost_rows):.4f}):\n{story}")


def test_cycle_records_returns_nothing_for_an_unknown_cycle(store: LedgerStore, replayed: dict) -> None:
    assert store.cycle_records("2026-09-18T23Z-9999") == []


# ----------------------------------------------------------------------------------------------
# Schema discipline
# ----------------------------------------------------------------------------------------------


def test_payload_registry_covers_all_fourteen_stages() -> None:
    assert set(STAGE_PAYLOAD_MODELS) == set(Stage)
    for stage in Stage:
        assert payload_model_for(stage) is STAGE_PAYLOAD_MODELS[stage]
    assert payload_model_for(Stage.DECISION_CARD) is DecisionCard


def test_market_brief_and_decision_card_are_ledger_shapes() -> None:
    brief = MarketBrief(
        cycle_id=CYCLES[0],
        generated_at=DAY_START,
        token_budget_max=12000,
        portfolio={"positions": [], "free_cash_by_sleeve": {"A": 100.0, "B": 50.0, "C": 0.0}},
        risk_budget={"positions_open": 0, "max_positions": 5},
        fee_tier={"tier_name": "tier1", "maker_pct": 0.4, "taker_pct": 0.8},
        model_outputs=[],
        early_signals=[],
        verified_events=[],
        regime={"label": "risk_on", "rule_fired": None},
        worldview_excerpt={"version": "1.0", "relevant_priors": []},
    )
    assert brief.token_budget_max == 12000

    hold = DecisionCard(
        cycle_id=CYCLES[0],
        selected_proposal_id=None,
        action=Action.HOLD,
        coin=None,
        sleeve=None,
        size_pct=None,
        confidence=40,
        confidence_band=ConfidenceBand.NO_TRADE,
        p_up=0.5,
        interval_low=0.45,
        interval_high=0.55,
        ev_after_fees=-0.001,
        fee_tier_assumed="tier1",
        evidence_split={"primary": 0, "verified": 0, "unverified": 0},
        strongest_objection=None,
        flip_condition="a verified primary-source event",
    )
    assert hold.action is Action.HOLD
    with pytest.raises(ValidationError):
        DecisionCard(**{**hold.model_dump(), "confidence": 140})
    with pytest.raises(ValidationError):
        DecisionCard(**{**hold.model_dump(), "unknown_field": 1})


def test_write_refuses_a_payload_from_another_stage(store: LedgerStore) -> None:
    with pytest.raises(LedgerSchemaError):
        store.write(
            Stage.EXECUTION,
            CYCLES[0],
            RawSnapshotPayload(
                source="kraken",
                coin_or_series="XBTUSD",
                fields={},
                quota_remaining=1,
                is_stale=False,
            ),
            producer_role="order_manager",
        )
    with pytest.raises(LedgerSchemaError):
        store.write(Stage.EXECUTION, CYCLES[0], "not a model", producer_role="order_manager")  # type: ignore[arg-type]
    with pytest.raises(LedgerSchemaError):
        store.write(Stage.RAW_SNAPSHOT, CYCLES[0], {"source": "kraken"}, producer_role="ingest")


def test_write_requires_a_cycle_id(store: LedgerStore) -> None:
    payload = RawSnapshotPayload(
        source="kraken", coin_or_series="XBTUSD", fields={}, quota_remaining=1, is_stale=False
    )
    with pytest.raises(LedgerWriteError):
        store.write(Stage.RAW_SNAPSHOT, "   ", payload, producer_role="ingest")


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        EarlySignalPayload(
            source_class=SourceClass.HEADLINE,
            ticker=None,
            event_type="headline",
            raw_text_or_ref="ref",
            detected_at=datetime(2026, 9, 18, 12, 0),
            latency_seconds_vs_first_article=None,
        )


def test_llm_call_envelope_defaults_from_the_payload(store: LedgerStore) -> None:
    record_id = store.write(
        Stage.LLM_CALL,
        CYCLES[0],
        LLMCallPayload(
            role="analyst_news_policy",
            model_family=ModelFamily.GLM,
            harness=Harness.HERMES_OPENROUTER,
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.0002,
            latency_ms=100,
            schema_valid=True,
            fallback_triggered=False,
        ),
        producer_role="analyst_news_policy",
    )
    row = store.query(
        'SELECT "producer_model_family" AS family, "harness" AS harness, '
        '"model_family" AS payload_family FROM "llm_call" WHERE "record_id" = ?',
        [record_id],
    )[0]
    assert row["family"] == "glm"
    assert row["harness"] == "hermes_openrouter"
    assert row["payload_family"] == "glm"
    assert "harness" in dict(store.table_columns(Stage.LLM_CALL))


def test_conflicting_envelope_and_payload_harness_is_refused(store: LedgerStore) -> None:
    with pytest.raises(LedgerWriteError):
        store.write(
            Stage.LLM_CALL,
            CYCLES[0],
            LLMCallPayload(
                role="judge",
                model_family=ModelFamily.CLAUDE_SONNET,
                harness=Harness.HERMES_OPENROUTER,
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.01,
                latency_ms=1,
                schema_valid=True,
                fallback_triggered=False,
            ),
            producer_role="judge",
            producer_model_family=ModelFamily.CLAUDE_SONNET,
            harness=Harness.CLAUDE_NATIVE,
        )


def test_json_columns_round_trip_as_structures(store: LedgerStore, replayed: dict) -> None:
    row = store.query(
        'SELECT "fields" AS fields, "source" AS source FROM "raw_snapshot" WHERE "source" = ?',
        ["kraken"],
    )[0]
    assert isinstance(row["fields"], dict)
    assert row["fields"]["spread_pct"] == 0.02

    row = store.query('SELECT "thesis_bullets" AS bullets FROM "proposal_sample" LIMIT 1')[0]
    assert row["bullets"] == ["listing catalyst", "volume expanding", "spread tight"]

    row = store.query('SELECT "evidence_split" AS split FROM "decision_card" LIMIT 1')[0]
    assert row["split"] == {"primary": 1, "verified": 1, "unverified": 0}


def test_every_table_has_the_columns_its_payload_model_declares() -> None:
    for stage in Stage:
        names = [name for name, _ in TABLE_COLUMNS[stage]]
        assert names[: len(ENVELOPE_COLUMNS)] == list(ENVELOPE_COLUMNS)
        for field_name in payload_model_for(stage).model_fields:
            assert field_name in names, (stage.value, field_name)
        assert len(names) == len(set(names)), stage.value


def test_horizon_durations_cover_the_plan() -> None:
    assert set(HORIZON_DURATIONS) == {"1h", "4h", "24h", "exit"}
    assert HORIZON_DURATIONS["24h"] == timedelta(hours=24)


# ----------------------------------------------------------------------------------------------
# Cost meter
# ----------------------------------------------------------------------------------------------


def test_cost_from_response_reads_the_exact_usage_cost() -> None:
    response = {
        "id": "gen-123",
        "model": "deepseek/deepseek-v4.1-flash",
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 8,
            "total_tokens": 128,
            "cost": 0.001234,
        },
    }
    assert CostMeter.cost_from_response(response) == 0.001234


def test_cost_from_response_never_estimates_from_tokens() -> None:
    with pytest.raises(CostError):
        CostMeter.cost_from_response({"usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    with pytest.raises(CostError):
        CostMeter.cost_from_response({"usage": {"cost": None, "prompt_tokens": 100}})
    with pytest.raises(CostError):
        CostMeter.cost_from_response({"choices": []})
    with pytest.raises(CostError):
        CostMeter.cost_from_response({"usage": {"cost": True}})
    with pytest.raises(CostError):
        CostMeter.cost_from_response("not a mapping")  # type: ignore[arg-type]


def test_cost_meter_records_and_sums_a_cycle(store: LedgerStore) -> None:
    meter = CostMeter(store)
    assert meter.cost_per_cycle(CYCLES[0]) == 0.0
    record_id = meter.record(
        cycle_id=CYCLES[0],
        role="judge",
        model_family=ModelFamily.CLAUDE_SONNET,
        harness=Harness.CLAUDE_NATIVE,
        tokens_in=2000,
        tokens_out=300,
        cost_usd=CostMeter.cost_from_response({"usage": {"cost": 0.0301}}),
        latency_ms=1900,
        schema_valid=True,
        fallback_triggered=False,
    )
    assert is_valid_ulid(record_id)
    assert meter.cost_per_cycle(CYCLES[0]) == pytest.approx(0.0301)
    assert meter.recorded_at(record_id).tzinfo is not None
    with pytest.raises(CostError):
        meter.recorded_at("01HZZZZZZZZZZZZZZZZZZZZZZZ")


# ----------------------------------------------------------------------------------------------
# The CLI and the MCP tool
# ----------------------------------------------------------------------------------------------


def _cli_env(monkeypatch: pytest.MonkeyPatch, store: LedgerStore) -> None:
    monkeypatch.setenv(ledger_store.DB_PATH_ENV, str(store.db_path))


def test_cli_named_query_prints_a_real_table(
    store: LedgerStore, replayed: dict, call_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, store)
    result = CliRunner().invoke(top_cli.app, ["ledger", "query", "--query", "hit_rate_by_band"])
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0]
    for column in ("confidence_band", "horizon", "n", "hits", "hit_rate"):
        assert column in header, f"{column!r} missing from the rendered header: {header!r}"
    assert "standard" in result.output
    assert "9 row(s) from named:hit_rate_by_band" in result.output
    assert call_log.exists()


def test_cli_sql_query_prints_a_real_table(
    store: LedgerStore, replayed: dict, call_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, store)
    result = CliRunner().invoke(
        top_cli.app,
        [
            "ledger",
            "query",
            "--sql",
            'SELECT "cycle_id" AS cycle_id, COUNT(*) AS records FROM "decision_card" '
            "GROUP BY 1 ORDER BY 1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "cycle_id" in result.output
    assert CYCLES[0] in result.output


def test_cli_cost_per_family_table(
    store: LedgerStore, replayed: dict, call_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, store)
    result = CliRunner().invoke(top_cli.app, ["ledger", "query", "--query", "cost_per_family"])
    assert result.exit_code == 0, result.output
    assert "claude_sonnet" in result.output
    assert "0.0762" in result.output


def test_cli_named_query_with_a_bound_parameter(
    store: LedgerStore, replayed: dict, call_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, store)
    result = CliRunner().invoke(
        top_cli.app,
        ["ledger", "query", "--query", "cycle_story", "--param", f"cycle_id={CYCLES[0]}"],
    )
    assert result.exit_code == 0, result.output
    assert "raw_snapshot" in result.output and "funding_request" in result.output


def test_ledger_query_is_the_shared_cli_and_mcp_path(
    store: LedgerStore, replayed: dict, call_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cli.run_command`` is exactly what the MCP tool ``ledger_query`` calls."""
    _cli_env(monkeypatch, store)
    output = top_cli.run_command(
        "ledger query",
        top_cli.LedgerQueryInput(sql='SELECT COUNT(*) AS n FROM "decision_card"', json_output=True),
        client="mcp",
        source="mcp",
    )
    assert output.status == "ok"
    assert output.payload["rows"] == [{"n": 4}]
    assert output.payload["columns"] == ["n"]
    assert json.loads(call_log.read_text(encoding="utf-8").strip())["client"] == "mcp"


def test_ledger_cli_json_output_carries_rows(store: LedgerStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _cli_env(monkeypatch, store)
    replay_synthetic_day(store)
    result = ledger_cli.main(named_query="cost_per_family", json_output=True)
    assert result["status"] == "ok"
    assert result["row_count"] == len(result["rows"]) > 0
    assert "model_family" in result["columns"]


def test_ledger_cli_usage_errors(store: LedgerStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _cli_env(monkeypatch, store)
    with pytest.raises(ConfigError):
        ledger_cli.main()
    with pytest.raises(ConfigError):
        ledger_cli.main(sql="SELECT 1", named_query="hit_rate_by_band")
    with pytest.raises(ConfigError):
        ledger_cli.main(named_query="no_such_query")
    with pytest.raises(ConfigError):
        ledger_cli.main(named_query="cycle_story")


def test_ledger_cli_refuses_to_write(
    store: LedgerStore, call_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, store)
    with pytest.raises(ConfigError):
        ledger_cli.main(sql='DELETE FROM "decision_card"')
    with pytest.raises(ConfigError):
        ledger_cli.main(sql='SELECT 1; DROP TABLE "decision_card"')
    result = CliRunner().invoke(
        top_cli.app, ["ledger", "query", "--sql", 'DELETE FROM "decision_card"']
    )
    assert result.exit_code == 2, result.output


def test_ledger_cli_reports_a_failed_query_as_an_error(
    store: LedgerStore, call_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(monkeypatch, store)
    result = ledger_cli.main(sql="SELECT * FROM no_such_table")
    assert result["status"] == "error"
    assert "query failed" in result["message"]

    cli_result = CliRunner().invoke(
        top_cli.app, ["ledger", "query", "--sql", "SELECT * FROM no_such_table"]
    )
    assert cli_result.exit_code == 1


def test_standalone_module_supports_named_queries(
    store: LedgerStore, replayed: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _cli_env(monkeypatch, store)
    assert ledger_cli._standalone(["--named", "hit_rate_by_band"]) == 0
    printed = capsys.readouterr().out
    assert "hit_rate" in printed

    assert ledger_cli._standalone(["--named", "nope"]) == 2
    assert "unknown named query" in capsys.readouterr().err

    assert ledger_cli._standalone(["--list-named"]) == 0
    assert "cost_per_family" in capsys.readouterr().out


def test_every_named_query_runs(store: LedgerStore, replayed: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _cli_env(monkeypatch, store)
    params = {"cycle_id": CYCLES[0]}
    for name in ledger_cli.NAMED_QUERIES:
        result = ledger_cli.main(named_query=name, params=params, json_output=True)
        assert result["status"] == "ok", name
        assert result["row_count"] == len(result["rows"]), name
        assert result["columns"] == list(ledger_cli.NAMED_QUERIES[name].columns), name
        assert result["rows"], name


def test_render_table_handles_empty_and_missing_values() -> None:
    assert ledger_cli.render_table(["a"], []) == "(0 rows)"
    table = ledger_cli.render_table(["a", "b"], [{"a": 1, "b": None}, {"a": 2, "b": True}])
    assert "a | b" in table
    assert "1 |" in table
    assert "2 | true" in table


# ----------------------------------------------------------------------------------------------
# Secret hygiene
# ----------------------------------------------------------------------------------------------


def test_no_credential_value_reaches_the_ledger_or_the_call_log(
    store: LedgerStore, call_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing in the ledger file or the shared call log may contain a credential value."""
    try:
        secrets = dict(config_loader.credentials())
    except ConfigError as exc:  # no credentials file on this box: nothing to leak
        pytest.skip(f"no credentials file: {exc}")
    values = {name: value for name, value in secrets.items() if len(value) >= 8}
    if not values:
        pytest.skip("no credential values long enough to test")

    replay_synthetic_day(store)
    _cli_env(monkeypatch, store)
    assert ledger_cli.main(named_query="cost_per_family")["status"] == "ok"

    ledger_text = json.dumps(
        [
            row
            for stage in Stage
            for row in store.query(f'SELECT * FROM "{stage.value}"')
        ],
        default=str,
        ensure_ascii=False,
    )
    db_bytes = store.db_path.read_bytes()
    wal = store.db_path.with_suffix(".duckdb.wal")
    if wal.exists():
        db_bytes += wal.read_bytes()
    call_log_text = call_log.read_text(encoding="utf-8") if call_log.exists() else ""

    for name, value in values.items():
        assert value not in ledger_text, f"{name} value found in a ledger record"
        assert value.encode() not in db_bytes, f"{name} value found in the DuckDB file"
        assert value not in call_log_text, f"{name} value found in the call log"


# ----------------------------------------------------------------------------------------------
# Opt-in live check: one real OpenRouter call, only to prove usage.cost capture
# ----------------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_cost_from_response_captures_openrouter_usage_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One real OpenRouter call, tiny ``max_tokens``, to prove ``usage.cost`` is captured.

    Opt-in twice over: the ``live`` marker plus ``AGENTOQUANT_LIVE_OPENROUTER=1``. The key is read
    through ``config_loader.credentials()`` and never printed. Cost is ~$0.000001 per run.
    """
    if os.environ.get("AGENTOQUANT_LIVE_OPENROUTER") != "1":
        pytest.skip("set AGENTOQUANT_LIVE_OPENROUTER=1 to run the live OpenRouter cost check")
    try:
        secrets = dict(config_loader.credentials())
    except ConfigError as exc:
        pytest.skip(f"no credentials file: {exc}")
    api_key = secrets.get("AGENTOQUANT_OPENROUTER_KEY")
    if not api_key:
        pytest.skip("AGENTOQUANT_OPENROUTER_KEY is not configured")

    import httpx

    request = {
        "model": "deepseek/deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 4,
        "usage": {"include": True},
    }
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=request,
        )
    response.raise_for_status()
    body = response.json()

    cost = CostMeter.cost_from_response(body)
    assert cost >= 0.0
    assert body["usage"]["cost"] == cost

    store = LedgerStore(db_path=tmp_path / "live.duckdb")
    meter = CostMeter(store)
    meter.record(
        cycle_id="2026-09-19T00Z-live",
        role="live_smoke",
        model_family=ModelFamily.DEEPSEEK,
        harness=Harness.HERMES_OPENROUTER,
        tokens_in=int(body["usage"]["prompt_tokens"]),
        tokens_out=int(body["usage"]["completion_tokens"]),
        cost_usd=cost,
        latency_ms=1,
        schema_valid=True,
        fallback_triggered=False,
    )
    assert meter.cost_per_cycle("2026-09-19T00Z-live") == cost
