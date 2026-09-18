# Phase 0 implementation plan — Tasks 1 to 6 (foundation, paper only)

Written 2026-09-18 by the Phase 0 agent for Shahrad's review and for the task agents that execute it.
Companion to `plan.md`, `tasks/todo.md`, `tasks/schema_scaffold_addendum.md` and `docs/HANDOFF.md`.

Where this document and the addendum disagree, **the addendum wins** — except for section 2 below, which
records decisions Shahrad has already made. Where this document is silent, the addendum is literal.

## 1. Objective and exit criteria

Phase 0 builds the shell everything else lives in: one installable package with a CLI and an MCP server
exposing the same commands, a DuckDB ledger that records every stage of every cycle and every token
spent, the data layer with a per-source quota budget, the early-signal listeners, the deterministic Risk
Gate, and a paper-trading harness that runs the hourly loop on Kraken in dry-run.

Exit criteria are the **Foundation checkpoint** in `tasks/todo.md` (evidence plan in section 9).

Explicitly out of scope: any forecasting model (Phase 1), any LLM cascade (Phase 2), any live order
(Task 26). Phase 0 ships a **placeholder decision source** so the loop can run end to end; Phase 2
replaces it with the real cascade. Nothing in Phase 0 may call `AddOrder`, `CancelOrder`, `AmendOrder`,
`Withdraw*` or any transfer endpoint.

## 2. Decisions already recorded — apply, do not re-litigate

| # | Decision | Consequence for Phase 0 |
|---|---|---|
| 1 | Package name is `agentoquant`. Every `ctagent` in the addendum reads as `agentoquant`. | Directory `agentoquant/`, all imports `from agentoquant...` |
| 2 | Sonnet-class roles run on OpenRouter `anthropic/claude-sonnet-5` with the project key; the addendum's `Harness` enum records the model **tier**, not the transport. | No effect in Phase 0 (no LLM roles yet); `config/models.yaml` is written so Task 16 needs no schema change |
| 3 | The Hermes gateway owns the Telegram **inbound** path (one poller per bot token). | `execution/telegram_bot.py` is outbound-only. It must never call `getUpdates` or poll. Inbound relay is Task 21 |
| 4 | Kraken live fee tier is **Tier 1** (maker 0.4000%); Tier 2 (0.30/0.60) is the optimistic backtest case. | `config/fee_tiers.yaml` carries the full table plus a `current_tier` pointer; the code prefers the live tier read from `TradeVolume` |
| 5 | CryptoRank's free plan has **no unlock/vesting endpoints** (Pro only). | Unlock data is best-effort and its absence **fails closed** for sleeve C. Task 3 records `is_stale`/missing rather than inventing a source |
| 6 | Kraken private-API quirks: ledger endpoint is `/0/private/Ledgers`; `TradeVolume` returns fees under `fees_maker`; `OpenOrders`/`ClosedOrders` nest rows under `open`/`closed`. | Task 3's Kraken connector must handle all three |
| 7 | GDELT returns 429 from this IP with a 5-second limit and 10–13 s latency. | Confirmation layer only, with backoff and caching; never a trigger, never on the critical path |
| 8 | Paper/dry-run only until Task 26; the agent never moves funds; secrets live in `~/.config/agentoquant/credentials.env` and are never printed. | Every task's tests run against fixtures, mocks or the paper venue. A live-key call is allowed only for read-only private endpoints in an opt-in `-m live` test |

## 3. Wave plan

Phase 0 has hard dependencies; full fan-out would create conflicts for no gain. Four waves, each merged
to `main` before the next starts.

