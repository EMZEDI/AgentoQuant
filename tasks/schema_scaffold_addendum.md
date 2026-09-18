# Schema and Scaffold Addendum: Crypto Trading Agent

Companion to `plan.md` and `todo.md`, written Sept 18, 2026. Nothing here changes the architecture; it makes the parts of the plan that were only described in prose literal enough that separate implementation calls stay consistent with each other. Read alongside `plan.md` for the why, and `todo.md` for the task-by-task acceptance criteria.

## 0. How to hand this to an implementation agent

- Give the implementer one task, or one phase, per prompt. Attach `plan.md` as background context every time, and attach this document every time too, not just on Task 1. Do not paste the full 28-task list into a single prompt and ask it to build everything at once.
- The file tree in section 1, the enums in section 3 and the record shapes in sections 4 to 6 are literal. Create them verbatim. If a task genuinely cannot follow one of these shapes, the implementer should say so in its output and propose the smallest possible deviation, rather than silently inventing its own shape.
- Every later task imports its enums and record types from the modules named in section 1, never redefines them locally. This is what keeps Task 17's brief builder, Task 18's proposal schema and Task 20's Decision Card agreeing on field names without a human re-reading all three.
- Tasks 4, 5, 6 and 26 (early-signal webhooks and listeners, the Risk Gate and kill switch, live order execution and API keys, and going live) can be drafted by a fast or cheap model like any other task, but the resulting code should be reviewed by Shahrad or re-checked by a stronger model before it runs against a funded account or a real webhook endpoint. This is a cost/risk judgment call, not a capability gap.

## 1. Repo scaffold

One installable Python package. The name below (`ctagent`) is a placeholder, rename it once and keep it consistent everywhere.

