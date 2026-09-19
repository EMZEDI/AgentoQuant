"""The CLI: one dispatch table, lazily resolved so later tasks never edit this file.

Frozen contract (``docs/phase0_implementation_plan.md`` section 5.3): :data:`COMMAND_TARGETS` maps a
command name to ``module:attribute``. A target that does not exist yet exits non-zero with
``not implemented yet (owned by <phase>)`` and no traceback.

Dispatch convention (frozen here, documented in ``README.md``): the CLI owns the argument surface,
validates the command's typed input model, and calls the target with that model's fields as keyword
arguments::

    main(**LedgerQueryInput(sql="select 1").model_dump())

The target returns a :class:`CommandOutput`, a plain dict with the same fields, or ``None``. Every
command also carries a typed output schema (:class:`CommandOutput`) so the CLI and the MCP server
return the same shape. Every invocation is appended to one call log
(``logs/mcp_calls.jsonl`` by default) with the calling client, so a call from Claude, from Hermes and
from the CLI land in the same file.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentoquant.config_loader import ConfigError, load_settings, repo_root, validate_all
from agentoquant.enums import Sleeve

# --------------------------------------------------------------------------------------
# Frozen dispatch table (plan section 5.3). Do not reorder; later tasks add their module only.
# --------------------------------------------------------------------------------------

COMMAND_TARGETS: dict[str, str] = {
    "ingest": "agentoquant.data.ingest:main",
    "forecast": "agentoquant.models.forecast:main",
    "decide": "agentoquant.agents.decide:main",
    "paper": "agentoquant.execution.paper:main",
    "review": "agentoquant.reports.daily_review:main",
    "retrain": "agentoquant.models.train:main",
    "backtest": "agentoquant.validation.backtest:main",
    "propose-skill": "agentoquant.skills.propose:main",
    "ledger": "agentoquant.ledger.cli:main",
    "worldview": "agentoquant.worldview_cli:main",
    "fund": "agentoquant.risk.funding_floor:cli",
}

#: Which task/phase owns each command's implementation. Used verbatim in the stub message.
COMMAND_OWNERS: dict[str, str] = {
    "ingest": "Phase 0 Task 3",
    "forecast": "Phase 1",
    "decide": "Phase 2",
    "paper": "Phase 0 Task 6",
    "review": "Phase 3",
    "retrain": "Phase 1",
    "backtest": "Phase 1",
    "propose-skill": "Phase 3",
    "ledger": "Phase 0 Task 2",
    "worldview": "Phase 2",
    "fund": "Phase 0 Task 5",
}

#: Exit codes: 2 = config/usage error, 3 = not implemented yet, 1 = the command itself failed.
EXIT_NOT_IMPLEMENTED = 3
EXIT_CONFIG = 2


# --------------------------------------------------------------------------------------
# Typed input schemas, one per command. The MCP server publishes these as JSON Schema.
# --------------------------------------------------------------------------------------


class CommandInput(BaseModel):
    """Base class for every command's typed input."""

    model_config = ConfigDict(extra="forbid")


class IngestInput(CommandInput):
    cycle_id: str | None = Field(default=None, description="Cycle id, e.g. 2026-09-18T14Z-0001")
    symbols: list[str] = Field(default_factory=list, description="Empty means the configured universe")
    force: bool = Field(default=False, description="Ignore the cache for this snapshot")


class ForecastInput(CommandInput):
    cycle_id: str | None = Field(default=None)
    coin: str | None = Field(default=None, description="Empty means every coin in the universe")
    horizon: Literal["4h", "24h"] = "4h"


class DecideInput(CommandInput):
    cycle_id: str | None = Field(default=None)
    dry_run: bool = Field(default=True, description="Never place anything; print the card only")


class PaperInput(CommandInput):
    cycle_id: str | None = Field(default=None)
    placeholder: bool = Field(
        default=False, description="Use the Phase 0 placeholder decision source instead of the cascade"
    )
    hours: int = Field(default=1, ge=1, description="How many hourly cycles to run")