| Wave | Task | Parallelism | Why this order |
|---|---|---|---|
| 1 | Task 1 — package skeleton, config, CLI, MCP stub | serial (1 agent) | every other task imports its enums, config loader and CLI dispatch table |
| 2 | Task 2 — ledger schema, DuckDB store, cost meter | serial (1 agent) | Tasks 3, 4, 5 and 6 all write records into it; it owns the 14 stage payload shapes |
| 3 | Tasks 3, 4, 5 — data connectors / early-signal listeners / Risk Gate | **3 agents in parallel**, one worktree each | file-disjoint (see section 4); Task 5 is pure logic with no data dependency |
| 4 | Task 6 — freqtrade bridge, paper harness, order manager, Telegram, scheduler | serial (1 agent) | needs Task 3 (data) and Task 5 (gate) merged |

Each wave is gated on the previous wave being merged and green. A task agent that finds it needs a file
it does not own writes the requirement into its report instead of editing — the owning task or the
phase agent makes that change.

## 4. File ownership matrix

Every path below belongs to exactly one task. `agentoquant/` replaces the addendum's `ctagent/` per
decision 1. Task 1 creates the full directory skeleton **including every `__init__.py`**, so later tasks
only add modules to directories that already exist.

| Owner | Paths |
|---|---|
| **Task 1** | `pyproject.toml`, `uv.lock`, `.python-version`, `.env.example`, `README.md`, `agentoquant/__init__.py`, `agentoquant/cli.py`, `agentoquant/mcp_server.py`, `agentoquant/config_loader.py`, `agentoquant/enums.py`, `config/*.yaml` (all seven), `tests/__init__.py`, `tests/conftest.py`, `tests/test_config_loader.py`, `tests/test_cli_mcp_parity.py`, and every `__init__.py` in the tree |
| **Task 2** | `agentoquant/ledger/{__init__,schema,store,cost_meter,cli}.py`, `tests/test_ledger_roundtrip.py` |
| **Task 3** | `agentoquant/data/connectors/{kraken,coingecko,twelve_data,alpha_vantage,gold_api,gdelt,defillama,cryptorank,grok_x_search}.py`, `agentoquant/data/{quota_manager,cache,ingest}.py`, `tests/test_connectors.py` |
| **Task 4** | `agentoquant/data/early_signals/{bybit_listings,okx_listings,kraken_listings,onchain_webhooks,github_releases,google_news_rss,telegram_previews,runner}.py`, `tests/test_early_signals.py` |
| **Task 5** | `agentoquant/risk/{__init__,gate,kill_switch,funding_floor}.py`, `tests/test_risk_gate_adversarial.py` |
| **Task 6** | `agentoquant/execution/{__init__,signal_store,freqtrade_strategy,order_manager,telegram_bot,paper}.py`, `agentoquant/scheduler.py`, `freqtrade_user_data/config.json`, `freqtrade_user_data/strategies/AgentBridgeStrategy.py`, `tests/test_execution_bridge.py`, `tests/test_paper_loop.py`, `deploy/*.service`, `deploy/*.timer` |

**Do not touch (any task except its owner):** `agentoquant/enums.py`, `agentoquant/config_loader.py`,
`agentoquant/cli.py`, `agentoquant/mcp_server.py`, `agentoquant/ledger/**`, `pyproject.toml`, `uv.lock`,
`config/*.yaml`. If a task needs a change in one of these, it says so in its report.

**Not created in Phase 0** (reserved for later tasks, do not pre-create): `agentoquant/data/market_store.py`
(Task 7), `agentoquant/features/**`, `agentoquant/labels/**`, `agentoquant/validation/**`,
`agentoquant/verifier/**`, `agentoquant/models/**`, `agentoquant/agents/**`, `agentoquant/reports/**`,
`agentoquant/skills/**`, and the named test files owned by other tasks.

## 5. Frozen interface contracts

These are the seams that let wave 3 run three agents in parallel without conflicts. They are frozen in
Task 1 (CLI/config) and Task 2 (records/store); wave 3 and 4 consume them and must not change them.

### 5.1 `agentoquant/enums.py` (Task 1)

Verbatim from addendum section 3: `Action`, `Sleeve`, `ConfidenceBand`, `SourceClass`, `ModelFamily`,
`Harness`, `Stage`. No additions, no local redefinitions anywhere.

