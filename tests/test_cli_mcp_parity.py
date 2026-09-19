"""CLI and MCP parity, proven over a real stdio MCP session.

Acceptance criteria (Task 1):
  - "The MCP server lists the same commands as tools and both Claude and Hermes can call one end to end"
Verification:
  - "CLI help and the MCP tool listing match"
  - "Manual check: a hello-world call from Claude and from Hermes lands in the same log"

The Claude/Hermes side is proven at the protocol level: the official MCP SDK client opens a real stdio
session against this server, which is the same transport and the same tool list any MCP client sees.
The call-log test then proves an MCP-client invocation and a CLI invocation land in one shared log.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from typer.testing import CliRunner

from agentoquant import cli
from agentoquant.mcp_server import registered_tool_names

EXIT_NOT_IMPLEMENTED = 3


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "agentoquant.mcp_server"],
        env={**os.environ},
    )


async def _list_tools() -> list[str]:
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [tool.name for tool in result.tools]


async def _call_tool(name: str, arguments: dict) -> dict:
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            if result.structuredContent:
                return result.structuredContent
            return json.loads(result.content[0].text)  # type: ignore[union-attr]


# ----------------------------------------------------------------------------------------------
# The two listings match
# ----------------------------------------------------------------------------------------------


def test_mcp_tool_listing_matches_the_cli_registry() -> None:
    assert registered_tool_names() == cli.mcp_tool_names()


def test_mcp_server_lists_the_same_tools_over_a_real_session() -> None:
    tools = asyncio.run(_list_tools())
    assert tools == cli.mcp_tool_names()


def test_every_plan_command_exists_as_a_leaf_command() -> None:
    expected = {
        "ingest",
        "forecast",
        "decide",
        "paper",
        "review",
        "retrain",
        "backtest",
        "propose-skill",
        "ledger query",
        "worldview get",
        "worldview flag",
        "fund request",
    }
    assert set(cli.cli_command_names()) == expected


def test_cli_help_lists_every_top_level_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    for group in cli.TOP_LEVEL_COMMANDS:
        assert group in result.output, f"{group} missing from `agentoquant --help`"


def test_ledger_and_worldview_expose_their_subcommands() -> None:
    runner = CliRunner()
    ledger = runner.invoke(cli.app, ["ledger", "--help"])
    assert "query" in ledger.output
    worldview = runner.invoke(cli.app, ["worldview", "--help"])
    assert "get" in worldview.output and "flag" in worldview.output


# ----------------------------------------------------------------------------------------------
# A command owned by a later task exits cleanly
# ----------------------------------------------------------------------------------------------


def test_unimplemented_command_exits_nonzero_without_a_traceback(call_log: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["ingest"])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "not implemented yet (owned by Phase 0 Task 3)" in result.output
    assert "Traceback" not in result.output


def test_unimplemented_command_over_mcp_returns_not_implemented(call_log: Path) -> None:
    payload = asyncio.run(_call_tool("ingest", {}))
    assert payload["status"] == "not_implemented"
    assert payload["not_implemented"] is True
    assert "Phase 0 Task 3" in payload["message"]


# ----------------------------------------------------------------------------------------------
# One shared call log for every client
# ----------------------------------------------------------------------------------------------


def _log_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_an_mcp_call_and_a_cli_call_land_in_the_same_log(call_log: Path) -> None:
    asyncio.run(_call_tool("ingest", {}))
    runner = CliRunner()
    result = runner.invoke(cli.app, ["ingest"])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED

    records = _log_lines(call_log)
    clients = {record["client"] for record in records}
    assert clients == {"mcp", "cli"}, records
    assert all(record["command"] == "ingest" for record in records)
    assert all(record["status"] == "not_implemented" for record in records)


def test_the_call_log_never_records_a_secret(call_log: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli.app, ["ingest"])
    raw = call_log.read_text(encoding="utf-8")
    from agentoquant.config_loader import credentials

    for value in credentials().values():
        if len(value) > 8:
            assert value not in raw, "a credential value reached the call log"


@pytest.mark.parametrize("command", ["forecast", "decide", "paper", "review", "retrain", "backtest"])
def test_phase_later_commands_are_stubs_with_an_owner(command: str, call_log: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, [command])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "not implemented yet (owned by" in result.output