```
ctagent/
├── pyproject.toml
├── README.md
├── .env.example
├── uv.lock                        # or requirements.lock; committed from Task 1 onward, see section 2
├── config/
│   ├── settings.yaml               # cadence, capital, cost cap, timezone
│   ├── sources.yaml                 # per-source endpoints, quotas, key refs (never raw keys)
│   ├── worldview.yaml                # versioned Worldview doc + the 5 standing priors (Task 15)
│   ├── fee_tiers.yaml                 # Kraken fee tier table + current tier pointer (Task 3)
│   ├── sleeves.yaml                    # sleeve A/B/C caps and entry confidence thresholds
│   ├── risk_limits.yaml                 # Risk Gate limits (Task 5)
│   └── models.yaml                       # allowed model family per role, harness routing (Task 16)
├── ctagent/
│   ├── __init__.py
│   ├── cli.py                        # command names fixed, see below
│   ├── mcp_server.py                  # exposes the same commands as MCP tools
│   ├── config_loader.py                # loads + validates config/*.yaml, fails closed on a disallowed model
│   ├── enums.py                          # section 3 of this doc, single source of truth
│   ├── ledger/
│   │   ├── __init__.py
│   │   ├── schema.py                     # section 4 record models (pydantic)
│   │   ├── store.py                       # DuckDB read/write API, one table per stage
│   │   └── cost_meter.py                   # tokens/dollars helper, writes llm_call records
│   ├── data/
│   │   ├── connectors/
│   │   │   ├── kraken.py                   # kraken-cli or ccxt
│   │   │   ├── coingecko.py
│   │   │   ├── twelve_data.py
│   │   │   ├── alpha_vantage.py
│   │   │   ├── gold_api.py
│   │   │   ├── gdelt.py
│   │   │   ├── defillama.py
│   │   │   ├── cryptorank.py
│   │   │   └── grok_x_search.py             # via OpenRouter native web plugin
│   │   ├── early_signals/
│   │   │   ├── bybit_listings.py
│   │   │   ├── okx_listings.py
│   │   │   ├── kraken_listings.py            # RSS + AssetPairs polling
│   │   │   ├── onchain_webhooks.py            # Alchemy or Helius
│   │   │   ├── github_releases.py
│   │   │   ├── google_news_rss.py
│   │   │   └── telegram_previews.py
│   │   ├── quota_manager.py
│   │   ├── cache.py
│   │   └── market_store.py                    # unified OHLCVT store, Task 7
│   ├── features/
│   │   ├── technical.py
│   │   ├── macro.py
│   │   ├── usage.py
│   │   ├── regime.py
│   │   └── feature_store.py
│   ├── labels/
│   │   ├── triple_barrier.py
│   │   ├── sample_weights.py
│   │   └── meta_labels.py
│   ├── validation/
│   │   ├── purged_kfold.py
│   │   ├── walk_forward.py
│   │   └── leakage_tests.py
│   ├── verifier/
│   │   ├── source_registry.py                  # source class per feed, Task 11
│   │   ├── corroboration.py
│   │   └── pump_detector.py
│   ├── models/
│   │   ├── registry.py                          # versioned artifacts, Task 12
│   │   ├── train.py
│   │   ├── baseline.py                           # logistic, AutoARIMA, GARCH
│   │   ├── primary.py                             # LightGBM / XGBoost
│   │   ├── bayesian.py                             # NumPyro hierarchical logistic + priors
│   │   ├── calibration.py                           # isotonic / Platt
│   │   ├── intervals.py                              # MAPIE conformal, see section 2 on v1 API
│   │   ├── drift.py                                   # River online monitor
│   │   └── tune.py                                     # Optuna + DSR/PBO gate, Task 14
│   ├── agents/
│   │   ├── harness/
│   │   │   ├── base.py
│   │   │   ├── claude_adapter.py
│   │   │   └── hermes_adapter.py                        # OpenRouter small profile
│   │   ├── schema_validator.py                             # enforces section 5/6 shapes, HOLD fallback
│   │   ├── brief_builder.py                                 # Market Brief, Task 17
│   │   ├── analysts.py                                        # 5 analyst prompts + parallel runner
│   │   ├── proposers.py                                        # 2 proposal samples, Task 18
│   │   ├── adversary.py                                          # Task 19
│   │   ├── judge.py                                               # Task 20
│   │   ├── reviewer.py                                             # Task 22
│   │   └── reflector.py                                             # Task 24
│   ├── skills/                                                        # versioned skill files, git-tracked
│   │   └── README.md
│   ├── risk/
│   │   ├── gate.py                                                     # Task 5
│   │   ├── kill_switch.py
│   │   └── funding_floor.py
│   ├── execution/
│   │   ├── signal_store.py                                              # what freqtrade reads
│   │   ├── freqtrade_strategy.py                                         # thin bridge, force entry/exit
│   │   ├── order_manager.py                                              # ladders, trailing, TP ladder
│   │   └── telegram_bot.py                                               # veto gate, commands, funding requests
│   ├── reports/
│   │   ├── daily_review.py                                                # Task 22
│   │   ├── weekly_review.py                                                # Task 25
│   │   └── templates/
│   └── scheduler.py                                                         # hourly cadence driver
├── freqtrade_user_data/
│   ├── config.json
│   └── strategies/
│       └── AgentBridgeStrategy.py                                            # interface_version = 3
├── tests/
│   ├── test_risk_gate_adversarial.py                                          # Task 5 acceptance tests
│   ├── test_leakage.py                                                         # Task 10
│   ├── test_schema_validation.py                                               # Task 16
│   └── test_ledger_roundtrip.py                                                # Task 2
└── scripts/
    └── backfill_kraken_history.py                                              # Task 7
```

CLI command names, fixed exactly as in `plan.md`: `ingest`, `forecast`, `decide`, `paper`, `review`, `retrain`, `backtest`, `propose-skill`, `ledger query`, `worldview get`, `worldview flag`, `fund request`. Task 1's acceptance criteria requires the MCP tool listing to match this list one for one.

## 2. Pinned dependency versions

Python **3.11.x**. This is the floor: freqtrade, JAX/NumPyro and LightGBM all have mature 3.11 wheels, and 3.11 avoids the wheel-availability gaps that show up on newer Python versions for some of these libraries.

Pin every package below in `pyproject.toml`, then run the project's package manager's lock command (`uv lock`, or `pip freeze` into `requirements.lock`) and commit the lockfile in Task 1. Every later task installs from the lockfile, not from `pyproject.toml` directly, so a package release between Task 1 and Task 28 cannot silently change behavior mid-project.