### 5.2 `agentoquant/config_loader.py` (Task 1)

```python
class ConfigError(Exception): ...
class DisallowedModelError(ConfigError): ...   # config names an Opus-class or "big" profile model

def load_settings() -> Settings          # config/settings.yaml      -> pydantic model
def load_sources() -> Sources            # config/sources.yaml       -> pydantic model
def load_models() -> Models              # config/models.yaml        -> validated against ModelFamily
def load_risk_limits() -> RiskLimits     # config/risk_limits.yaml
def load_sleeves() -> Sleeves            # config/sleeves.yaml
def load_fee_tiers() -> FeeTiers         # config/fee_tiers.yaml
def load_worldview() -> Worldview        # config/worldview.yaml
def credentials() -> dict[str, str]      # ~/.config/agentoquant/credentials.env; never logs values
def repo_root() -> Path
```

Validation fails closed at import of the config: a disallowed model family, a missing required key, or a
missing credentials file raises at startup with a message naming the offending key.

### 5.3 CLI and MCP dispatch (Task 1)

`cli.py` holds one table and dispatches lazily, so a task can implement its command by adding its own
module and never editing `cli.py`:

```python
COMMAND_TARGETS = {
    "ingest":        "agentoquant.data.ingest:main",          # Task 3
    "forecast":      "agentoquant.models.forecast:main",      # Phase 1
    "decide":        "agentoquant.agents.decide:main",        # Phase 2
    "paper":         "agentoquant.execution.paper:main",      # Task 6
    "review":        "agentoquant.reports.daily_review:main", # Phase 3
    "retrain":       "agentoquant.models.train:main",         # Phase 1
    "backtest":      "agentoquant.validation.backtest:main",  # Phase 1
    "propose-skill": "agentoquant.skills.propose:main",       # Phase 3
    "ledger":        "agentoquant.ledger.cli:main",           # Task 2  (`ledger query`)
    "worldview":     "agentoquant.worldview_cli:main",        # Phase 2 (`worldview get|flag`)
    "fund":          "agentoquant.risk.funding_floor:cli",    # Task 5  (`fund request`)
}
```

A target that does not exist yet exits with `not implemented yet (owned by <phase>)` and a non-zero
status — it must not raise an import traceback. `mcp_server.py` exposes exactly this list as tools, one
for one; `tests/test_cli_mcp_parity.py` asserts the two listings are identical, which is Task 1's
acceptance criterion.

### 5.4 `agentoquant/ledger/schema.py` (Task 2)

All fourteen stage payload models from addendum section 4, plus the shared `LedgerEnvelope`, plus
`MarketBrief` (addendum section 5) and `DecisionCard` (addendum section 6). Decision: `MarketBrief` and
`DecisionCard` live here rather than in `agents/`, because they are ledger-recorded shapes that Tasks 5,
6, 17, 20 and 21 all import — putting them in a Phase 2 module would force Phase 0 to import Phase 2
code. This is the smallest deviation that keeps the addendum's field names literal.

### 5.5 `agentoquant/ledger/store.py` and `cost_meter.py` (Task 2)

```python
class LedgerStore:
    def __init__(self, db_path: Path | str | None = None): ...
    def migrate(self) -> None                                    # creates the 14 tables
    def write(self, stage: Stage, cycle_id: str, payload: BaseModel, *,
              producer_role: str, producer_model_family: ModelFamily = ModelFamily.NONE,
              harness: Harness = Harness.NONE) -> str            # returns record_id (ULID)
    def query(self, sql: str, params: Sequence | None = None) -> list[dict]
    def cycle_records(self, cycle_id: str) -> list[dict]          # one cycle's full story
    def cost_by(self, dimension: Literal["cycle","family","role","day"]) -> list[dict]
    def close(self) -> None

class CostMeter:
    def record(self, *, cycle_id: str, role: str, model_family: ModelFamily, harness: Harness,
               tokens_in: int, tokens_out: int, cost_usd: float, latency_ms: int,
               schema_valid: bool, fallback_triggered: bool) -> str
    @staticmethod
    def cost_from_response(response: dict) -> float    # reads OpenRouter usage.cost, no estimation
    def cost_per_cycle(self, cycle_id: str) -> float
```