class ReviewInput(CommandInput):
    date: str | None = Field(default=None, description="ISO date, default today in settings timezone")
    send: bool = Field(default=False, description="Deliver the digest to Telegram")


class RetrainInput(CommandInput):
    coin: str | None = Field(default=None)
    reason: str | None = Field(default=None)


class BacktestInput(CommandInput):
    strategy: str | None = Field(default=None)
    timerange: str | None = Field(default=None, description="e.g. 20260101-20260901")
    fee_tier: str | None = Field(default=None, description="Tier id from config/fee_tiers.yaml")


class ProposeSkillInput(CommandInput):
    skill: str | None = Field(default=None, description="Skill name to propose or update")
    note: str | None = Field(default=None)


class LedgerQueryInput(CommandInput):
    sql: str | None = Field(default=None, description="Raw SQL (mutually exclusive with named_query)")
    named_query: str | None = Field(default=None, description="A named query shipped by the ledger")
    params: dict[str, str] = Field(default_factory=dict, description="Bind parameters")
    json_output: bool = Field(default=False, description="Emit JSON instead of a table")


class WorldviewGetInput(CommandInput):
    prior_id: str | None = Field(default=None, description="Empty returns the whole worldview doc")
    doc_version: str | None = Field(default=None)


class WorldviewFlagInput(CommandInput):
    prior_id: str = Field(min_length=1, description="The prior being flagged (never edited)")
    note: str = Field(min_length=1, description="Why it looks miscalibrated")
    cycle_id: str | None = Field(default=None)


class FundRequestInput(CommandInput):
    sleeve: Sleeve = Field(description="Which sleeve needs the money")
    amount: float = Field(gt=0)
    currency: Literal["CAD", "USD"] = "CAD"
    reason: Literal["funding_floor_breach", "unsizeable_high_confidence_card"] = "funding_floor_breach"
    deadline_hours: int = Field(default=48, ge=1)


# --------------------------------------------------------------------------------------
# Typed output schema, shared by every command and by the MCP tools.
# --------------------------------------------------------------------------------------


class CommandOutput(BaseModel):
    """The typed output of every command. Owning tasks may narrow ``payload``, never this envelope."""

    model_config = ConfigDict(extra="forbid")

    command: str
    status: Literal["ok", "not_implemented", "error"]
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    not_implemented: bool = False

    @classmethod
    def stub(cls, command: str, owner: str) -> CommandOutput:
        return cls(
            command=command,
            status="not_implemented",
            message=f"not implemented yet (owned by {owner})",
            payload={"owner": owner},
            not_implemented=True,
        )


# --------------------------------------------------------------------------------------
# Command registry: canonical leaf command names, one row per command.
# --------------------------------------------------------------------------------------


class CommandSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    #: Canonical name, spaces and hyphens as the plan spells it (e.g. "ledger query").
    name: str
    #: Top-level CLI command this leaf hangs off ("ledger query" -> "ledger").
    group: str
    #: Subcommand name inside the group, or None for a top-level command.
    subcommand: str | None = None
    target: str
    owner: str
    summary: str
    input_model: type[CommandInput]

    @property
    def mcp_tool_name(self) -> str:
        return self.name.replace(" ", "_").replace("-", "_")


def _spec(
    name: str,
    target: str,
    owner: str,
    summary: str,
    input_model: type[CommandInput],
    group: str | None = None,
    subcommand: str | None = None,
) -> CommandSpec:
    group = group or name
    return CommandSpec(
        name=name,
        group=group,
        subcommand=subcommand,
        target=target,
        owner=owner,
        summary=summary,
        input_model=input_model,
    )


