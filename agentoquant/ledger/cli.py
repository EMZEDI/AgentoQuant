"""``agentoquant ledger query``: read the decision ledger by SQL or by a named query.

Dispatched by ``agentoquant/cli.py`` (frozen table, ``docs/phase0_implementation_plan.md`` section
5.3), which calls ``main(**LedgerQueryInput(...).model_dump())``::

    agentoquant ledger query --sql "SELECT stage, COUNT(*) FROM decision_card GROUP BY 1"
    agentoquant ledger query --query hit_rate_by_band
    agentoquant ledger query --query cost_per_family
    agentoquant ledger query --query cycle_story --param cycle_id=2026-09-18T00Z-0001

The same function is the MCP tool ``ledger_query`` (``agentoquant/mcp_server.py``), so the CLI and
MCP paths return identical rows.

Exit codes follow the CLI convention: 0 ok, 2 config or usage error (``ConfigError``, which
``cli._invoke`` maps to 2), 1 the command failed (a SQL error comes back as ``status="error"``).

Read-only by construction: this surface only ever SELECTs, and an ad-hoc ``--sql`` statement must be a
single read statement. The ledger is the source of truth for the whole system, so no query path is
allowed to mutate it.

Standalone use (``python -m agentoquant.ledger.cli --named hit_rate_by_band``) is also supported; the
``--named`` spelling lives here because the top-level CLI's argument surface is owned by Task 1 and
frozen, where the same option is spelled ``--query``/``-q``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import duckdb

from agentoquant.config_loader import ConfigError
from agentoquant.enums import Stage
from agentoquant.ledger.store import LedgerStore

# ----------------------------------------------------------------------------------------------
# Named queries
# ----------------------------------------------------------------------------------------------

#: Every stage's (stage, cycle_id) pairs, so cycle-level roll-ups read all fourteen tables at once.
_STAGE_CYCLE_UNION = " UNION ALL ".join(
    f'SELECT \'{stage.value}\' AS stage, "cycle_id" AS cycle_id, "record_id" AS record_id, '
    f'"ts" AS ts, "producer_role" AS producer_role, '
    f'"producer_model_family" AS producer_model_family, "harness" AS harness '
    f'FROM "{stage.value}"'
    for stage in Stage
)


@dataclass(frozen=True)
class NamedQuery:
    """A query shipped by the ledger: fixed SQL, declared columns, declared required parameters."""

    name: str
    description: str
    sql: str
    columns: tuple[str, ...]
    params: tuple[str, ...] = field(default=())


NAMED_QUERIES: dict[str, NamedQuery] = {
    query.name: query
    for query in (
        NamedQuery(
            name="hit_rate_by_band",
            description="Hit rate by confidence band and horizon, over every card with an outcome "
            "(executed, rejected and vetoed alike).",
            sql=(
                'SELECT c."confidence_band" AS confidence_band, o."horizon" AS horizon, '
                'COUNT(*) AS n, '
                'SUM(CASE WHEN o."realized_up" THEN 1 ELSE 0 END) AS hits, '
                'ROUND(SUM(CASE WHEN o."realized_up" THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) '
                'AS hit_rate '
                'FROM "decision_card" c JOIN "outcome" o ON o."decision_card_id" = c."record_id" '
                'WHERE o."realized_up" IS NOT NULL '
                'GROUP BY 1, 2 ORDER BY 1, 2'
            ),
            columns=("confidence_band", "horizon", "n", "hits", "hit_rate"),
        ),
        NamedQuery(
            name="cost_per_family",
            description="LLM calls and dollars per model family: the cost side of value per family.",
            sql=(
                'SELECT "model_family" AS model_family, COUNT(*) AS calls, '
                'COUNT(DISTINCT "cycle_id") AS decisions, '
                'ROUND(SUM("cost_usd"), 6) AS cost_usd, '
                'ROUND(AVG("cost_usd"), 6) AS cost_per_call_usd, '
                'ROUND(SUM("cost_usd") / COUNT(DISTINCT "cycle_id"), 6) AS cost_per_decision_usd, '
                'SUM("tokens_in") AS tokens_in, SUM("tokens_out") AS tokens_out '
                'FROM "llm_call" GROUP BY 1 ORDER BY cost_usd DESC'
            ),
            columns=(
                "model_family",
                "calls",
                "decisions",
                "cost_usd",
                "cost_per_call_usd",
                "cost_per_decision_usd",
                "tokens_in",
                "tokens_out",
            ),
        ),
        NamedQuery(
            name="cost_per_cycle",
            description="LLM dollars per cycle, most expensive first.",
            sql=(
                'SELECT "cycle_id" AS cycle_id, COUNT(*) AS calls, '
                'ROUND(SUM("cost_usd"), 6) AS cost_usd '
                'FROM "llm_call" GROUP BY 1 ORDER BY cost_usd DESC'
            ),
            columns=("cycle_id", "calls", "cost_usd"),
        ),
        NamedQuery(
            name="cost_per_role",
            description="LLM dollars per role, most expensive first.",
            sql=(
                'SELECT "role" AS role, "model_family" AS model_family, COUNT(*) AS calls, '
                'ROUND(SUM("cost_usd"), 6) AS cost_usd '
                'FROM "llm_call" GROUP BY 1, 2 ORDER BY cost_usd DESC'
            ),
            columns=("role", "model_family", "calls", "cost_usd"),
        ),
        NamedQuery(
            name="cycle_cost",
            description="What one cycle cost, per role and model family.",
            sql=(
                'SELECT "role" AS role, "model_family" AS model_family, COUNT(*) AS calls, '
                'ROUND(SUM("cost_usd"), 6) AS cost_usd, '
                'SUM("tokens_in") AS tokens_in, SUM("tokens_out") AS tokens_out '
                'FROM "llm_call" WHERE "cycle_id" = $cycle_id '
                'GROUP BY 1, 2 ORDER BY cost_usd DESC, role'
            ),
            columns=("role", "model_family", "calls", "cost_usd", "tokens_in", "tokens_out"),
            params=("cycle_id",),
        ),
        NamedQuery(
            name="cycle_story",
            description="One cycle's full story: every stage's records in time order.",
            sql=(
                "SELECT stage, record_id, ts, producer_role, producer_model_family, harness "
                f'FROM ({_STAGE_CYCLE_UNION}) WHERE "cycle_id" = $cycle_id '
                "ORDER BY ts, record_id"
            ),
            columns=(
                "stage",
                "record_id",
                "ts",
                "producer_role",
                "producer_model_family",
                "harness",
            ),
            params=("cycle_id",),
        ),
        NamedQuery(
            name="cycles",
            description="Every cycle in the ledger with its record count and time span.",
            sql=(
                'SELECT "cycle_id" AS cycle_id, COUNT(*) AS records, '
                'COUNT(DISTINCT stage) AS stages_present, MIN("ts") AS first_ts, '
                f'MAX("ts") AS last_ts FROM ({_STAGE_CYCLE_UNION}) '
                'GROUP BY 1 ORDER BY cycle_id'
            ),
            columns=("cycle_id", "records", "stages_present", "first_ts", "last_ts"),
        ),
        NamedQuery(
            name="stage_counts",
            description="Record count per stage, over every cycle in the ledger.",
            sql=(
                f'SELECT stage, COUNT(*) AS records FROM ({_STAGE_CYCLE_UNION}) '
                'GROUP BY 1 ORDER BY 1'
            ),
            columns=("stage", "records"),
        ),
        NamedQuery(
            name="orphans",
            description="Cycles missing one or more of the fourteen stages (an empty result is the "
            "healthy case: every record linked).",
            sql=(
                'SELECT "cycle_id" AS cycle_id, COUNT(DISTINCT stage) AS stages_present, '
                f'(14 - COUNT(DISTINCT stage)) AS stages_missing FROM ({_STAGE_CYCLE_UNION}) '
                'GROUP BY 1 HAVING COUNT(DISTINCT stage) < 14 ORDER BY cycle_id'
            ),
            columns=("cycle_id", "stages_present", "stages_missing"),
        ),
    )
}

#: Statements ``--sql`` is allowed to run: reads only, one statement.
_READ_ONLY_STARTS = (
    "select",
    "with",
    "from",
    "describe",
    "summarize",
    "pragma",
    "show",
    "explain",
)


# ----------------------------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def render_table(columns: Sequence[str], rows: Sequence[dict]) -> str:
    """A plain readable table: header, separator, one line per row."""
    columns = list(columns)
    if not columns:
        return "(no columns)"
    if not rows:
        return "(0 rows)"
    body = [[_cell(row.get(column)) for column in columns] for row in rows]
    widths = [
        max(len(column), *(len(line[index]) for line in body))
        for index, column in enumerate(columns)
    ]
    lines = [
        " | ".join(column.ljust(widths[index]) for index, column in enumerate(columns)),
        "-+-".join("-" * width for width in widths),
    ]
    lines.extend(
        " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(line)) for line in body
    )
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# Query execution
# ----------------------------------------------------------------------------------------------


def _assert_read_only(sql: str) -> None:
    body = sql.strip().lstrip("(").strip()
    if not body:
        raise ConfigError("--sql was empty")
    first = body.split(None, 1)[0].lower().rstrip(";")
    if first not in _READ_ONLY_STARTS:
        raise ConfigError(
            f"--sql must be a read statement ({', '.join(_READ_ONLY_STARTS)}); got {first!r}. "
            "The ledger is written through LedgerStore.write, never through a query."
        )
    if ";" in sql.strip().rstrip(";"):
        raise ConfigError("--sql must be a single statement")


def _run_sql(store: LedgerStore, sql: str, params: dict[str, str]) -> tuple[list[str], list[dict]]:
    _assert_read_only(sql)
    rows = store.query(sql, params or None)
    columns = list(rows[0].keys()) if rows else []
    return columns, rows


def _run_named(
    store: LedgerStore, name: str, params: dict[str, str]
) -> tuple[list[str], list[dict]]:
    query = NAMED_QUERIES.get(name)
    if query is None:
        raise ConfigError(
            f"unknown named query {name!r}; available: {', '.join(sorted(NAMED_QUERIES))}"
        )
    missing = [key for key in query.params if key not in params]
    if missing:
        raise ConfigError(
            f"named query {name!r} needs --param {missing[0]}=... (missing: {', '.join(missing)})"
        )
    bound = {key: params[key] for key in query.params}
    rows = store.query(query.sql, bound)
    return list(query.columns), rows


def main(
    sql: str | None = None,
    named_query: str | None = None,
    params: dict[str, str] | None = None,
    json_output: bool = False,
    named: str | None = None,
) -> dict[str, Any]:
    """Run one ledger query. Returns the CLI/MCP output envelope's payload fields.

    ``named`` is an alias for ``named_query`` so the module can also be driven directly with
    ``--named``; the top-level CLI passes ``named_query`` (its option is ``--query``).
    """
    bound_params = dict(params or {})
    choice = named_query or named
    if sql and choice:
        raise ConfigError("pass exactly one of --sql or a named query")
    if not sql and not choice:
        raise ConfigError(
            f"pass --sql '...' or a named query (available: {', '.join(sorted(NAMED_QUERIES))})"
        )

    store = LedgerStore()
    try:
        if sql:
            label = "sql"
            columns, rows = _run_sql(store, sql, bound_params)
        else:
            label = f"named:{choice}"
            columns, rows = _run_named(store, str(choice), bound_params)
    except duckdb.Error as exc:
        # The command itself failed: the CLI exits 1, the MCP tool returns a typed error.
        return {"status": "error", "message": f"query failed: {type(exc).__name__}: {exc}"}

    if not columns and rows:  # pragma: no cover - defensive
        columns = list(rows[0].keys())
    summary = f"{len(rows)} row(s) from {label}"
    if json_output:
        return {
            "status": "ok",
            "message": summary,
            "query": label,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
        }
    print(render_table(columns, rows) if rows else f"(0 rows) from {label}")
    return {"status": "ok", "message": summary}


# ----------------------------------------------------------------------------------------------
# Standalone entry point
# ----------------------------------------------------------------------------------------------


def _standalone(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m agentoquant.ledger.cli", description="Query the AgentoQuant decision ledger."
    )
    parser.add_argument("--sql", "-s", default=None, help="Raw read-only SQL")
    parser.add_argument("--named", "-n", default=None, help="A named query shipped by the ledger")
    parser.add_argument(
        "--query", "-q", dest="named_query", default=None, help="Alias for --named"
    )
    parser.add_argument("--param", "-p", action="append", default=[], help="Bind parameter key=value")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Emit JSON")
    parser.add_argument(
        "--list-named", action="store_true", help="List the named queries and exit"
    )
    args = parser.parse_args(argv)

    if args.list_named:
        for name in sorted(NAMED_QUERIES):
            print(f"{name}\t{NAMED_QUERIES[name].description}")
        return 0

    params: dict[str, str] = {}
    for item in args.param:
        key, separator, value = item.partition("=")
        if not separator or not key:
            print(f"error: --param expects key=value, got {item!r}", file=sys.stderr)
            return 2
        params[key] = value

    try:
        result = main(
            sql=args.sql,
            named_query=args.named_query,
            params=params,
            json_output=args.json_output,
            named=args.named,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - the CLI contract is an exit code, not a traceback
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=_cell))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":  # pragma: no cover - exercised through _standalone in tests
    raise SystemExit(_standalone())


__all__ = ["NAMED_QUERIES", "NamedQuery", "main", "render_table"]