`LedgerStore.write` is generic and takes any of the fourteen payload models — that is why wave 3's three
tasks never need to edit `ledger/**`. Each stage writes to its own table; every table carries the
envelope columns as real columns so cross-stage joins on `cycle_id` stay cheap.

### 5.6 `agentoquant/execution/signal_store.py` (Task 6, consumed by Task 6's strategy only)

```python
class SignalStore:
    def publish(self, card: DecisionCard, verdict: RiskGateVerdict, *, cycle_id: str) -> Path  # atomic
    def latest(self) -> dict | None
    def ack(self, cycle_id: str, result: dict) -> None   # strategy -> pipeline, same store
```

Atomic write (temp file + rename). The freqtrade strategy polls this directory only — it never imports
the agent package and never talks to an LLM.

### 5.7 `agentoquant/risk/gate.py` (Task 5)

```python
class RiskGate:
    def __init__(self, limits: RiskLimits, ledger: LedgerStore | None = None): ...
    def evaluate(self, card: DecisionCard, context: PortfolioContext) -> RiskGateVerdict
```

`RiskGateVerdict` is the addendum's `risk_gate_verdict` payload (`approved|shrunk|rejected`,
`rule_fired`, `original_size_pct`, `final_size_pct`, `funding_floor_breach`). The gate returns it; it
never mutates the card. `final_size_pct <= original_size_pct` always — asserted in tests.

## 6. Per-task notes

- **Task 1.** Create the whole skeleton with every `__init__.py`; write all seven config files with the
  values from `plan.md`'s hard-limits section; create the venv with `uv venv --python 3.11`, lock with
  `uv lock`, and commit the lockfile. `pytest -q` must pass with the two Task 1 test files. **Known
  risk:** `freqtrade` + the ML stack (`numpyro`/`jax`, `lightgbm`, `mapie`) in one lockfile may not
  resolve together. If `uv lock` fails, do **not** drop a pin — report the exact conflict with both
  resolver messages and propose the smallest deviation (extras split, or freqtrade in its own venv).
- **Task 2.** Fourteen tables, envelope columns real, `test_ledger_roundtrip.py` replays a synthetic day
  and asserts every record is present and linked, plus hit-rate-by-confidence-band and
  cost-per-decision-by-family queries. One opt-in `-m live` test makes a single real OpenRouter call
  through the project key to prove `usage.cost` capture — skipped by default so CI costs nothing.
- **Task 3.** One hourly snapshot inside every source's quota, cache-backed; a failed source is recorded
  as missing and the cycle degrades to hold-only when critical data is stale. Respect decision 5
  (unlocks best-effort, fail closed) and decision 6 (Kraken quirks). Grok X Search goes through the
  project's OpenRouter key with `x-ai/grok-4.3` + `plugins:[{"id":"web"}]`; citations are stored.
- **Task 4.** Push/fast-poll listeners between cycles; every event lands with its `SourceClass` and
  timestamp so the Verifier (Task 11) can rank it. Kraken listing RSS + AssetPairs poll every 10–30 s.
  Webhook receivers must verify signatures and fail closed. No MTProto.
- **Task 5.** Deterministic, no LLM, no I/O beyond the ledger. `test_risk_gate_adversarial.py` must
  reject or shrink every adversarial proposal in the task's list (oversized, over-concentrated, no stop,
  taker order, illiquid, over turnover cap, over the Ontario net-buy cap) and prove the gate can never
  enlarge. The funding floor raises a `funding_request` record instead of silently shrinking.