| Package | Pin | Note |
|---|---|---|
| freqtrade | `>=2026.5,<2027` | Cuts monthly calendar releases; re-check the current tag at Task 6 and update the pin once, then freeze |
| ccxt | `>=4.4,<5` | Releases multiple times a week; do not chase patch versions, just freeze whatever `>=4.4,<5` resolves to at lock time |
| duckdb | `>=1.4.3,<1.5` | Pin to the 1.4 LTS line |
| pyarrow | `>=17,<18` | Parquet I/O alongside DuckDB |
| lightgbm | `>=4.6,<5` | |
| scikit-learn | `>=1.5,<2` | Baselines, calibration |
| statsforecast | `>=1.7,<2` | AutoARIMA baseline |
| arch | `>=7.0,<8` | GARCH baseline |
| numpyro | `>=0.18,<1.0` | Requires `jax` and `jaxlib`; pin both to the same minor as numpyro's own constraint, CPU wheels only, no GPU needed |
| river | `>=0.21,<1.0` | Still pre-1.0; treat any minor bump as a breaking-change risk and re-run tests before moving the pin |
| mapie | `>=1.4,<2` | **API break**: MAPIE 1.x replaced the old `MapieRegressor` class from 0.x with `SplitConformalRegressor`, `CrossConformalRegressor` and related classes. A model trained on older tutorials will reach for the 0.x API; use the 1.x classes only |
| optuna | `>=4.6,<5` | |
| pydantic | `>=2.9,<3` | All ledger, brief and card schemas are pydantic v2 models |
| typer | `>=0.12,<1` | CLI |
| mcp | `>=1.0,<2` | Official MCP Python SDK, for `mcp_server.py` |
| httpx | `>=0.27,<0.28` | Async HTTP for all connectors |
| python-telegram-bot | `>=22.8,<23` | |
| duckdb-engine | pin to match `duckdb` | Only if a SQLAlchemy layer is added; not required by the scaffold above |

`kraken-cli` (the MCP server for Kraken access) is a separate runtime dependency, not a pip package of this repo; pin whatever version is current when Task 3 is implemented and record it in `config/sources.yaml`.

## 3. Canonical enums

Define these once in `ctagent/enums.py` and import them everywhere. Do not let any task redeclare its own version of the action vocabulary, sleeve names or model families.

```python
class Action(str, Enum):
    ENTER_LADDERED = "enter_laddered"
    ADD = "add"
    TRIM = "trim"
    EXIT = "exit"
    ROTATE = "rotate"
    SET_STOP = "set_stop"
    TRAIL_STOP = "trail_stop"
    TAKE_PROFIT_LADDER = "take_profit_ladder"
    EVENT_TRADE = "event_trade"
    REBALANCE = "rebalance"
    HOLD = "hold"
    CANCEL_ORDER = "cancel_order"

# Rejected outright by the schema validator (Task 16) and tested against
# in the Risk Gate adversarial suite (Task 5): grid_trade, scalp,
# hourly_rebalance, market_chase_breakout.

class Sleeve(str, Enum):
    A = "A"   # blue chip and stable
    B = "B"   # AI-serving
    C = "C"   # new / breakout, second exchange only

class ConfidenceBand(str, Enum):
    NO_TRADE = "no_trade"     # confidence < 55
    SMALL = "small"            # 55 to 70
    STANDARD = "standard"        # 70 to 85
    MAX = "max"                    # > 85

class SourceClass(str, Enum):
    EXCHANGE_ANNOUNCEMENT = "exchange_announcement"
    ONCHAIN = "onchain"
    OFFICIAL_HANDLE = "official_handle"
    GITHUB_RELEASE = "github_release"
    HEADLINE = "headline"
    TELEGRAM_PREVIEW = "telegram_preview"
    CORROBORATED_TWO_SOURCE = "corroborated_two_source"

class ModelFamily(str, Enum):
    CLAUDE_SONNET = "claude_sonnet"
    DEEPSEEK = "deepseek"
    KIMI = "kimi"
    GLM = "glm"
    GEMINI_FLASH = "gemini_flash"
    NONE = "none"          # deterministic / non-LLM stage

class Harness(str, Enum):
    CLAUDE_NATIVE = "claude_native"
    HERMES_OPENROUTER = "hermes_openrouter"
    NONE = "none"

class Stage(str, Enum):
    RAW_SNAPSHOT = "raw_snapshot"
    EARLY_SIGNAL = "early_signal"
    VERIFIED_EVENT = "verified_event"
    MODEL_OUTPUT = "model_output"
    ANALYST_REPORT = "analyst_report"
    PROPOSAL_SAMPLE = "proposal_sample"
    ADVERSARY_OBJECTION = "adversary_objection"
    DECISION_CARD = "decision_card"
    RISK_GATE_VERDICT = "risk_gate_verdict"
    HUMAN_ACTION = "human_action"
    EXECUTION = "execution"
    OUTCOME = "outcome"
    LLM_CALL = "llm_call"
    FUNDING_REQUEST = "funding_request"
```

