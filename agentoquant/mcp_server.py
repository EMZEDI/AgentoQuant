"""MCP server exposing the same commands as the CLI.

Every command in :mod:`agentoquant.cli` is registered here as one MCP tool, in the same order, so
Claude, Hermes, the scheduler and the CLI all call identical code and land in the same call log.

Parity is not maintained by hand: both listings derive from ``cli.COMMAND_SPECS``, and
``tests/test_cli_mcp_parity.py`` asserts the two listings match over a real stdio session.

Stdout belongs to the MCP transport. Anything diagnostic goes to stderr.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from agentoquant import cli
from agentoquant.config_loader import ConfigError, validate_all


#: The MCP tool listing, derived from the CLI's registry so the two cannot drift.
def registered_tool_names() -> list[str]:
    """Tool names this server exposes, in the CLI's order."""
    return cli.mcp_tool_names()


def _startup() -> None:
    """Validate every config file and the credentials file before serving. Fails closed."""
    try:
        validate_all()
    except ConfigError as exc:
        print(f"agentoquant-mcp: config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def _call(command: str, inputs: cli.CommandInput) -> dict[str, Any]:
    """Run one command and return the typed output envelope as JSON-ready data."""
    try:
        output = cli.run_command(command, inputs, client="mcp", source="mcp")
    except ConfigError as exc:
        return cli.CommandOutput(command=command, status="error", message=str(exc)).model_dump(
            mode="json"
        )
    return output.model_dump(mode="json")


mcp = FastMCP("agentoquant")


@mcp.tool(name="ingest", description="One hourly snapshot across every source inside its quota.")
def ingest(
    cycle_id: str | None = None,
    symbols: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    return _call("ingest", cli.IngestInput(cycle_id=cycle_id, symbols=symbols or [], force=force))


@mcp.tool(name="forecast", description="Run the champion models for a coin and horizon.")
def forecast(
    coin: str | None = None,
    horizon: str = "4h",
    cycle_id: str | None = None,
) -> dict[str, Any]:
    return _call("forecast", cli.ForecastInput(cycle_id=cycle_id, coin=coin, horizon=horizon))


@mcp.tool(
    name="decide",
    description="One decision cycle: brief, analysts, proposals, adversary, judge.",
)
def decide(cycle_id: str | None = None, dry_run: bool = True) -> dict[str, Any]:
    return _call("decide", cli.DecideInput(cycle_id=cycle_id, dry_run=dry_run))


@mcp.tool(name="paper", description="Run the hourly loop in paper mode (dry-run only).")
def paper(
    cycle_id: str | None = None,
    placeholder: bool = False,
    hours: int = 1,
) -> dict[str, Any]:
    return _call(
        "paper", cli.PaperInput(cycle_id=cycle_id, placeholder=placeholder, hours=hours)
    )


@mcp.tool(name="review", description="Build and optionally send the daily review.")
def review(date: str | None = None, send: bool = False) -> dict[str, Any]:
    return _call("review", cli.ReviewInput(date=date, send=send))


@mcp.tool(name="retrain", description="Retrain or refresh a model under the lab's validation rules.")
def retrain(coin: str | None = None, reason: str | None = None) -> dict[str, Any]:
    return _call("retrain", cli.RetrainInput(coin=coin, reason=reason))


@mcp.tool(
    name="backtest",
    description="Backtest with purged/embargoed validation and the DSR/PBO gate.",
)
def backtest(
    strategy: str | None = None,
    timerange: str | None = None,
    fee_tier: str | None = None,
) -> dict[str, Any]:
    return _call(
        "backtest", cli.BacktestInput(strategy=strategy, timerange=timerange, fee_tier=fee_tier)
    )


@mcp.tool(name="propose_skill", description="Propose a skill or playbook update for human approval.")
def propose_skill(skill: str | None = None, note: str | None = None) -> dict[str, Any]:
    return _call("propose-skill", cli.ProposeSkillInput(skill=skill, note=note))


@mcp.tool(name="ledger_query", description="Query the decision ledger by SQL or by a named query.")
def ledger_query(
    sql: str | None = None,
    query: str | None = None,
    param: dict[str, str] | None = None,
    json_output: bool = True,
) -> dict[str, Any]:
    return _call(
        "ledger query",
        cli.LedgerQueryInput(
            sql=sql, named_query=query, params=param or {}, json_output=json_output
        ),
    )


@mcp.tool(
    name="worldview_get",
    description="Read the versioned Worldview document or one standing prior.",
)
def worldview_get(prior: str | None = None, doc_version: str | None = None) -> dict[str, Any]:
    return _call(
        "worldview get", cli.WorldviewGetInput(prior_id=prior, doc_version=doc_version)
    )


@mcp.tool(
    name="worldview_flag",
    description="Flag a prior as possibly miscalibrated (priors are never edited).",
)
def worldview_flag(prior: str, note: str, cycle_id: str | None = None) -> dict[str, Any]:
    return _call(
        "worldview flag",
        cli.WorldviewFlagInput(prior_id=prior, note=note, cycle_id=cycle_id),
    )


@mcp.tool(
    name="fund_request",
    description="Raise a Funding Request card instead of silently shrinking.",
)
def fund_request(
    sleeve: str,
    amount: float,
    currency: str = "CAD",
    reason: str = "funding_floor_breach",
    deadline_hours: int = 48,
) -> dict[str, Any]:
    return _call(
        "fund request",
        cli.FundRequestInput(
            sleeve=sleeve,
            amount=amount,
            currency=currency,
            reason=reason,
            deadline_hours=deadline_hours,
        ),
    )


def main() -> None:
    """Console entry point (``agentoquant-mcp``). Serves MCP over stdio."""
    _startup()
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