COMMAND_SPECS: dict[str, CommandSpec] = {
    spec.name: spec
    for spec in (
        _spec(
            "ingest",
            COMMAND_TARGETS["ingest"],
            COMMAND_OWNERS["ingest"],
            "One hourly snapshot across every source inside its quota.",
            IngestInput,
        ),
        _spec(
            "forecast",
            COMMAND_TARGETS["forecast"],
            COMMAND_OWNERS["forecast"],
            "Run the champion models for a coin and horizon.",
            ForecastInput,
        ),
        _spec(
            "decide",
            COMMAND_TARGETS["decide"],
            COMMAND_OWNERS["decide"],
            "One decision cycle: brief, analysts, proposals, adversary, judge.",
            DecideInput,
        ),
        _spec(
            "paper",
            COMMAND_TARGETS["paper"],
            COMMAND_OWNERS["paper"],
            "Run the hourly loop in paper mode (dry-run only).",
            PaperInput,
        ),
        _spec(
            "review",
            COMMAND_TARGETS["review"],
            COMMAND_OWNERS["review"],
            "Build and optionally send the daily review.",
            ReviewInput,
        ),
        _spec(
            "retrain",
            COMMAND_TARGETS["retrain"],
            COMMAND_OWNERS["retrain"],
            "Retrain or refresh a model under the lab's validation rules.",
            RetrainInput,
        ),
        _spec(
            "backtest",
            COMMAND_TARGETS["backtest"],
            COMMAND_OWNERS["backtest"],
            "Backtest with purged/embargoed validation and the DSR/PBO gate.",
            BacktestInput,
        ),
        _spec(
            "propose-skill",
            COMMAND_TARGETS["propose-skill"],
            COMMAND_OWNERS["propose-skill"],
            "Propose a skill or playbook update for human approval.",
            ProposeSkillInput,
        ),
        _spec(
            "ledger query",
            COMMAND_TARGETS["ledger"],
            COMMAND_OWNERS["ledger"],
            "Query the decision ledger by SQL or by a named query.",
            LedgerQueryInput,
            group="ledger",
            subcommand="query",
        ),
        _spec(
            "worldview get",
            COMMAND_TARGETS["worldview"],
            COMMAND_OWNERS["worldview"],
            "Read the versioned Worldview document or one standing prior.",
            WorldviewGetInput,
            group="worldview",
            subcommand="get",
        ),
        _spec(
            "worldview flag",
            COMMAND_TARGETS["worldview"],
            COMMAND_OWNERS["worldview"],
            "Flag a prior as possibly miscalibrated (priors are never edited).",
            WorldviewFlagInput,
            group="worldview",
            subcommand="flag",
        ),
        _spec(
            "fund request",
            COMMAND_TARGETS["fund"],
            COMMAND_OWNERS["fund"],
            "Raise a Funding Request card instead of silently shrinking.",
            FundRequestInput,
            group="fund",
            subcommand="request",
        ),
    )
}

#: The twelve leaf commands, in the order the plan lists them. This is the parity source of truth:
#: the CLI's top-level help plus these subcommands, and the MCP tool listing, are both derived from it.
COMMAND_NAMES: tuple[str, ...] = tuple(COMMAND_SPECS)

#: The eleven top-level CLI commands.
TOP_LEVEL_COMMANDS: tuple[str, ...] = tuple(
    dict.fromkeys(spec.group for spec in COMMAND_SPECS.values())
)


def mcp_tool_names() -> list[str]:
    """The MCP tool listing: one tool per leaf command, in the same order."""
    return [spec.mcp_tool_name for spec in COMMAND_SPECS.values()]


def cli_command_names() -> list[str]:
    """The CLI's leaf-command listing, in the same order as :func:`mcp_tool_names`."""
    return list(COMMAND_NAMES)


def mcp_name_to_command(name: str) -> str | None:
    """Map an MCP tool name back to its canonical CLI command name."""
    for spec in COMMAND_SPECS.values():
        if spec.mcp_tool_name == name:
            return spec.name
    return None


