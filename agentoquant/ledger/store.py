"""DuckDB store: one physical table per :class:`~agentoquant.enums.Stage` value, plus the generic
write/query API and the outcome recording path.

Shape and frozen API: ``tasks/schema_scaffold_addendum.md`` section 4 and
``docs/phase0_implementation_plan.md`` section 5.5. Fourteen tables, one per stage; every table
carries the envelope as **real columns** (``record_id``, ``cycle_id``, ``stage``, ``ts``,
``producer_role``, ``producer_model_family``, ``harness``, ``schema_version``) rather than a nested
blob, so cross-stage joins on ``cycle_id`` stay cheap.

``LedgerStore.write`` is generic: it takes any of the fourteen payload models, so Tasks 3, 4, 5 and 6
write their records without touching ``ledger/**``.

Single-writer assumption
------------------------
DuckDB does not support multi-process writes well: one process holds the write lock on a database
file. The ledger is therefore a **single-writer** store. The hourly loop, the listeners, the Risk Gate
and the CLI must not write concurrently from separate processes; the scheduler owns the write path
and everything else reads. Connections here are short-lived (opened per operation, closed after) so a
reader never holds the file open and the writer never holds it longer than one statement.

Outcome scheduling
------------------
``due_outcomes`` lists the decision cards whose outcome row for a time-based horizon (``1h``, ``4h``,
``24h``) is due and not yet written, covering executed, rejected and vetoed cards alike, because veto
value and Adversary accuracy are measured from those counterfactual outcomes. ``write_outcome``
records the row. **Computing the outcome prices from market data is Task 7 and Task 12's job, not
this module's**; the store records whatever the outcome meter computes. The ``exit`` horizon is
event-driven (the executor writes it when the position closes), so it is never in the due list.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import duckdb
from pydantic import BaseModel, ValidationError

from agentoquant.config_loader import load_settings, repo_root
from agentoquant.enums import Harness, ModelFamily, Stage
from agentoquant.ledger.schema import (
    LedgerEnvelope,
    LedgerPayload,
    OutcomePayload,
    new_ulid,
    payload_model_for,
)

#: Environment override, mirroring ``cli.call_log_path()``'s ``AGENTOQUANT_CALL_LOG`` convention.
DB_PATH_ENV = "AGENTOQUANT_LEDGER_PATH"

#: How hard the store tries to open a ledger another process is holding. DuckDB is single-writer and
#: this store opens a short-lived connection per operation, so two processes touching the file at the
#: same instant collide. The cross-process lock below is the real fix; this retry is the backstop for
#: a holder that does not take the sidecar lock (an older process, or a tool opening the file by hand).
CONNECT_ATTEMPTS = 8
CONNECT_BACKOFF_S = 0.05
CONNECT_BACKOFF_MAX_S = 0.5


class _ReentrantFileLock:
    """An advisory cross-process lock that one thread may take more than once.

    DuckDB takes its **own** per-process lock on the database file and holds it while any connection
    is open, so two processes in a tight write loop starve each other: measured, a retry alone got two
    of three processes through and left the third starved for the whole retry window. Serialising on
    a sidecar lock file means only one process ever reaches DuckDB's lock, so the collision cannot
    happen at all.

    The depth counter makes it reentrant within a process: ``write`` calls ``query`` internally, and a
    second ``flock`` on the same handle from the same thread would deadlock against itself.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()

    def _depth(self) -> int:
        return int(getattr(self._local, "depth", 0))

    def __enter__(self) -> _ReentrantFileLock:
        depth = self._depth()
        if depth == 0:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._local.handle = handle
        self._local.depth = depth + 1
        return self

    def __exit__(self, *exc_info: object) -> None:
        depth = self._depth() - 1
        self._local.depth = depth
        if depth == 0:
            handle = getattr(self._local, "handle", None)
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
                self._local.handle = None


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, _ReentrantFileLock] = {}


def file_lock_for(db_path: Path) -> _ReentrantFileLock:
    """The process-wide lock for one ledger file, so every store on it shares one handle."""
    key = str(db_path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _ReentrantFileLock(Path(f"{db_path}.lock"))
            _LOCKS[key] = lock
    return lock

#: How long after a decision card each time-based outcome horizon falls due. ``exit`` is not
#: time-based: the executor writes it when the position closes.
HORIZON_DURATIONS: dict[str, timedelta | None] = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "24h": timedelta(hours=24),
    "exit": None,
}

