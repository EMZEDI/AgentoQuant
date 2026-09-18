# Task List: Crypto Trading Agent

Abstract-level breakdown, revised Sept 18, 2026 (round 3: two samples on the small profile, cost metering, action vocabulary, early-signal layer, verified sources, second exchange and funding flow, Kraken July 2026 fee tiers). "Components" replaces "files touched" because nothing is implemented yet. Scope sizes follow the planning skill (S: 1 to 2 components, M: 3 to 5, L: 5 to 8).

## Phase 0: Foundation (paper only)

## Task 1: Package skeleton, config, CLI and MCP server stub

**Description:** Create the single installable package with the agreed layout, a CLI with the command names fixed (ingest, forecast, decide, paper, review, retrain, backtest, propose-skill, ledger query, worldview, fund request), and an MCP server that exposes the same commands so Claude, Hermes and the scheduler call identical code. Deployed on the Hermes VM.

**Acceptance criteria:**
- [ ] Every command exists as a stub with a typed input and output schema
- [ ] The MCP server lists the same commands as tools and both Claude and Hermes can call one end to end
- [ ] Config (settings, sources, worldview, fee tier) is loaded from files and validated on start

**Verification:**
- [ ] CLI help and the MCP tool listing match
- [ ] Manual check: a hello-world call from Claude and from Hermes lands in the same log

**Dependencies:** None

**Components:** package layout, CLI, MCP server, config loader

**Estimated scope:** M

## Task 2: Decision ledger, event schema and cost metering in DuckDB

**Description:** The single source of truth every stage writes to: raw snapshots, early signals, verified events, model outputs, analyst reports, proposal samples, objections, Decision Cards, Risk Gate verdicts, human actions (including funding requests), executions, outcomes at +1h, +4h, +24h and exit, and every LLM call with tokens in, tokens out, dollars, role and model family.

**Acceptance criteria:**
- [ ] Every stage has a typed record with a shared cycle id and a producer field (role, model family, harness)
- [ ] Outcome fields fill automatically, including for rejected and vetoed proposals
- [ ] Ledger can be queried by sleeve, regime, signal source, confidence band, agent, model family and cost

**Verification:**
- [ ] Replay a synthetic day and confirm every record is present and linked
- [ ] Query returns hit rate by confidence band, and cost per decision by model family, on the synthetic data
- [ ] Manual check: one cycle's full story, including what it cost, is readable end to end

**Dependencies:** Task 1

**Components:** ledger schema, DuckDB store, write/read API, cost meter

**Estimated scope:** S

## Task 3: Data layer connectors with a rate-limit budget