# --------------------------------------------------------------------------------------
# Call log: one file for Claude, Hermes, the scheduler and the CLI.
# --------------------------------------------------------------------------------------


def call_log_path() -> Path:
    """Where every invocation is recorded. ``AGENTOQUANT_CALL_LOG`` overrides settings.yaml."""
    override = os.environ.get("AGENTOQUANT_CALL_LOG")
    if override:
        return Path(override)
    try:
        relative = load_settings().call_log_path
    except ConfigError:
        relative = "logs/mcp_calls.jsonl"
    path = Path(relative)
    return path if path.is_absolute() else repo_root() / path


def log_call(command: str, output: CommandOutput, *, client: str, source: str) -> None:
    """Append one JSON line to the shared call log. Never raises, never records a secret."""
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "command": command,
        "status": output.status,
        "client": client,
        "source": source,
        "message": output.message,
    }
    try:
        path = call_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # logging must never break a cycle
        print(f"warning: could not write the call log: {type(exc).__name__}", file=sys.stderr)


# --------------------------------------------------------------------------------------
# Lazy dispatch
# --------------------------------------------------------------------------------------


class NotImplementedTarget(Exception):
    """The module or attribute named by the dispatch table does not exist yet."""


def resolve_target(target: str) -> Any:
    """Import ``module:attribute`` lazily.

    Raises :class:`NotImplementedTarget` only when the *target itself* (or a parent package in its
    path) is missing. A ``ModuleNotFoundError`` raised from inside an existing module is a real
    dependency bug and is re-raised rather than disguised as "not implemented".
    """
    module_name, _, attribute = target.partition(":")
    if not module_name or not attribute:
        raise ConfigError(f"malformed dispatch target: {target!r}")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing == module_name or module_name.startswith(missing + "."):
            raise NotImplementedTarget(f"{module_name} does not exist yet") from exc
        raise
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise NotImplementedTarget(f"{module_name}:{attribute} does not exist yet") from exc


def dispatch(spec: CommandSpec, inputs: CommandInput) -> CommandOutput:
    """Resolve the spec's target and call it with the validated input fields as keyword arguments."""
    try:
        target = resolve_target(spec.target)
    except NotImplementedTarget:
        return CommandOutput.stub(spec.name, spec.owner)
    result = target(**inputs.model_dump())
    return _coerce_output(result, spec)


def _coerce_output(result: Any, spec: CommandSpec) -> CommandOutput:
    if result is None:
        return CommandOutput(command=spec.name, status="ok", message="ok")
    if isinstance(result, CommandOutput):
        return result
    if isinstance(result, BaseModel):
        return CommandOutput(
            command=spec.name,
            status="ok",
            message="ok",
            payload=result.model_dump(mode="json"),
        )
    if isinstance(result, dict):
        payload = dict(result)
        message = str(payload.pop("message", "ok"))
        status = payload.pop("status", "ok")
        if status not in ("ok", "not_implemented", "error"):
            status = "ok"
        return CommandOutput(command=spec.name, status=status, message=message, payload=payload)
    return CommandOutput(command=spec.name, status="ok", message=str(result))


def run_command(
    name: str, inputs: CommandInput, *, client: str = "cli", source: str = "cli"
) -> CommandOutput:
    """Validate, dispatch and log one command. Shared by the CLI and the MCP server."""
    spec = COMMAND_SPECS.get(name)
    if spec is None:
        raise ConfigError(f"unknown command: {name!r}")
    output = dispatch(spec, inputs)
    log_call(name, output, client=client, source=source)
    return output


# --------------------------------------------------------------------------------------
# Typer application
# --------------------------------------------------------------------------------------

app = typer.Typer(
    name="agentoquant",
    help="AgentoQuant: agentic hourly crypto trading system (Kraken spot, paper first).",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)