- **Task 6.** freqtrade dry-run on Kraken with the thin bridge strategy; every action in the vocabulary
  has a working dry-run path and grid/scalp actions are rejected. Placeholder decision source for the
  Phase 0 loop (`--placeholder`), replaced in Phase 2. Telegram is outbound-only (decision 3). Scheduler
  is a systemd **user** unit (`deploy/agentoquant-hourly.{service,timer}`, linger is on) — chosen over
  Hermes cron because it survives without an LLM in the path; if you disagree, say so in the report.

## 7. Verification gates per wave

| Wave | Gate before merge |
|---|---|
| 1 | `uv lock` succeeds and is committed; `pytest -q` green; `agentoquant --help` lists all eleven commands; CLI listing == MCP tool listing; `agentoquant ingest` exits non-zero with "not implemented yet" and no traceback |
| 2 | `pytest -q` green including `test_ledger_roundtrip.py`; 14 tables exist in the DuckDB file; a synthetic day replays with every record linked by `cycle_id`; `agentoquant ledger query` prints a real table |
| 3 | `pytest -q` green including `test_risk_gate_adversarial.py`; each of the three branches merges cleanly into `main` with no file overlap; a live read-only Kraken smoke call returns the live fee tier |
| 4 | `pytest -q` green; the hourly loop runs unattended in dry-run; one laddered entry, one trailing stop and one partial exit are visible in both the freqtrade log and the ledger |

Phase agent (me) reviews every diff against the plan, re-runs the full suite on the merged `main`, and
checks hygiene (no secrets, no `ctagent` leftovers, no out-of-scope files, em-dash-free reports) before
committing. Subagent self-reports are treated as claims, not evidence.

## 8. Risks and contingencies

| Risk | Handling |
|---|---|
| `freqtrade` + ML stack lockfile conflict | Task 1 reports the exact conflict and proposes an extras split or a separate freqtrade venv. Never drop a pin silently |
| freqtrade install is heavy (~500 MB, minutes) | One venv per worktree via `uv sync --frozen` from the warm cache; budget for it in wave 3 and 4 |
| No swap on the box; 7.6 GB RAM | Phase 0 is light; Optuna/JAX tuning (Phase 1) needs the 4 GB swap Shahrad was asked to add. Flagged, not blocking |
| Live keys present in the environment | Tests default to fixtures/mocks; the only live paths are read-only Kraken reads and the single opt-in OpenRouter smoke call |
| 3-day paper run cannot complete inside one session | The loop starts after Task 6 merges and runs as a soak; the checkpoint is reported with the first N hours of real evidence plus the running job's status, and re-reported when the 72 hours complete |
| GDELT 429 / source outages | Quota manager, cache and backoff; degraded mode is a designed state, not an error |

## 9. Checkpoint evidence plan (Foundation)

| Checkpoint line | Evidence to collect |
|---|---|
| Full loop runs in paper mode for 3 days with post-only orders and stops on the exchange | systemd timer uptime, cycle count from the ledger, freqtrade dry-run log showing post-only limit orders and a stop on the exchange |
| Every cycle and every token is in the ledger | `ledger query` output: cycles, per-stage record counts, zero orphan `cycle_id`s |
| Cost per cycle measured | `cost_by("cycle")` over the run + per-source call counts from the quota manager |
| No API quota breaches | quota manager report over the run window |
| Risk Gate blocks every adversarial proposal | `pytest tests/test_risk_gate_adversarial.py -v` output |
| Review with Shahrad | this document + the wave reports |

## 10. Subagent roles and count

Six task agents (one per task; wave 3 runs three concurrently) plus two independent critics at the end —
one that writes and runs an adversarial test suite against the merged `main`, one that audits the
implementation against the spec for drift and missing acceptance criteria. Both critics report to
Shahrad through the phase agent. The phase agent (this session) owns the plan, the merges, the
checkpoint and the final report; it does not implement tasks.
