# AgentoQuant

An agentic hourly crypto trading system. Kraken spot, paper-first, human veto via Telegram, one
editable pipeline: config files, versioned artifacts and code that can be changed and re-run.

**Status: Phase 0, paper only.** Nothing here places, modifies or cancels a real order. Live trading
starts at Task 26 at 25 percent of capital with the paper track in shadow. Private Kraken calls are
read-only. No Opus model and no "big" profile is allowed anywhere; a config naming one fails validation
at startup.

Spec: `plan.md` (architecture, hourly loop, standing priors), `tasks/todo.md` (the 28 tasks),
`tasks/schema_scaffold_addendum.md` (file tree, pinned dependencies, canonical enums, record shapes),
`docs/phase0_implementation_plan.md` (Phase 0 waves and frozen interfaces), `.hermes.md` (agent rules).

## Install

Python 3.11 via `uv` (3.11 is the floor: freqtrade, JAX/NumPyro and LightGBM all have mature 3.11
wheels). The lockfile is committed; every task installs from it, not from `pyproject.toml`.

```bash
uv venv --python 3.11
uv lock                 # only when a pin changes
uv sync --frozen --extra dev
source .venv/bin/activate
```

Credentials live **outside the repo**, at `~/.config/agentoquant/credentials.env` (mode 600). The
variable *names* are listed in `config/sources.yaml`; no value is ever printed, logged or committed.
`.env.example` documents the names only.

## Layout

```
agentoquant/
├── cli.py             # the one dispatch table; commands resolve lazily via importlib
├── mcp_server.py      # the same commands as MCP tools, one for one
├── config_loader.py   # config/*.yaml -> pydantic models, fails closed
├── enums.py           # Action, Sleeve, ConfidenceBand, SourceClass, ModelFamily, Harness, Stage
├── ledger/  data/  features/  labels/  validation/  verifier/  models/
├── agents/  skills/  risk/  execution/  reports/  scheduler.py (Task 6)
config/                # settings, sources, worldview, fee_tiers, sleeves, risk_limits, models
tests/
```

Directories exist from Task 1 with their `__init__.py`; the modules inside them are owned by the
later tasks named in `docs/phase0_implementation_plan.md` section 4.

## CLI

```bash
agentoquant --help                      # all eleven top-level commands
agentoquant ingest --symbols BTC ETH    # Task 3 owns the implementation
agentoquant ledger query --sql "select 1"
agentoquant ledger query --query cycle_records --param cycle_id=2026-09-18T14Z-0001 --json
agentoquant worldview get
agentoquant worldview flag --prior ai_compute_demand --note "..."
agentoquant fund request --sleeve B --amount 500 --reason funding_floor_breach
```

Commands whose implementation does not exist yet exit non-zero with
`not implemented yet (owned by Phase 0 Task 3)` and no traceback. The command surface is fixed in
`agentoquant/cli.py`; later tasks add their own module and never edit `cli.py`.

### Dispatch convention (frozen)

`COMMAND_TARGETS` maps a command name to `module:attribute`. On invocation the CLI resolves the target
lazily with `importlib`, validates the command's typed input model, and calls the target with the input
model's fields as keyword arguments:

```python
main(**LedgerQueryInput(sql="select 1", named_query=None, params={}, json=False).model_dump())
```

The target returns a `CommandOutput`, a plain dict with the same fields, or `None` (treated as a
successful empty result). A missing module or attribute is the not-implemented path, never a traceback.

## MCP server

`agentoquant-mcp` serves the same twelve leaf commands (`ingest`, `forecast`, `decide`, `paper`,
`review`, `retrain`, `backtest`, `propose-skill`, `ledger_query`, `worldview_get`, `worldview_flag`,
`fund_request`) over stdio, with the same typed input schemas as JSON Schema, so Claude, Hermes and the
scheduler call identical code. Every call is appended to one JSONL log
(`logs/mcp_calls.jsonl` by default, override with `AGENTOQUANT_CALL_LOG`) together with the calling
client, so a call from Claude, from Hermes and from the CLI land in the same file.
`tests/test_cli_mcp_parity.py` asserts the CLI listing and the MCP tool listing cannot drift apart.

## Tests

```bash
pytest -q                # default: fixtures and mocks only, no live calls
pytest -q -m live        # opt-in: read-only live endpoints (never a Kraken write)
```