ledger_app = typer.Typer(help="Decision ledger (owned by Phase 0 Task 2).", no_args_is_help=True)
worldview_app = typer.Typer(
    help="Versioned Worldview document and the standing priors (owned by Phase 2).",
    no_args_is_help=True,
)
fund_app = typer.Typer(
    help="Funding requests: the agent never moves funds (owned by Phase 0 Task 5).",
    no_args_is_help=True,
)
app.add_typer(ledger_app, name="ledger")
app.add_typer(worldview_app, name="worldview")
app.add_typer(fund_app, name="fund")


def _validate_config_on_start() -> None:
    """Config is loaded from files and validated on start. Fails closed, with no traceback."""
    try:
        validate_all()
    except ConfigError as exc:
        typer.secho(f"config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_CONFIG) from None


def _emit(output: CommandOutput, *, json_output: bool = False) -> None:
    if json_output:
        typer.echo(json.dumps(output.model_dump(mode="json"), indent=2))
    else:
        stream = sys.stdout if output.status == "not_implemented" else sys.stdout
        print(output.message, file=stream)
        if output.payload:
            print(json.dumps(output.payload, indent=2, ensure_ascii=False), file=stream)


def _invoke(name: str, inputs: CommandInput, *, json_output: bool = False) -> None:
    try:
        output = run_command(name, inputs, client="cli", source="cli")
    except ConfigError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_CONFIG) from None
    except ValidationError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_CONFIG) from None
    _emit(output, json_output=json_output)
    if output.status == "not_implemented":
        raise typer.Exit(code=EXIT_NOT_IMPLEMENTED)
    if output.status == "error":
        raise typer.Exit(code=1)


@app.callback()
def _startup(ctx: typer.Context) -> None:  # noqa: ARG001 - typer passes the context
    """Validate config/*.yaml and the credentials file before any command runs."""
    _validate_config_on_start()


@app.command("ingest", help="One hourly snapshot across every source inside its quota.")
def ingest(
    cycle_id: str = typer.Option(None, "--cycle-id", help="Cycle id, e.g. 2026-09-18T14Z-0001"),
    symbols: list[str] = typer.Option([], "--symbols", help="Empty means the configured universe"),
    force: bool = typer.Option(False, "--force", help="Ignore the cache for this snapshot"),
) -> None:
    _invoke("ingest", IngestInput(cycle_id=cycle_id, symbols=list(symbols), force=force))


@app.command("forecast", help="Run the champion models for a coin and horizon.")
def forecast(
    coin: str = typer.Option(None, "--coin", help="Empty means every coin in the universe"),
    horizon: str = typer.Option("4h", "--horizon", help="4h or 24h"),
    cycle_id: str = typer.Option(None, "--cycle-id"),
) -> None:
    _invoke("forecast", ForecastInput(cycle_id=cycle_id, coin=coin, horizon=horizon))


@app.command("decide", help="One decision cycle: brief, analysts, proposals, adversary, judge.")
def decide(
    cycle_id: str = typer.Option(None, "--cycle-id"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="Never place anything"),
) -> None:
    _invoke("decide", DecideInput(cycle_id=cycle_id, dry_run=dry_run))


@app.command("paper", help="Run the hourly loop in paper mode (dry-run only).")
def paper(
    cycle_id: str = typer.Option(None, "--cycle-id"),
    placeholder: bool = typer.Option(
        False, "--placeholder", help="Phase 0 placeholder decision source instead of the cascade"
    ),
    hours: int = typer.Option(1, "--hours", help="How many hourly cycles to run"),
) -> None:
    _invoke("paper", PaperInput(cycle_id=cycle_id, placeholder=placeholder, hours=hours))


@app.command("review", help="Build and optionally send the daily review.")
def review(
    date: str = typer.Option(None, "--date", help="ISO date, default today in settings timezone"),
    send: bool = typer.Option(False, "--send", help="Deliver the digest to Telegram"),
) -> None:
    _invoke("review", ReviewInput(date=date, send=send))