`config_loader.py` validates every entry in `config/models.yaml` against `ModelFamily`; a config referencing anything outside this enum (an Opus-class model, a "big" profile) fails validation at startup, per Task 16's acceptance criteria.

## 4. Ledger records

One physical DuckDB table per `Stage` value (fourteen tables total). Every table shares this envelope as real columns, not a nested blob, so cross-stage joins on `cycle_id` stay cheap:

```python
class LedgerEnvelope(BaseModel):
    record_id: str            # ULID, generated at write time
    cycle_id: str               # e.g. "2026-09-18T14Z-0001"
    stage: Stage
    ts: datetime                  # UTC
    producer_role: str              # e.g. "analyst_technical", "judge", "risk_gate", "human"
    producer_model_family: ModelFamily
    harness: Harness
    schema_version: int = 1
```

Stage-specific payload fields, appended to the envelope columns in each table:

**raw_snapshot**
`source: str` (kraken | coingecko | twelve_data | alpha_vantage | gold_api | gdelt | defillama | cryptorank | grok_x_search), `coin_or_series: str`, `fields: JSON`, `quota_remaining: int`, `is_stale: bool`

**early_signal**
`source_class: SourceClass`, `ticker: Optional[str]`, `event_type: str` (listing | unlock | large_transfer | release | headline | policy), `raw_text_or_ref: str`, `detected_at: datetime`, `latency_seconds_vs_first_article: Optional[int]`

**verified_event**
`source_event_ids: list[str]`, `evidence_quality: float` (0 to 1), `source_class: SourceClass`, `corroboration_count: int`, `is_primary: bool`, `notes: str`

**model_output**
`model_name: str` (lightgbm_primary | logistic_baseline | autoarima | garch | bayesian_hier | river_drift), `coin: str`, `horizon: str` (4h | 24h), `p_up: float`, `interval_low: float`, `interval_high: float`, `interval_method: str` (conformal_enbpi | credible_90 | none), `regime_label: Optional[str]`, `model_version: str`, `training_window: str`

**analyst_report**
`analyst_role: str` (technical | news_policy | social_hype | macro_geopolitics | onchain_usage), `coin: str`, `summary: str`, `stance: str` (bullish | bearish | neutral), `key_evidence_ids: list[str]`, `confidence: float`, `schema_valid: bool`

**proposal_sample**
`family: ModelFamily`, `coin: str`, `action: Action`, `size_pct: float`, `entry_type: str` (post_only_limit | market), `stop: Optional[float]`, `target: Optional[float]`, `horizon: str` (4h | 24h | event), `thesis_bullets: list[str]` (exactly 3), `confidence: float`, `invalidation_condition: str`, `cited_ids: list[str]`

**adversary_objection**
`against_proposal_id: str`, `severity: int` (1 to 5), `objection_text: str`, `own_p_up: float`, `recommended_change: str` (shrink | wait | reject | none), `tool_calls_used: list[str]`

**decision_card** (see section 5 for the full shape and the Telegram render)

**risk_gate_verdict**
`decision_card_id: str`, `verdict: str` (approved | shrunk | rejected), `rule_fired: Optional[str]`, `original_size_pct: float`, `final_size_pct: float`, `funding_floor_breach: bool`

**human_action**
`decision_card_id: Optional[str]`, `command: str` (veto | pause | resume | status | why | flat | fund_approve | fund_decline | auto_execute_timeout), `actor: str`, `responded_at: datetime`, `within_window: bool`