**Description:** Hourly snapshot across all sources: kraken-cli or ccxt on Kraken (including the account's current fee tier), CoinGecko MCP, Twelve Data, Alpha Vantage, gold-api.com, GDELT, DefiLlama free API, CryptoRank Sandbox API, Grok X Search through OpenRouter's native web plugin. Each source has a call budget and a cache; a missing source degrades gracefully.

**Acceptance criteria:**
- [ ] One hourly snapshot completes inside every source's quota
- [ ] A failed source is recorded as missing, and the cycle continues in hold-only mode when critical data is stale
- [ ] Macro series present (gold, dollar proxy, conflict index), usage series present for VVV and AKT, unlock schedule present for sleeve B pairs

**Verification:**
- [ ] 24-hour dry run with zero quota breaches and a per-source call count and cost report
- [ ] Manual check: snapshot for BTC, one AI coin and one candidate looks sane, and a Grok X Search call returns cited posts for a ticker

**Dependencies:** Task 2

**Components:** connector modules per source, quota manager, cache

**Estimated scope:** M

## Task 4: Early-signal listeners and primary-source feeds

**Description:** Push and fast-poll listeners that run between hourly cycles: Bybit and OKX announcement endpoints, Kraken asset-listings RSS and AssetPairs polling every 10 to 30 seconds, Alchemy or Helius webhooks for large transfers and unlock transactions on sleeve B tokens, GitHub release feeds, Google News RSS per ticker, Telegram announcement channel web previews. Each event lands in the ledger with its source class and timestamp.

**Acceptance criteria:**
- [ ] A new Kraken or Bybit listing appears in the ledger within one minute of the announcement
- [ ] On-chain webhook events arrive and are attributed to a token within two blocks
- [ ] Every early signal carries a source class (exchange, on-chain, official handle, release, headline) for the Verifier

**Verification:**
- [ ] Replay of three past listing days shows the signal ahead of the first article
- [ ] Manual check: one week of listener uptime with restarts logged

**Dependencies:** Task 2

**Components:** listener processes, webhook receiver, RSS pollers

**Estimated scope:** M

## Task 5: Risk Gate rules engine, kill switch and funding floor

**Description:** Deterministic limits that no agent can bypass: per-position and sleeve caps, 3 to 5 concurrent positions, daily turnover cap, daily loss halt, weekly drawdown halt with human restart, minimum liquidity, maximum spread, mandatory stop on the exchange, post-only default, cooldown after consecutive losses, exchange status, the Ontario net-buy cap, and the free-cash floor per sleeve that raises a Funding Request instead of silently shrinking positions.

**Acceptance criteria:**
- [ ] Gate can only shrink or reject a proposal, never enlarge it
- [ ] Kill switch (/flat and daily halt) closes positions and blocks new entries
- [ ] Every verdict, including a funding floor breach, is written to the ledger with the rule that fired

**Verification:**
- [ ] Unit tests with adversarial proposals (oversized, over-concentrated, no stop, taker order, illiquid coin, over the turnover cap, over the net-buy cap) are all rejected or shrunk
- [ ] Manual check: halt and restart sequence works in paper mode

**Dependencies:** Task 2

**Components:** rules engine, limits config, kill switch, funding floor

**Estimated scope:** S

## Task 6: Paper-trading harness, freqtrade signal bridge and order management

**Description:** freqtrade in dry-run on Kraken with a thin strategy that consumes signals from the pipeline's signal store through force entry and exit, plus kraken-cli paper mode. Order management implements the action vocabulary: post-only limit entries repriced at most a few times, laddered entries and partial exits through adjust_trade_position, stops on the exchange (stoploss_on_exchange), trailing stops and custom stoploss ratcheting, take-profit ladders through minimal ROI or custom exit, unfilled timeouts, market orders only for stops and emergency exits, protections enabled.

**Acceptance criteria:**
- [ ] Hourly cycle runs unattended for 3 days in paper mode on the VM with a placeholder strategy
- [ ] Every action in the vocabulary has a working execution path in dry-run, and grid or scalp actions are rejected
- [ ] Orders, fills, fees at the configured tier and LLM cost per cycle are logged with the cycle id

**Verification:**
- [ ] 3-day paper run with no missed cycles and a cost report
- [ ] Manual check: a laddered entry, a trailing stop and a partial exit are visible in the freqtrade log and the ledger

**Dependencies:** Tasks 2, 3, 5

**Components:** freqtrade config, signal store, bridge strategy, order manager, Telegram bot, scheduler

**Estimated scope:** M

## Checkpoint: After Tasks 1 to 6
- [ ] Full loop runs in paper mode for 3 days with every cycle and every token in the ledger
- [ ] Post-only execution and stops on the exchange confirmed in dry-run
- [ ] Cost per cycle measured and inside budget
- [ ] No API quota breaches; early-signal listeners stable
- [ ] Risk Gate blocks every adversarial test proposal
- [ ] Review with Shahrad before proceeding

## Phase 1: Forecasting lab

## Task 7: Kraken history backfill and unified market store

**Description:** Backfill full 1h (and 4h, 1d) OHLCVT history for all sleeve pairs from Kraken's downloadable CSV archives, fill gaps from the Trades endpoint, refresh hourly from the OHLC endpoint (720-candle limit), and pull altcoin history from CoinGecko as a gap filler. One DuckDB and Parquet schema for every source, with survivorship caveats recorded.

**Acceptance criteria:**
- [ ] Every sleeve pair has continuous 1h history with gaps listed, not silently filled
- [ ] Hourly refresh appends without duplicates
- [ ] Quarterly archive updates can be re-run idempotently

**Verification:**
- [ ] Row counts and gap report per pair
- [ ] Manual check: a known event day shows the expected candles

**Dependencies:** Task 3

**Components:** archive loader, trades paginator, market store

**Estimated scope:** M

## Task 8: Feature pipeline with macro, geopolitics, usage and regime

**Description:** Technical features, volume and order book features, GARCH volatility, trend baselines, macro deltas (gold, dollar, conflict index), filtered sentiment and social features, DefiLlama usage deltas, unlock proximity from CryptoRank, AI category strength, fractional differentiation where memory matters, the current fee tier, and a regime classifier.

**Acceptance criteria:**
- [ ] Features computed per coin per hour from the store, with no look-ahead (leakage test passes)
- [ ] Regime label produced each hour with a documented rule or model
- [ ] Feature importance report available for the daily audit

**Verification:**
- [ ] Backfill on historical data and spot-check against known events
- [ ] Manual check: regime labels match intuition on a chart

**Dependencies:** Task 7

**Components:** feature store, regime classifier

**Estimated scope:** M

## Task 9: Triple-barrier labels and meta-labeling

**Description:** Volatility-scaled triple-barrier labels for the 4h and 24h horizons, sample weights by label uniqueness, and the meta-labeling split: a primary model sets the side, a secondary model decides whether to act and sizes the bet.

**Acceptance criteria:**
- [ ] Barriers scale with realized volatility and are documented per sleeve
- [ ] Label overlap and uniqueness weights are computed and stored
- [ ] Meta-label targets are derived from the primary model's out-of-fold sides only

**Verification:**
- [ ] Label distribution report per sleeve and regime
- [ ] Manual check: ten labelled windows reviewed on a chart

**Dependencies:** Task 8

**Components:** labeler, weights, meta-label builder

**Estimated scope:** S

## Task 10: Purged validation harness and leakage tests

**Description:** Purged k-fold with embargo, combinatorial purged CV and walk-forward splits as the only accepted evaluation paths, plus a leakage test suite that fails the build if any feature or split peeks ahead. Random shuffled k-fold is rejected by the harness.

**Acceptance criteria:**
- [ ] A no-signal synthetic target scores at chance under the harness
- [ ] Leakage tests run in CI and fail on a planted look-ahead feature
- [ ] Every model report states the split scheme, embargo and fee tier used

**Verification:**
- [ ] Synthetic-noise test passes; planted-leak test fails as intended
- [ ] Manual check: one model report reviewed for split correctness

**Dependencies:** Task 9

**Components:** validation harness, leakage tests

**Estimated scope:** S

## Task 11: Verifier with primary-source handling

**Description:** Corroboration rules with source classes: exchange announcements and on-chain events are primary evidence with quality near 1.0; an official-handle X post is primary for its own project but needs a price or volume reaction to move sizing; everything else needs two independent sources or a price and volume reaction. Bot and cluster heuristics, pump-and-dump detector, single viral post treated as noise, citations stored for Grok results, output evidence quality 0 to 1.

**Acceptance criteria:**
- [ ] A labelled sample of known pump events and fake headlines is flagged at an agreed recall
- [ ] Primary-source events outrank social chatter in every replayed cycle
- [ ] Sentiment can only confirm, never trigger, a signal downstream

**Verification:**
- [ ] Precision and recall on the labelled sample reported
- [ ] Manual check: three recent listing or unlock episodes reviewed against Verifier output

**Dependencies:** Tasks 3, 4

**Components:** verifier rules, source registry, pump detector

**Estimated scope:** M

## Task 12: Primary and baseline models with calibration and intervals

**Description:** LightGBM or XGBoost as the primary classifier (FreqAI as the runtime where practical), logistic baseline, AutoARIMA and GARCH baselines, River online drift monitor; isotonic or Platt calibration on purged out-of-fold predictions; MAPIE EnbPI conformal intervals. Models are versioned tools callable by the agents.

**Acceptance criteria:**
- [ ] Brier score beats the naive base rate on purged held-out windows
- [ ] Calibration curve within tolerance in each confidence band; conformal coverage near nominal
- [ ] Model artifacts versioned with training window, feature list, split scheme and calibration map

**Verification:**
- [ ] Walk-forward report generated automatically with coverage and calibration plots
- [ ] Manual check: predicted probabilities and intervals on a recent week look plausible

**Dependencies:** Tasks 9, 10

**Components:** training pipeline, calibration, intervals, model registry

**Estimated scope:** M

## Task 13: Bayesian model with Shahrad's priors

**Description:** NumPyro (or PyMC) hierarchical logistic model estimating P(up) with a 90 percent credible interval, with prior means on coefficients that encode the standing priors.

**Acceptance criteria:**
- [ ] Each prior is documented with its coefficient, prior mean and rationale
- [ ] Posterior updates as ledger data grows and reports where data disagrees with a prior
- [ ] Credible interval width flows into position sizing

**Verification:**
- [ ] Prior predictive and posterior predictive checks pass
- [ ] Manual check: a synthetic war escalation scenario widens intervals and lowers speculative P(up)

**Dependencies:** Task 12

**Components:** Bayesian model, priors config, prior audit report

**Estimated scope:** M

## Task 14: Tuning loop with the deflated Sharpe gate

**Description:** Optuna TPE with pruning (50 to 200 trials) whose objective is the mean out-of-sample metric across walk-forward folds after fees at the realistic tier, BoTorch or Ax reserved for expensive joint searches, a trial ledger, and a gate that computes deflated Sharpe and probability of backtest overfitting before any candidate can move to paper trading.

**Acceptance criteria:**
- [ ] Optimizer cannot propose parameters outside the Risk Gate bounds
- [ ] Every trial is logged with its out-of-sample result and the fee tier assumed
- [ ] A candidate is marked Backtested on the promotion board only if DSR is above zero and PBO passes after costs

**Verification:**
- [ ] Optuna study summary saved with best parameters and their out-of-sample results
- [ ] Manual check: best parameters are not degenerate and the gate rejects a planted overfit

**Dependencies:** Tasks 12, 13

**Components:** backtester, objective function, tuning job, DSR/PBO gate

**Estimated scope:** M

## Checkpoint: After Tasks 7 to 14
- [ ] Calibrated probabilities and conformal intervals on purged held-out data
- [ ] Verifier catches labelled pump events and ranks primary sources correctly
- [ ] Positive net-of-fees EV in at least one sleeve at the realistic tier that survives the DSR and PBO gate
- [ ] Review with Shahrad before proceeding

## Phase 2: Agent cascade

## Task 15: Worldview document and regime overlay rules

**Description:** The versioned Worldview document every agent reads plus deterministic regime overlay rules (risk-off caps, policy shock pause, dollar and gold regime mapping).

**Acceptance criteria:**
- [ ] Worldview is versioned in git and injected into every agent prompt
- [ ] Regime rules are unit-tested and logged when they fire
- [ ] The Adversary is explicitly allowed to argue against a prior using data, but cannot edit it

**Verification:**
- [ ] Rule tests pass for each regime scenario
- [ ] Manual check: Shahrad reads the Worldview document and confirms it is his

**Dependencies:** Task 8

**Components:** worldview doc, regime rules

**Estimated scope:** S

## Task 16: Harness layer for Claude and Hermes with schema validation and cost metering

**Description:** Adapters that run a role prompt on a Sonnet-class Claude or on the Hermes small profile via OpenRouter, enforce a JSON schema on every output, fall back to HOLD on malformed output, reject any number not present in the Market Brief, and write tokens, dollars, role and model family to the ledger for every call. No big profile and no Opus anywhere in config.

**Acceptance criteria:**
- [ ] The same role can be run on either harness with one config change
- [ ] Malformed or uncited output never reaches the Judge
- [ ] Cost and model family are logged for every call, and a config referencing a disallowed model fails validation

**Verification:**
- [ ] Fault injection: a malformed sample becomes HOLD and is logged
- [ ] Manual check: one analyst run on two families produces schema-identical reports with cost recorded

**Dependencies:** Tasks 1, 2

**Components:** claude adapter, hermes adapter, schema validator, cost meter

**Estimated scope:** M

## Task 17: Market Brief and the five analysts

**Description:** The Market Brief format (portfolio, risk budget, fee tier, model outputs with intervals, early signals and verified events, regime, worldview) and five independent analysts on the Hermes small profile, each producing a structured report from the same brief with no peer chatter.

**Acceptance criteria:**
- [ ] Brief fits a bounded token budget and cites ledger facts only
- [ ] Each analyst report is schema-validated and stored with its model family and cost
- [ ] Analysts run in parallel and a failed analyst does not block the cycle

**Verification:**
- [ ] 50 historical cycles replayed; all reports parse
- [ ] Manual check: five reports for one cycle read as distinct, useful views

**Dependencies:** Tasks 12, 13, 15, 16

**Components:** brief builder, analyst prompts, parallel runner

**Estimated scope:** M

## Task 18: Two proposal samples with the action vocabulary

**Description:** Two proposal samples per cycle, DeepSeek on the Hermes small profile and a Sonnet-class Claude, each producing per-coin actions from the vocabulary (enter laddered, add, trim, exit, rotate, set or trail stop, take-profit ladder, event trade, hold) with size, entry type, stop, target, horizon, three-bullet thesis, confidence and invalidation condition, citing only brief and analyst content. If both say HOLD, the cycle ends and no further model calls are made.

**Acceptance criteria:**
- [ ] Samples are schema-validated, restricted to the vocabulary, and attributed to a family
- [ ] A cycle where both samples say HOLD ends without Adversary or Judge calls
- [ ] Per-family sample accuracy and cost are measurable from the ledger

**Verification:**
- [ ] Replay on the same 50 cycles; samples parse and reference only available content
- [ ] Manual check: the HOLD short-circuit is exercised and logged, and one sample proposes a laddered entry with a stop

**Dependencies:** Task 17

**Components:** proposer prompt, sampler, action schema, short-circuit rule

**Estimated scope:** M

## Task 19: Adversary agent on the small profile with base-rate tools

**Description:** Separate-context agent on the Hermes small profile (Kimi or GLM), always a different family than the leading sample, with tools for ledger base rates, unlock calendar (CryptoRank), fee calculator at the current tier and upcoming macro events. Produces objections with severity 1 to 5, its own P(up) and a recommended change. One round only.

**Acceptance criteria:**
- [ ] Objections cite evidence or a tool result
- [ ] Adversary accuracy is measurable (objection severity versus realized outcome)
- [ ] The Adversary never sees the proposer's reasoning before forming its own probability

**Verification:**
- [ ] Replay on the same 50 cycles; objections logged and scored
- [ ] Manual check: objections on three known bad trades are convincing

**Dependencies:** Task 18

**Components:** adversary prompt, base-rate tool, calendar tool, fee tool

**Estimated scope:** M

## Task 20: Judge selection, confidence fusion and Decision Card

**Description:** The Sonnet-class Judge picks one proposal or HOLD (selection, not blending), fuses model probability and interval, analyst reports, sample disagreement, Adversary objections, evidence quality and regime fit into a calibrated confidence 0 to 100 and EV after fees at the current tier; sleeve thresholds; quarter-Kelly sizing scaled by confidence, interval width and volatility target; the Decision Card format with the action vocabulary. Agreement is logged but never used as confidence.

**Acceptance criteria:**
- [ ] Confidence mapping is calibrated against realized outcomes and re-fit daily
- [ ] No trade when EV after fees is not positive by margin or a severity-5 objection is unresolved
- [ ] Decision Card always shows action, confidence, interval, EV, evidence split by source class, strongest objection and flip condition

**Verification:**
- [ ] Replay produces a calibration curve on the 50 cycles
- [ ] Manual check: Shahrad reviews ten cards for honesty and readability

**Dependencies:** Task 19

**Components:** selection rule, fusion model, sizing, card renderer

**Estimated scope:** M

## Task 21: Telegram veto gate, commands and funding requests

**Description:** Post the Decision Card to Telegram, auto-execute after a 10-minute window unless vetoed; commands /veto, /pause, /resume, /status, /why, /flat, /fund approve, /fund decline. Funding Request cards (amount, sleeve, reason, deadline) are raised by the Risk Gate's free-cash floor or by a Judge card that cannot be sized; the bot polls balances after approval, confirms arrival, and logs the request, response and arrival time.

**Acceptance criteria:**
- [ ] Veto within the window cancels execution; silence executes
- [ ] /flat closes all positions and pauses entries
- [ ] A funding request is raised, answered and confirmed end to end with every step in the ledger; the bot never initiates a transfer itself

**Verification:**
- [ ] Paper-mode test of every command including a simulated deposit
- [ ] Manual check: card and funding request are readable on a phone

**Dependencies:** Tasks 6, 20

**Components:** Telegram handlers, veto timer, funding request flow

**Estimated scope:** M

## Checkpoint: After Tasks 15 to 21
- [ ] Two weeks of paper trading through the full cascade
- [ ] Calibration report reviewed and cost per cycle within budget
- [ ] Funding request flow exercised once
- [ ] Sample of Decision Cards reviewed with Shahrad

## Phase 3: Learning loops

## Task 22: Daily Reviewer at 10:00 Toronto time with cost accounting

**Description:** Daily audit from the ledger, drafted by the Hermes small profile and signed off by a Sonnet-class Claude, delivered on Telegram at 10:00 America/Toronto: net PnL after fees and after LLM cost, hit rate, Brier and calibration by band, attribution by sleeve, signal source class, analyst, model family and regime, Adversary accuracy, veto value, cost per decision, per executed trade and per HOLD cycle, fee drag, drawdown, prior flags.

**Acceptance criteria:**
- [ ] Report generated automatically every day at 10:00 Toronto time, DST-safe
- [ ] Every metric is reproducible from the ledger
- [ ] Report flags any prior that hurt calibration, any model family that underperformed, and any role whose cost exceeded its benefit

**Verification:**
- [ ] Seven consecutive daily reports produced in paper mode
- [ ] Manual check: numbers reconcile with freqtrade's own PnL and the OpenRouter and Claude usage dashboards

**Dependencies:** Task 21

**Components:** audit job, report template, delivery

**Estimated scope:** M

## Task 23: Champion/challenger retraining and promotion gate

**Description:** Retrain primary and baseline models on the growing ledger under the lab's validation rules, update the Bayesian posterior, keep River updating hourly; promote a challenger only if it beats the champion out of sample on log-loss or Brier and fee-adjusted EV. Weekly Optuna re-tune inside risk bounds with the DSR gate.

**Acceptance criteria:**
- [ ] Promotion decisions logged with the comparison metrics
- [ ] A worse challenger is never promoted
- [ ] Rollback to the previous champion is one action

**Verification:**
- [ ] Inject a deliberately bad challenger and confirm rejection
- [ ] Manual check: one legitimate promotion flows into the next hourly cycle

**Dependencies:** Task 22

**Components:** retraining job, promotion gate, model registry

**Estimated scope:** M

## Task 24: Reflector agent, skill approval flow and promotion board

**Description:** Weekly Reflector on a Sonnet-class Claude that reads embargoed outcomes and the audits, drafts playbook (skill) updates and belief updates for specific analysts with ledger evidence attached, and opens them for approval on Telegram; approved changes merge into the versioned skills directory. The promotion board (Idea, Backtested, Paper, Shadow, Live) is maintained from the same job.

**Acceptance criteria:**
- [ ] Every proposal cites the ledger evidence that motivated it
- [ ] Nothing merges without an explicit approval; the pipeline's tests run on every proposal
- [ ] Approved skills are versioned and visible in the next cycle's prompts; board status changes are logged

**Verification:**
- [ ] One proposal approved and one rejected end to end in paper mode
- [ ] Manual check: a proposed skill reads as a concrete rule, not a vague suggestion

**Dependencies:** Task 22

**Components:** reflector prompt, approval flow, skills repo, board updater

**Estimated scope:** M

## Task 25: Weekly strategy review, value per model family and feasibility gate

**Description:** Weekly report on net EV versus the $100 to $200 target after fees and LLM cost, regime breakdown, live versus paper calibration, value added per model family from counterfactual replay (would the cheap sample alone have made the same calls), the downgrade recommendation for any role whose cost trails its benefit, and a capital scaling recommendation.

**Acceptance criteria:**
- [ ] Daily EV distribution shown against the target, net of LLM cost
- [ ] Clear recommendation: go live, add capital, cut trade count, or lower target; plus keep, downgrade or remove per role
- [ ] Prior audit and cost summarized weekly

**Verification:**
- [ ] Four weekly reports produced during paper trading
- [ ] Manual check: Shahrad can make the go/no-go call and the role decisions from the report alone

**Dependencies:** Tasks 22, 23, 24

**Components:** weekly job, counterfactual replay, report template

**Estimated scope:** M

## Checkpoint: After Tasks 22 to 25 (feasibility gate)
- [ ] Four weeks of paper results with measured daily EV net of fees and LLM cost versus target
- [ ] Calibration holding
- [ ] At least one promoted challenger and one approved skill flowed end to end
- [ ] Value per model family reported
- [ ] Go/no-go decision with Shahrad

## Phase 4: Go-live and scaling

## Task 26: Live trading at 25 percent of capital with a shadow paper track

**Description:** Switch freqtrade to live on Kraken with 25 percent of capital, tightened limits, trade-only API keys with withdrawals disabled, the paper track still running in shadow, and weekly comparison of live versus paper calibration and fills (post-only fill rate, repricing count).

**Acceptance criteria:**
- [ ] Live limits are stricter than paper limits
- [ ] Keys have no withdrawal permission
- [ ] Scaling to full capital requires two profitable weeks with live calibration matching paper

**Verification:**
- [ ] First live week reconciles with exchange statements
- [ ] Manual check: /flat tested live with a tiny position; a live funding request round-trips

**Dependencies:** Checkpoint 3 go decision

**Components:** live config, key management, shadow runner, reconciliation

**Estimated scope:** S

## Task 27: Second exchange onboarding (UAE residency) and speculative sleeve activation

**Description:** Only after sleeves A and B prove positive: open Bybit (SCA entity, residency outside Dubai) or OKX UAE (Dubai) with trade-only keys, run its spot demo through the same signal bridge, then enable Sleeve C with the listings screen, usage filter, liquidity floor, unlock checks, 5 percent position cap, entry confidence 75 or higher, and manual profit sweeps back to Kraken through funding request cards.

**Acceptance criteria:**
- [ ] Candidates fail closed when usage or liquidity data is missing
- [ ] Sleeve cap and position cap enforced by the Risk Gate on the new venue
- [ ] Sweep proposals go through the funding flow; the bot never withdraws

**Verification:**
- [ ] Two weeks of Sleeve C on the exchange demo before live
- [ ] Manual check: three candidates walked through the screen by hand

**Dependencies:** Task 26

**Components:** second exchange config, listings screen, sleeve config, sweep proposals

**Estimated scope:** M

## Task 28: Optional upgrades

**Description:** Multi-model Judge via the council-decision skill, LunarCrush or other paid sentiment tier if the sentiment layer earns it, neural forecasters as challengers, BoTorch or Ax for joint multi-asset tuning, and a whitelisted sweep address with a 24-hour lock if manual sweeps become a bottleneck.

**Acceptance criteria:**
- [ ] Each upgrade is evaluated as a challenger against the current system through the promotion board
- [ ] Paid tools and extra model calls are kept only if the weekly report shows a measurable gain net of their cost

**Verification:**
- [ ] Before and after comparison in the weekly report
- [ ] Manual check: cost versus gain reviewed with Shahrad

**Dependencies:** Task 27

**Components:** varies by upgrade

**Estimated scope:** M

## Checkpoint: Complete
- [ ] Two profitable weeks at 25 percent of capital with live calibration matching paper
- [ ] All hard limits verified live
- [ ] Scale-up decision made with Shahrad