OUTCOME_HORIZONS: tuple[str, ...] = tuple(HORIZON_DURATIONS)

#: The four dimensions ``cost_by`` accepts.
COST_DIMENSIONS: tuple[str, ...] = ("cycle", "family", "role", "day")

#: Envelope column names, in declaration order. Present as real columns on every table.
ENVELOPE_COLUMNS: tuple[str, ...] = tuple(LedgerEnvelope.model_fields)


class LedgerError(Exception):
    """Base for ledger failures."""


class LedgerSchemaError(LedgerError):
    """A payload does not match the stage it is being written to, or a type has no column type."""


class LedgerWriteError(LedgerError):
    """A write was refused: wrong payload, missing cycle id, conflicting envelope, duplicate outcome."""


class LedgerLockError(LedgerError):
    """The ledger file is locked by another process and the retries were exhausted.

    DuckDB permits one writer at a time and this store opens a short-lived connection per operation,
    so two processes that open the file at the same instant collide. The lock is released as soon as
    the other connection closes, which is why a bounded retry absorbs it - but when it cannot, the
    failure is named rather than surfacing as a bare ``duckdb.IOException`` out of a constructor,
    where the paper loop would die before its own error handling.
    """


# ----------------------------------------------------------------------------------------------
# Payload model -> DuckDB columns
# ----------------------------------------------------------------------------------------------


def _unwrap(annotation: Any) -> Any:
    """Strip ``Annotated`` and ``Optional`` wrappers so the base type can be mapped."""
    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
            continue
        if origin is Union:
            args = [arg for arg in get_args(annotation) if arg is not type(None)]
            if len(args) == 1:
                annotation = args[0]
                continue
        return annotation


def _column_type(annotation: Any) -> str:
    base = _unwrap(annotation)
    if base is datetime:
        return "TIMESTAMPTZ"
    if base is str:
        return "VARCHAR"
    if base is bool:
        return "BOOLEAN"
    if base is int:
        return "BIGINT"
    if base is float:
        return "DOUBLE"
    if base in (dict, list) or get_origin(base) in (dict, list, tuple, set, frozenset):
        # Parameterised containers (list[str] in five payloads, dict[str, int], ...) store as JSON.
        return "JSON"
    if isinstance(base, type) and issubclass(base, Enum):
        return "VARCHAR"
    raise LedgerSchemaError(f"no DuckDB column type for annotation {annotation!r}")


def _columns_for(payload_model: type[BaseModel]) -> list[tuple[str, str]]:
    """Envelope columns first, then the payload's own columns. The envelope wins on a name clash.

    ``llm_call`` is the one stage whose payload repeats an envelope name: the addendum gives both the
    envelope and the payload a ``harness`` field, and they mean the same thing (which harness tier ran
    the role). One physical column serves both; ``write`` refuses a write where the two disagree.
    """
    columns: list[tuple[str, str]] = [
        (name, _column_type(field.annotation))
        for name, field in LedgerEnvelope.model_fields.items()
    ]
    taken = {name for name, _ in columns}
    for name, field in payload_model.model_fields.items():
        if name in taken:
            continue
        columns.append((name, _column_type(field.annotation)))
    return columns


#: The physical schema: one entry per stage, table name = stage value.
TABLE_COLUMNS: dict[Stage, list[tuple[str, str]]] = {
    stage: _columns_for(payload_model_for(stage)) for stage in Stage
}

#: Stage value -> table name (they are the same string, kept explicit so nothing guesses).
STAGE_TABLES: dict[Stage, str] = {stage: stage.value for stage in Stage}

#: Stages whose write is idempotent on a natural key, and the key.
#:
#: ``risk_gate_verdict`` has **two** writers for one decision: the gate writes its own verdict
#: (``risk/gate.py``) and the hourly loop writes back the verdict it was handed
#: (``execution/paper.py``). One cycle therefore landed as two identical rows, so ``COUNT(*) ...
#: GROUP BY cycle_id`` double-counted the stage. The natural key is ``(cycle_id,
#: decision_card_id)``: a second write whose payload is **identical** to the stored one is a no-op
#: that returns the first record's id.
#:
#: The dedupe deliberately collapses identical repeats only. It cannot collapse a *conflicting*
#: re-evaluation, because ``decision_card_id`` is not a unique key in practice: the gate falls back
#: to ``selected_proposal_id`` when the card has not been stored yet (``gate.py``, ``_card_id``), so
#: two different decisions can share an id. Merging those would lose a decision's verdict, which is
#: worse than the double count this fixes.
STAGE_NATURAL_KEYS: dict[Stage, tuple[str, ...]] = {
    Stage.RISK_GATE_VERDICT: ("cycle_id", "decision_card_id"),
}