@app.command("retrain", help="Retrain or refresh a model under the lab's validation rules.")
def retrain(
    coin: str = typer.Option(None, "--coin"),
    reason: str = typer.Option(None, "--reason"),
) -> None:
    _invoke("retrain", RetrainInput(coin=coin, reason=reason))


@app.command("backtest", help="Backtest with purged/embargoed validation and the DSR/PBO gate.")
def backtest(
    strategy: str = typer.Option(None, "--strategy"),
    timerange: str = typer.Option(None, "--timerange", help="e.g. 20260101-20260901"),
    fee_tier: str = typer.Option(None, "--fee-tier", help="Tier id from config/fee_tiers.yaml"),
) -> None:
    _invoke("backtest", BacktestInput(strategy=strategy, timerange=timerange, fee_tier=fee_tier))


@app.command("propose-skill", help="Propose a skill or playbook update for human approval.")
def propose_skill(
    skill: str = typer.Option(None, "--skill", help="Skill name to propose or update"),
    note: str = typer.Option(None, "--note"),
) -> None:
    _invoke("propose-skill", ProposeSkillInput(skill=skill, note=note))


@ledger_app.command("query", help="Query the decision ledger by SQL or by a named query.")
def ledger_query(
    sql: str = typer.Option(None, "--sql", "-s", help="Raw SQL"),
    query: str = typer.Option(None, "--query", "-q", help="A named query shipped by the ledger"),
    param: list[str] = typer.Option([], "--param", "-p", help="Bind parameter as key=value"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table"),
) -> None:
    if (sql is None) == (query is None):
        typer.secho(
            "error: pass exactly one of --sql or --query", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=EXIT_CONFIG)
    params: dict[str, str] = {}
    for item in param:
        key, sep, value = item.partition("=")
        if not sep or not key:
            typer.secho(
                f"error: --param expects key=value, got {item!r}", fg=typer.colors.RED, err=True
            )
            raise typer.Exit(code=EXIT_CONFIG)
        params[key] = value
    _invoke(
        "ledger query",
        LedgerQueryInput(sql=sql, named_query=query, params=params, json_output=json_output),
        json_output=json_output,
    )


@worldview_app.command("get", help="Read the Worldview document or one standing prior.")
def worldview_get(
    prior: str = typer.Option(None, "--prior", help="Empty returns the whole worldview doc"),
    doc_version: str = typer.Option(None, "--doc-version"),
) -> None:
    _invoke("worldview get", WorldviewGetInput(prior_id=prior, doc_version=doc_version))


@worldview_app.command("flag", help="Flag a prior as possibly miscalibrated (never edited).")
def worldview_flag(
    prior: str = typer.Option(..., "--prior", help="The prior being flagged"),
    note: str = typer.Option(..., "--note", help="Why it looks miscalibrated"),
    cycle_id: str = typer.Option(None, "--cycle-id"),
) -> None:
    _invoke("worldview flag", WorldviewFlagInput(prior_id=prior, note=note, cycle_id=cycle_id))


@fund_app.command("request", help="Raise a Funding Request card instead of silently shrinking.")
def fund_request(
    sleeve: str = typer.Option(..., "--sleeve", help="A, B or C"),
    amount: float = typer.Option(..., "--amount"),
    currency: str = typer.Option("CAD", "--currency", help="CAD or USD"),
    reason: str = typer.Option(
        "funding_floor_breach", "--reason", help="funding_floor_breach or unsizeable_high_confidence_card"
    ),
    deadline_hours: int = typer.Option(48, "--deadline-hours"),
) -> None:
    _invoke(
        "fund request",
        FundRequestInput(
            sleeve=sleeve,
            amount=amount,
            currency=currency,
            reason=reason,
            deadline_hours=deadline_hours,
        ),
    )


def main() -> None:
    """Console entry point (``agentoquant``)."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