**execution**
`decision_card_id: str`, `order_type: str` (post_only_limit | market | stop_loss | stop_loss_limit), `venue: str` (kraken | bybit | okx), `fill_price: Optional[float]`, `fill_qty: Optional[float]`, `fee_paid: Optional[float]`, `reprice_count: int`, `status: str` (filled | partial | unfilled_timeout | cancelled)

**outcome**
`decision_card_id: str`, `horizon: str` (1h | 4h | 24h | exit), `pnl_pct: Optional[float]`, `pnl_abs: Optional[float]`, `realized_up: Optional[bool]`, `still_open: bool`

**llm_call**
`role: str`, `model_family: ModelFamily`, `harness: Harness`, `tokens_in: int`, `tokens_out: int`, `cost_usd: float`, `latency_ms: int`, `schema_valid: bool`, `fallback_triggered: bool`

**funding_request**
`sleeve: Sleeve`, `amount: float`, `currency: str`, `reason: str` (funding_floor_breach | unsizeable_high_confidence_card), `deadline: datetime`, `status: str` (requested | approved | declined | arrived | expired), `requested_at: datetime`, `responded_at: Optional[datetime]`, `arrived_at: Optional[datetime]`

## 5. Market Brief

What every analyst and both proposers read. Built fresh each cycle by `brief_builder.py`, token-budgeted, and cites ledger record ids so downstream stages can trace every number back to its source.

```python
class MarketBrief(BaseModel):
    cycle_id: str
    generated_at: datetime
    token_budget_max: int
    portfolio: dict            # positions, free_cash_by_sleeve: {A, B, C}, sleeve_weights
    risk_budget: dict           # daily_turnover_used, daily_loss_used_pct, drawdown_used_pct, positions_open, max_positions
    fee_tier: dict                # {tier_name, maker_pct, taker_pct}
    model_outputs: list[dict]      # one entry per coin/horizon, same fields as ledger.model_output
    early_signals: list[dict]       # verified_event summaries from the last N hours
    verified_events: list[dict]
    regime: dict                      # {label, rule_fired}
    worldview_excerpt: dict             # {version, relevant_priors: list[str]}
```

When the brief would exceed `token_budget_max`, drop the lowest-`evidence_quality` verified events first, never truncate `portfolio` or `risk_budget`. Every analyst and proposer prompt is instructed to cite only ids present in the brief; the schema validator rejects any cited id that is not in that cycle's brief.

## 6. Decision Card

The Judge's output (Task 20) and what gets posted to Telegram (Task 21). Same shape in the ledger and in the rendered message, so `/why` can just re-render the stored record.

```python
class DecisionCard(BaseModel):
    cycle_id: str
    selected_proposal_id: Optional[str]     # null means HOLD
    action: Action
    coin: Optional[str]
    sleeve: Optional[Sleeve]
    size_pct: Optional[float]
    confidence: int                           # 0 to 100
    confidence_band: ConfidenceBand
    p_up: float
    interval_low: float
    interval_high: float
    ev_after_fees: float
    fee_tier_assumed: str
    evidence_split: dict                        # {primary: int, verified: int, unverified: int}
    strongest_objection: Optional[dict]           # {severity: int, text: str}
    flip_condition: str
```

Telegram render template (fixed order, so `/why` and the daily audit always show the same layout):

```
[{action}] {coin} · {sleeve} · size {size_pct}%
Confidence: {confidence} ({confidence_band})   P(up): {p_up} [{interval_low}, {interval_high}]
EV after fees ({fee_tier_assumed}): {ev_after_fees}
Evidence: {evidence_split.primary} primary / {evidence_split.verified} verified / {evidence_split.unverified} unverified
Strongest objection (sev {strongest_objection.severity}): {strongest_objection.text}
Flips if: {flip_condition}
Auto-executes in 10 min unless /veto
```

Funding Request card, same rendering convention:

```
Funding request: {sleeve} needs {amount} {currency}
Reason: {reason}
Deadline: {deadline}
Reply /fund approve or /fund decline
```

## 7. What this addendum deliberately leaves open

Exact OpenRouter model IDs per role, the exact Kraken fee tier to assume in the first month of backtests, and whether funding requests get a standing monthly cap are still open questions in `plan.md` and are Shahrad's calls, not the implementer's. Everything else needed to start Task 1 without inventing a shape is above.