# ----------------------------------------------------------------------------------------------
# Value conversion
# ----------------------------------------------------------------------------------------------


def _to_db(value: Any) -> Any:
    """Python value -> what DuckDB stores. Enums become their ``value``, structures become JSON."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return value


def _is_json_column(type_name: Any) -> bool:
    return type_name is not None and str(type_name).upper().endswith("JSON")


def _from_db(value: Any, is_json: bool) -> Any:
    """DuckDB value -> Python value: JSON text decoded, every timestamp returned UTC-aware."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(
            UTC
        )
    if is_json and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:  # pragma: no cover - defensive, JSON columns are written by us
            return value
    return value


def default_db_path() -> Path:
    """The ledger file: ``AGENTOQUANT_LEDGER_PATH`` if set, else ``settings.ledger_path``.

    ``config/settings.yaml`` carries ``ledger_path: data/ledger.duckdb``; a relative path is resolved
    against the repo root. ``/data/`` is gitignored, so the database file is never committed.
    """
    override = os.environ.get(DB_PATH_ENV)
    if override:
        return Path(override)
    relative = Path(load_settings().ledger_path)
    return relative if relative.is_absolute() else repo_root() / relative


# ----------------------------------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------------------------------


class LedgerStore:
    """The decision ledger. One table per stage, envelope columns real, short-lived connections."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = file_lock_for(self.db_path)
        self.migrate()

    # -- connection --------------------------------------------------------------------------

    def _connect(self) -> duckdb.DuckDBPyConnection:
        """Open a fresh connection, retrying while another process holds the file lock.

        DuckDB is single-writer, so a second process opening the same file at the same instant gets
        ``Conflicting lock is held``. The lock is released as soon as the short-lived connection
        closes, so a bounded retry with jitter absorbs the collision: without it two production
        processes cannot share the ledger the plan calls the single source of truth, and the failure
        lands in ``__init__`` as an uncaught ``IOException``.
        """
        delay = CONNECT_BACKOFF_S
        last: Exception | None = None
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                connection = duckdb.connect(str(self.db_path))
            except duckdb.IOException as exc:
                last = exc
                if attempt == CONNECT_ATTEMPTS - 1:
                    break
                # Jittered, so two processes retrying in lockstep do not collide again on every try.
                time.sleep(delay * (0.5 + random.random()))
                delay = min(delay * 2, CONNECT_BACKOFF_MAX_S)
                continue
            connection.execute("SET TimeZone='UTC'")
            return connection
        raise LedgerLockError(
            f"could not open {self.db_path} after {CONNECT_ATTEMPTS} attempts: "
            f"{type(last).__name__}: {last}"
        ) from last

    @contextmanager
    def _connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """A connection held under the cross-process lock and closed on the way out.

        Every path that touches the file goes through here, so only one process ever reaches DuckDB's
        own lock and the two can share the ledger the plan calls the single source of truth. The
        connection is always closed: DuckDB's lock is held while any connection is open.
        """
        with self._lock:
            connection = self._connect()
            try:
                yield connection
            finally:
                connection.close()

    def close(self) -> None:
        """No persistent connection is held, so this is a no-op kept for the frozen interface."""
        return None

    # -- schema ------------------------------------------------------------------------------

    def migrate(self) -> None:
        """Create the fourteen tables (and their cycle_id indexes). Idempotent, safe to re-run."""
        with self._connection() as connection:
            for stage in Stage:
                table = STAGE_TABLES[stage]
                columns = ", ".join(f'"{name}" {kind}' for name, kind in TABLE_COLUMNS[stage])
                connection.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({columns})')
                connection.execute(
                    f'CREATE INDEX IF NOT EXISTS "idx_{table}_cycle_id" '
                    f'ON "{table}" ("cycle_id")'
                )

    def tables(self) -> list[str]:
        """The ledger's table names, sorted."""
        rows = self.query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        )
        return [row["table_name"] for row in rows]

    def table_columns(self, stage: Stage) -> list[tuple[str, str]]:
        """``PRAGMA table_info`` for one stage's table: (name, type) in physical order."""
        rows = self.query(f'PRAGMA table_info("{STAGE_TABLES[stage]}")')
        return [(row["name"], row["type"]) for row in rows]

    # -- write -------------------------------------------------------------------------------

    def write(
        self,
        stage: Stage,
        cycle_id: str,
        payload: BaseModel,
        *,
        producer_role: str,
        producer_model_family: ModelFamily = ModelFamily.NONE,
        harness: Harness = Harness.NONE,
    ) -> str:
        """Write one stage record and return its ``record_id`` (a ULID generated here).

        ``payload`` must be the payload model registered for ``stage`` (a model or a plain dict with
        the same fields is validated into it). Envelope fields default from the payload where the
        payload carries them (``llm_call`` carries ``model_family`` and ``harness``), so the envelope
        and the payload can never disagree.
        """
        if not isinstance(stage, Stage):
            raise LedgerSchemaError(f"stage must be a Stage member, got {type(stage).__name__}")
        if not isinstance(cycle_id, str) or not cycle_id.strip():
            raise LedgerWriteError("cycle_id must be a non-empty string")
        model = payload_model_for(stage)
        body_model = self._coerce_payload(stage, payload, model)

        payload_family = getattr(body_model, "model_family", None)
        payload_harness = getattr(body_model, "harness", None)
        if producer_model_family is ModelFamily.NONE and isinstance(payload_family, ModelFamily):
            producer_model_family = payload_family
        if harness is Harness.NONE and isinstance(payload_harness, Harness):
            harness = payload_harness
        if not isinstance(producer_model_family, ModelFamily):
            raise LedgerWriteError("producer_model_family must be a ModelFamily member")
        if not isinstance(harness, Harness):
            raise LedgerWriteError("harness must be a Harness member")

        envelope = LedgerEnvelope(
            record_id=new_ulid(),
            cycle_id=cycle_id,
            stage=stage,
            ts=datetime.now(UTC),
            producer_role=producer_role,
            producer_model_family=producer_model_family,
            harness=harness,
        )
        env = envelope.model_dump()
        body = body_model.model_dump()
        for name in set(env) & set(body):
            if env[name] != body[name]:
                raise LedgerWriteError(
                    f"stage {stage.value}: envelope and payload disagree on {name!r} "
                    f"({env[name]!r} vs {body[name]!r})"
                )
        row = {**env, **body}
        natural_key = STAGE_NATURAL_KEYS.get(stage)
        if natural_key is not None:
            existing_id = self._dedupe_on_natural_key(stage, natural_key, body_model, row)
            if existing_id is not None:
                return existing_id
        columns = TABLE_COLUMNS[stage]
        names = ", ".join(f'"{name}"' for name, _ in columns)
        placeholders = ", ".join("?" for _ in columns)
        values = [_to_db(row.get(name)) for name, _ in columns]
        with self._connection() as connection:
            connection.execute(
                f'INSERT INTO "{STAGE_TABLES[stage]}" ({names}) VALUES ({placeholders})', values
            )
        return envelope.record_id

    def _coerce_payload(
        self, stage: Stage, payload: BaseModel, model: type[LedgerPayload]
    ) -> LedgerPayload:
        if isinstance(payload, model):
            return payload
        if isinstance(payload, BaseModel):
            data = payload.model_dump()
        elif isinstance(payload, dict):
            data = dict(payload)
        else:
            raise LedgerSchemaError(
                f"stage {stage.value} expects {model.__name__}, got {type(payload).__name__}"
            )
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise LedgerSchemaError(
                f"stage {stage.value} payload is not a valid {model.__name__}: "
                f"{len(exc.errors())} validation error(s); first: {exc.errors()[0].get('loc')} "
                f"{exc.errors()[0].get('msg')}"
            ) from exc

    def _dedupe_on_natural_key(
        self,
        stage: Stage,
        key_columns: tuple[str, ...],
        body_model: LedgerPayload,
        row: dict[str, Any],
    ) -> str | None:
        """The stored record's id when an **identical** record already holds this stage's natural key.

        Returns ``None`` when the key is absent or when the stored payload differs, in which case the
        caller writes the new row. Identical repeats are collapsed (one decision, one verdict row);
        differing payloads are not, because a shared key is not proof of a shared decision - see
        :data:`STAGE_NATURAL_KEYS`.
        """
        where = " AND ".join(f'"{name}" = ?' for name in key_columns)
        stored_rows = self.query(
            f'SELECT * FROM "{STAGE_TABLES[stage]}" WHERE {where}',
            [_to_db(row[name]) for name in key_columns],
        )
        if not stored_rows:
            return None
        payload_columns = [
            name for name, _ in TABLE_COLUMNS[stage] if name not in ENVELOPE_COLUMNS
        ]
        body = body_model.model_dump(mode="json")
        for stored in stored_rows:
            if all(stored.get(name) == body.get(name) for name in payload_columns):
                return str(stored["record_id"])
        return None

    # -- read --------------------------------------------------------------------------------

    def query(self, sql: str, params: Sequence | None = None) -> list[dict]:
        """Run one statement and return rows as dicts. JSON columns decode, timestamps are UTC."""
        with self._connection() as connection:
            cursor = connection.execute(sql, params if params is not None else [])
            description = cursor.description or []
            names = [column[0] for column in description]
            json_indexes = {
                index
                for index, column in enumerate(description)
                if len(column) > 1 and _is_json_column(column[1])
            }
            rows = cursor.fetchall()
        return [
            {
                name: _from_db(value, index in json_indexes)
                for index, (name, value) in enumerate(zip(names, row, strict=True))
            }
            for row in rows
        ]

    def cycle_records(self, cycle_id: str) -> list[dict]:
        """One cycle's full story: every stage's records, ordered by time then record id."""
        records: list[dict] = []
        for stage in Stage:
            records.extend(
                self.query(
                    f'SELECT * FROM "{STAGE_TABLES[stage]}" WHERE "cycle_id" = ?', [cycle_id]
                )
            )
        records.sort(key=lambda record: (record.get("ts"), record.get("record_id") or ""))
        return records

    def cycle_cost(self, cycle_id: str) -> list[dict]:
        """What one cycle cost, per role and model family, from the llm_call table."""
        return self.query(
            'SELECT "role" AS role, "model_family" AS model_family, '
            'COUNT(*) AS calls, ROUND(SUM("cost_usd"), 6) AS cost_usd, '
            'SUM("tokens_in") AS tokens_in, SUM("tokens_out") AS tokens_out '
            'FROM "llm_call" WHERE "cycle_id" = ? '
            'GROUP BY 1, 2 ORDER BY cost_usd DESC, role',
            [cycle_id],
        )

    def cost_by(self, dimension: Literal["cycle", "family", "role", "day"]) -> list[dict]:
        """LLM cost rolled up by ``cycle``, ``family``, ``role`` or ``day`` (UTC)."""
        if dimension not in COST_DIMENSIONS:
            raise ValueError(
                f"unknown cost dimension {dimension!r}; expected one of {', '.join(COST_DIMENSIONS)}"
            )
        selectors = {
            "cycle": '"cycle_id" AS cycle_id',
            "family": '"model_family" AS model_family',
            "role": '"role" AS role',
            "day": 'CAST("ts" AS DATE) AS day',
        }
        return self.query(
            f'SELECT {selectors[dimension]}, COUNT(*) AS calls, '
            f'ROUND(SUM("cost_usd"), 6) AS cost_usd, '
            f'SUM("tokens_in") AS tokens_in, SUM("tokens_out") AS tokens_out '
            f'FROM "llm_call" GROUP BY 1 ORDER BY 1'
        )

    # -- outcomes ----------------------------------------------------------------------------

    def due_outcomes(self, horizon: str, as_of: datetime | None = None) -> list[dict]:
        """Decision cards whose ``horizon`` outcome is due and not yet written.

        Covers every card that carries a coin and an action, whatever the Risk Gate verdict or the
        human action was: executed, rejected and vetoed cards all get an outcome row, which is what
        makes veto value and Adversary accuracy measurable. ``as_of`` defaults to now (UTC).
        """
        if horizon not in HORIZON_DURATIONS:
            raise ValueError(
                f"unknown outcome horizon {horizon!r}; expected one of {', '.join(OUTCOME_HORIZONS)}"
            )
        duration = HORIZON_DURATIONS[horizon]
        if duration is None:
            # ``exit`` is written by the executor when the position closes, not by the clock.
            return []
        moment = (as_of or datetime.now(UTC)).astimezone(UTC)
        cutoff = moment - duration
        rows = self.query(
            'SELECT c."record_id" AS decision_card_id, c."cycle_id" AS cycle_id, c."ts" AS ts, '
            'c."action" AS action, c."coin" AS coin, c."sleeve" AS sleeve, '
            'c."size_pct" AS size_pct, c."confidence" AS confidence, '
            'c."confidence_band" AS confidence_band, '
            '(SELECT v."verdict" FROM "risk_gate_verdict" v '
            ' WHERE v."decision_card_id" = c."record_id" ORDER BY v."ts" DESC LIMIT 1) AS verdict, '
            '(SELECT h."command" FROM "human_action" h '
            ' WHERE h."decision_card_id" = c."record_id" ORDER BY h."ts" DESC LIMIT 1) '
            'AS human_command '
            'FROM "decision_card" c '
            'WHERE c."ts" <= ? AND c."coin" IS NOT NULL AND c."action" <> ? '
            'AND NOT EXISTS (SELECT 1 FROM "outcome" o '
            '                WHERE o."decision_card_id" = c."record_id" AND o."horizon" = ?) '
            'ORDER BY c."ts", c."record_id"',
            [cutoff, "hold", horizon],
        )
        for row in rows:
            row["horizon"] = horizon
            row["due_at"] = row["ts"] + duration
        return rows

    def write_outcome(
        self,
        decision_card_id: str,
        horizon: str,
        *,
        cycle_id: str | None = None,
        pnl_pct: float | None = None,
        pnl_abs: float | None = None,
        realized_up: bool | None = None,
        still_open: bool = False,
        replace: bool = False,
        producer_role: str = "outcome_meter",
        producer_model_family: ModelFamily = ModelFamily.NONE,
        harness: Harness = Harness.NONE,
    ) -> str:
        """Record one outcome row for a decision card and return its ``record_id``.

        The card's ``cycle_id`` is looked up when not given, so outcomes stay linked. A second write
        for the same (card, horizon) is refused unless ``replace=True``: a duplicate would inflate
        every hit-rate and PnL roll-up that joins on it.
        """
        if horizon not in HORIZON_DURATIONS:
            raise ValueError(
                f"unknown outcome horizon {horizon!r}; expected one of {', '.join(OUTCOME_HORIZONS)}"
            )
        card = self.query(
            'SELECT "cycle_id" FROM "decision_card" WHERE "record_id" = ?', [decision_card_id]
        )
        if not card:
            raise LedgerWriteError(f"unknown decision card: {decision_card_id!r}")
        resolved_cycle = cycle_id or card[0]["cycle_id"]
        existing = self.query(
            'SELECT "record_id" FROM "outcome" WHERE "decision_card_id" = ? AND "horizon" = ?',
            [decision_card_id, horizon],
        )
        if existing:
            if not replace:
                raise LedgerWriteError(
                    f"outcome already recorded for card {decision_card_id!r} at horizon "
                    f"{horizon!r}; pass replace=True to overwrite"
                )
            with self._connection() as connection:
                connection.execute(
                    'DELETE FROM "outcome" WHERE "decision_card_id" = ? AND "horizon" = ?',
                    [decision_card_id, horizon],
                )
        payload = OutcomePayload(
            decision_card_id=decision_card_id,
            horizon=horizon,
            pnl_pct=pnl_pct,
            pnl_abs=pnl_abs,
            realized_up=realized_up,
            still_open=still_open,
        )
        return self.write(
            Stage.OUTCOME,
            resolved_cycle,
            payload,
            producer_role=producer_role,
            producer_model_family=producer_model_family,
            harness=harness,
        )


def new_record_id() -> str:
    """A fresh ULID for a ledger record."""
    return new_ulid()


__all__ = [
    "COST_DIMENSIONS",
    "DB_PATH_ENV",
    "ENVELOPE_COLUMNS",
    "HORIZON_DURATIONS",
    "OUTCOME_HORIZONS",
    "STAGE_TABLES",
    "STAGE_NATURAL_KEYS",
    "TABLE_COLUMNS",
    "LedgerError",
    "LedgerSchemaError",
    "LedgerStore",
    "LedgerWriteError",
    "default_db_path",
    "new_record_id",
]
