# Implementation Plan: Crypto Trading Agent (hourly, human in the loop)

Status: architecture and phase plan only. Nothing is implemented yet. Companion task list in tasks/todo.md. The visual version lives on the Miro board "Crypto Trading Agent: Decision Pipeline" (sections 1 to 17). Revised Sept 18, 2026 (round 3) from Shahrad's sticky notes and answers: two proposal samples on the small profile, token cost accounting, a fuller action vocabulary, an early-signal layer ahead of news articles, Grok X Search through OpenRouter, verified usage and unlock sources, the second-exchange decision, the funding flow, and Kraken's July 2026 fee tiers.

## Overview

A continuously running trading system that makes one decision cycle per hour across a concentrated book of 3 to 5 crypto positions on Kraken (blue chip and AI-serving coins), with a second centralized exchange added later for early-stage coins once sleeves A and B have proven positive. Each hourly cycle ingests prices, macro, geopolitics, primary-source early signals, news, social and usage data, filters out fake and manufactured hype, runs quantitative models that output calibrated probabilities with uncertainty, interprets everything through Shahrad's standing worldview, and then runs a layered agent cascade: five independent analysts in parallel on cheap models, two candidate proposals from two model families, one adversarial critique round on a cheap model, and one Sonnet-class Judge that selects one proposal and scores its confidence from calibration. The Decision Card speaks a full action vocabulary (laddered entries, adds, trims, trailing and rolling stops, take-profit ladders, rotation, event trades), not just buy and sell. A deterministic Risk Gate and a Telegram veto window sit between the Judge and the exchange. Every input, argument, decision, outcome and every token spent is written to a ledger, and a daily loop uses that ledger to retrain models, propose new skills, audit calibration and report whether the system is making money net of fees and net of LLM cost. Everything runs in paper mode first, and the whole pipeline is packaged as one editable tool.

## Goals and constraints (as stated by Shahrad)

- Hourly cadence, not high frequency. Human verification in the loop via Telegram (veto window). Daily review delivered at 10:00 Toronto time, read by Shahrad himself.
- Target at least $100 to $200 per day. This is a floor to size the project against, not a cap: the objective is risk-adjusted expected value, and larger upside is welcome when the evidence supports it.
- Universe covers three types: blue chip and stable (BTC, ETH, SOL, stablecoins as cash), AI-serving coins (VVV, DGRID-type projects, and similar), and new coins with breakout potential. The speculative sleeve opens only after sleeves A and B prove positive.
- Starting capital under $10k. Kraken spot keys exist (spot only, no shorting: sell means rotate to stablecoins). A second CEX comes later, opened with UAE residency.
- Online self-improvement: the system learns from its own decisions by creating and updating skills and model tools (XGBoost, linear, Bayesian) that feed future decisions.
- Daily analysis of decisions to check whether things are actually working, including whether each model's tokens are worth what they cost.
- An Adversary agent attacks the trading agent's decision; an aggregator (Judge) finalizes it.
- Concentrated but not reckless: several positions, not too many, because the point is gains.
- A deliberately biased perspective: news is treated as true, but a set of standing priors (below) always shapes interpretation.
- Use many sub-agents including Hermes Agent, on the small (cheap) profile; two proposal samples, not three; never the most expensive Claude model.
- Cascade and accumulate agents the way prior successful trading-agent work did, with Deep Think style parallel sampling and selection.
- Paper trading for all predictions before any money moves.
- Proper use of XGBoost and other learning and optimization methods on historical data scraped from Kraken and other sources.
- Trades are more than buy and sell: the system must handle the different types of trading (ladders, limits, rolling stops, and so on).
- Pool signals faster than journalists write articles, without depending on slow news APIs.
- The pipeline is a reusable, editable tool: files, models, config and code that can be changed and re-run.

## Standing priors (the bias layer)

These are Shahrad's and are encoded into the system, not learned from data. The system may report when a prior hurts calibration; it never edits one.

1. AI compute demand keeps compounding: AI-serving and inference infrastructure tokens have a structural tailwind beyond short-term hype.
2. BTC and ETH are the reserve layer: keep a core allocation, rotate speculative profits back into it.
3. Usage beats narrative: only trade hype that has real product usage, on-chain activity or revenue behind it; pure meme momentum is noise unless treated as a short-horizon trade.
4. Policy news moves everything: US and UAE regulatory and macro news dominate short-term direction over technicals.
5. Wars, gold and the dollar set the regime: Middle East and other conflicts, the gold price and the value of the US dollar are standing regime signals. Escalation with gold and the dollar rising together reads as risk-off for crypto beta.

Each prior is encoded three ways: as a numeric prior in the Bayesian model (a prior mean on the relevant coefficient), as a deterministic regime rule the Risk Gate and Judge respect, and as a versioned Worldview document every agent reads.

## Reality check on the target (updated for Kraken's July 2026 fee tiers)

Under $10k of capital, $100 to $200 per day is 1 to 2 percent per day or more. Kraken moved to cross-platform fee tiers on July 9, 2026: spot Tier 1 is 0.40 percent maker and 0.80 percent taker, falling to 0.30 and 0.60 above $2.5k of 30-day volume, 0.22 and 0.38 above $10k, and 0.20 and 0.35 above $25k (the tier also counts assets held on the platform); stablecoin and FX pairs are 0.20 flat. A taker round trip at Tier 1 costs 1.6 percent, which makes post-only maker execution mandatory rather than a preference, and every trade still needs an expected move well above the maker round trip at the tier the book is actually on. The published evidence is sobering: on hourly BTC from 2018 to 2026 with 27 walk-forward folds, gradient boosting edged neural models but nothing beat buy and hold after costs and bootstrap, and a real-money contest between six frontier LLMs in late 2025 ended with fees dominating PnL and win rates of 25 to 30 percent. Realistic directional accuracy for a model like ours is 52 to 56 percent. The edge, if there is one, has to come from trade selection, cost awareness, regime filters and sizing, not from raw prediction accuracy. The plan handles this honestly: the system optimizes expected value after fees and after LLM cost, every model and rule is a challenger until it survives a deflated Sharpe gate and paper trading, and Checkpoint 3 is an explicit feasibility gate where the options are to go live, add capital, reduce trade count in favor of higher-conviction setups, or accept a lower target.

## What the evidence says, and what the design does about it

- Hierarchical wiring (independent analysts, one decision maker) beat debate and collaborative wiring in crypto multi-agent tests; the price and on-chain analyst was the alpha driver. Response: independent single-modality analysts with structured outputs and no peer chatter.
- Judge-based selection beat majority vote and blending on open-ended tasks, majority vote explains most multi-agent debate gains, and agreement between models is only weakly tied to being right. Response: one Judge selects one proposal; confidence comes from calibration, never from consensus; debate capped at one round.
- Models agree on wrong answers about 60 percent of the time, more so within one developer. Response: analysts spread across model families via Hermes and OpenRouter, the Adversary on a different family than the chosen proposal, cheap models only where output is schema validated with a HOLD fallback.
- Fees dominated the real-money LLM contest; the most active model made 238 trades against 38 for the least active. Response: daily turnover cap in the Risk Gate, EV after fees inside the Judge, post-only execution.
- A survey of 77 agentic trading studies found 2 of 19 with time-consistent splits, 1 of 19 modeling costs and none trading live. Response: anonymized tickers and dates in backtests, timestamped news, an explicit cost model, deflated Sharpe, paper before live.
- Shuffled k-fold on overlapping financial labels reports near-perfect scores on pure noise. Response: purged k-fold with embargo, combinatorial purged CV and walk-forward as the only accepted numbers.
- freqtrade has no first-party LLM hook and the maintainers closed the request. Response: the agent layer writes signals to a store and a thin freqtrade strategy consumes them through force entry and exit; the model proposes, the runtime disposes.

## Architecture Decisions

- freqtrade is the execution and backtesting shell. Rationale: confirmed Kraken spot support including stops on the exchange, trailing stops, position adjustment for ladders and partial exits, order timeouts, protections, dry-run mode, FreqAI for sliding-window retraining, native Telegram control, and a REST API for force entry and exit that the agent layer drives.
- Kraken's own kraken-cli MCP server is used for agent-side Kraken access (balances, paper trading, one-off queries) rather than community Kraken MCPs.
- The Risk Gate is deterministic code, not an LLM, and it is the last word before the human. It can shrink or reject, never enlarge.
- The agent cascade is layered and grounded in prior art: five parallel independent analysts, two proposal samples from two families, one Adversary round, one Judge that selects rather than blends.
- Roles run on the small profile wherever possible. Hermes Agent on the Hetzner VM (small OpenRouter profile) runs the five analysts, one of the two proposal samples (DeepSeek), the Adversary (Kimi or GLM, always a different family than the chosen sample) and the daily Reviewer draft. A Sonnet-class Claude, never Opus, runs the second proposal sample, the Judge (one compact call per actionable cycle, skipped on HOLD cycles), the Reviewer sign-off and the weekly Reflector. No big profile is used anywhere.
- Every LLM call is metered. Tokens in, tokens out, dollars, role and model family go into the ledger. The daily report shows PnL net of LLM cost, cost per decision, cost per executed trade and cost per HOLD cycle; the weekly report estimates the value each model family added by replaying decisions as if the cheap sample alone had been used. A role whose benefit trails its cost for a month is downgraded to a cheaper family or removed; the Judge is the last role to be cut. Everything stays inside the 130 CAD a month cap that also covers coding work.
- The VM is the runtime: freqtrade, DuckDB ledger, scheduler, MCP server and the existing Telegram gateway all live there.
- Confidence is a calibrated number, not a vibe. The Judge's fusion is re-fit against realized outcomes so that a 70 means roughly a 70 percent hit rate over time.
- Priors are encoded numerically (Bayesian) and as rules, not just as prompt text.
- The Decision Card speaks a full action vocabulary and execution is post-only by default. Actions: enter laddered, add (DCA), trim, exit, rotate, set or trail the stop, take-profit ladder, event trade, rebalance (weekly), hold or cancel. Kraken has no native OCO, so the stop lives on the exchange (stop-loss or stop-loss-limit through freqtrade's stoploss on exchange) and freqtrade manages the take-profit ladder and trailing logic client-side. Grid trading, scalps and hourly rebalancing are excluded as fee-hostile. Market orders are reserved for stops and emergency exits.
- Ingestion has two layers. An early-signal layer polls primary sources every 10 to 30 seconds or receives pushes (exchange listing announcement APIs from Bybit and OKX, Kraken's asset-listings RSS and AssetPairs polling, on-chain webhooks from Alchemy or Helius, official project X accounts through Grok X Search with handle filters, GitHub release feeds, Google News RSS, Telegram announcement channels via web previews). A confirmation layer (GDELT every 15 minutes, Alpha Vantage news sentiment, optional paid aggregators) corroborates but never leads. The Verifier treats exchange announcements and on-chain events as primary evidence, an official-handle X post as primary for its own project but still requiring a price or volume reaction to move sizing, and everything else as needing two independent sources.
- Grok's X access runs through OpenRouter, which Hermes already uses: the native web plugin on x-ai/grok models enables X Search with allowed and excluded handles and date filters, with the xAI tool cost passed through ($5 per 1k posts from Sept 21, 2026). The direct xAI API is the fallback with no markup. Results arrive as cited summaries, not a raw post feed, so the Verifier stores the citations.
- Usage data comes from DefiLlama's free API (fees, revenue, TVL, DEX volumes; Venice and Akash have fee adapters, Bittensor and Render are thin). Token unlocks are best-effort: no free first-party unlock API existed in September 2026 (DefiLlama's moved to a paid tier and CryptoRank's is Pro-only — see the corrections appendix), so unlock data is used where a source provides it, its absence is recorded as missing, and it fails closed for sleeve C. CryptoPanic's free API was discontinued in early 2026 and is optional paid only.
- Forecasting follows the financial ML playbook: full Kraken history from the CSV archives (the OHLC endpoint returns only the last 720 candles), volatility-scaled triple-barrier labels with meta-labeling, purged and embargoed validation, LightGBM or XGBoost as the primary model with classical and Bayesian baselines, isotonic or Platt calibration, conformal intervals, Optuna with a walk-forward objective and a deflated Sharpe gate before anything reaches paper trading.
- Champion/challenger for models; weekly embargoed Reflector for skills with human approval; the ledger as the single source of truth; paper first, then 25 percent of capital, then full capital, with the paper track in shadow after go-live.
- Second exchange: from Canada only Coinbase and Crypto.com serve a Toronto resident in 2026, neither lists small caps early, Coinbase charges 0.40 to 0.60 percent per side and neither is supported by freqtrade, while Kraken already lists VVV, TAO, RENDER and AKT. So the second account is opened with UAE residency when sleeve C is due: Bybit through its SCA-licensed entity if the residency is outside Dubai (full global catalog, spot demo trading, freqtrade official), or OKX UAE if it is Dubai (full VARA licence, 0.08 and 0.10 fees, spot demo, freqtrade official, narrower approved list). Ontario's CSA net-buy cap of CAD 30k per 12 months on assets other than BTC, ETH, LTC and BCH applies on Kraken too and is enforced by the Risk Gate while Shahrad is a Canadian resident.
- Funding flow: the agent never moves fiat and never holds withdrawal permission. When free cash per sleeve drops below its floor, or the Judge has a high-confidence card it cannot size, the bot posts a Funding Request card on Telegram (amount, sleeve, reason, deadline); Shahrad replies /fund approve or /fund decline and moves the money himself (Interac e-Transfer into Kraken from Canada, free deposit with CAD 10k daily and 30k weekly limits; AED bank transfer once in the UAE; USDC on Solana or Polygon between exchanges for well under a dollar); the bot polls balances, confirms arrival on Telegram and logs the whole exchange in the ledger. A whitelisted sweep address with a 24-hour lock is a later option.
- The whole pipeline is one installable package with one CLI and the same commands exposed as MCP tools, so Claude, Hermes and the scheduler call identical code. Everything that shapes behavior is a file or a versioned artifact.
- Telegram for the veto gate and funding requests; the daily digest also on Telegram at 10:00 Toronto time.
- News and sentiment are confirmation, never a trigger. A single viral post or single-source story cannot move the book.

## How a decision is made (the hourly loop)

1. Ingest. Early-signal layer (exchange listing feeds, on-chain webhooks, official X accounts through Grok X Search, GitHub releases, Google News RSS, Telegram previews) plus the hourly snapshot: exchange data (kraken-cli or ccxt: OHLCV, order book depth, spreads, balances, current fee tier), market-wide data (CoinGecko MCP), macro (Twelve Data and Alpha Vantage for gold via XAU/USD and the dollar index proxy, gold-api.com), geopolitics and news (GDELT conflict events with a Middle East filter and tone), usage (DefiLlama fees, revenue and TVL) and unlocks (CryptoRank). Output: a timestamped raw snapshot in the ledger.
2. Verify. Dedupe, source credibility weights, recency decay. Exchange announcements and on-chain events count as primary evidence; an official-handle X post is primary for its own project but needs a price or volume reaction to move sizing; anything else needs two independent sources or a price and volume reaction. Bot and cluster heuristics; a social spike without volume or order book support is hype risk, not signal; a single viral post is noise. Output: verified events with an evidence quality score from 0 to 1.
3. Quant models. The forecasting lab's champion models produce, per coin, P(up over 4h and 24h) with a calibrated probability and a conformal interval, expected move, volatility forecast and a regime label.
4. Worldview lens. Prior-adjusted probabilities from the Bayesian model, regime overlay rules (conflict escalation plus gold and dollar rising caps the speculative sleeve, blocks new speculative entries and tightens stops; a verified policy shock pauses new entries for 24 hours), and the Worldview document itself.
5. Analysts, five in parallel on the Hermes small profile. Technical, news and policy, social and hype, macro and geopolitics, on-chain and usage. Each reads the same Market Brief, writes a schema-validated structured report, and never sees the others. Malformed output becomes HOLD.
6. Proposal samples, two families. DeepSeek on the Hermes small profile and a Sonnet-class Claude, each producing per-coin actions from the action vocabulary with size, entry type, stop, target, horizon, a three-bullet thesis, a confidence estimate and an invalidation condition, citing only brief facts. If both say HOLD, the cycle ends here and no further model calls are made.
7. Adversary, one round, Hermes small profile on a different family than the leading sample, with tools for ledger base rates, unlock calendar (CryptoRank), fee calculator at the current tier and upcoming macro events. Output: objections with severity 1 to 5, its own P(up), and a recommended change (shrink, wait or reject).
8. Judge, Sonnet-class Claude, selection not blending. Picks one proposal or HOLD, fuses model probability and interval, analyst reports, sample disagreement, Adversary objections, evidence quality and regime fit into a calibrated confidence from 0 to 100 and an EV after fees at the current tier. Decision rule: act only if EV after fees is positive by a margin, confidence clears the sleeve threshold (roughly 60 for blue chip, 65 for AI coins, 75 for speculative), and no unresolved severity-5 objection remains. Size is a quarter Kelly scaled by confidence and interval width, then by target volatility over forecast volatility. Output: the Decision Card.
9. Risk Gate. Deterministic code. Per-position and sleeve caps, 3 to 5 concurrent positions, daily turnover cap, daily loss halt, weekly drawdown halt with human restart, minimum liquidity and maximum spread, mandatory stop on the exchange, post-only default, cooldown after consecutive losses, exchange status check, the Ontario net-buy cap, and the free-cash floor that triggers a Funding Request. It can shrink or reject, never enlarge.
10. Human veto. The Decision Card is posted to Telegram with action, size, confidence, EV after fees, top reasons, top risk, the Adversary's strongest objection and what would flip the call. It auto-executes after a 10-minute window unless vetoed. Commands: /veto, /pause, /resume, /status, /why, /flat, /fund approve, /fund decline.
11. Executor. The signal store hands the decision to the thin freqtrade strategy (dry-run first, then live) through force entry and exit: post-only limit orders repriced at most a few times, laddered entries and partial exits through position adjustment, the stop placed on the exchange at once, trailing and take-profit logic client-side, market orders only for stops and emergency exits, fills and fees logged.
12. Ledger. Every stage's inputs and outputs, every LLM call's tokens and cost, the human action, the execution result, and outcomes at +1h, +4h, +24h and at exit, for executed, rejected and vetoed proposals alike.

## How confidence is computed and shown

Inputs to the Judge: the Bayesian P(up) and its credible interval, the LightGBM and logistic probabilities with conformal intervals, the analyst reports, the two samples and their disagreement, the Adversary's probability and objection severities, the evidence quality score, and regime fit. The Judge produces a single confidence from 0 to 100 through a fusion mapping that is re-fit daily against realized outcomes (isotonic calibration), so the number stays honest. Agreement between samples is recorded but never used as confidence on its own. Bands: below 55 no trade; 55 to 70 small size; 70 to 85 standard size; above 85 maximum size within caps. Interval width matters on its own: a wide interval sizes a trade down even at the same point estimate.

The Decision Card always shows: action from the vocabulary and size, confidence and band, P(up) with interval, EV after fees at the current tier, evidence count split into primary, verified and unverified, the Adversary's strongest objection with severity, and the single condition that would flip the decision. The daily audit publishes a calibration curve so drift is visible within days.

## Trade types and the action vocabulary (spot only)

- Enter, laddered: two or three post-only limit slices (freqtrade adjust_trade_position with custom_entry_price; Kraken limit with the post flag). Sensible.
- Add (DCA): increase a winning or planned accumulation position at set levels (positive stake in adjust_trade_position, capped by max_entry_position_adjustment). Sensible for sleeve A accumulation.
- Trim: partial profit or exposure cut (negative stake in adjust_trade_position, or force exit with an amount). Sensible.
- Exit: limit exit with an unfilled timeout; market only on emergency exit. Sensible.
- Rotate: move value between sleeves or into stablecoins in risk-off (stablecoin pairs cost 0.20 flat). Daily, not hourly.
- Set or trail the stop: stop on the exchange (Kraken stop-loss or stop-loss-limit through stoploss on exchange), trailing stop, custom stoploss ratcheting after each candle. Required on every position.
- Take-profit ladder: minimal ROI ladder or custom exit with partial exits, limit orders to stay maker. Sensible.
- Event trade: position around a listing, unlock or policy date with a pre-set dated exit. Sensible with a hard time stop.
- Rebalance: return sleeves to target weights through a scheduled job. Weekly, never hourly.
- Hold or cancel: the default outcome of most cycles; cancel or amend resting orders that no longer fit (AmendOrder keeps queue position on quantity decreases).
- Excluded as fee-hostile: grid trading, scalps, hourly rebalancing, breakout chasing with market orders.

## Early signals and the verified data sources

- Early signal layer (seconds to minutes): Bybit and OKX public announcement endpoints, Kraken asset-listings RSS and AssetPairs polling every 10 to 30 seconds; Alchemy or Helius webhooks on free tiers for large transfers, unlock transactions and new pools; official project X accounts through Grok X Search with handle filters; GitHub release feeds; project blogs by RSS; Telegram announcement channels via t.me/s web previews (MTProto only with a dedicated account and accepted ban risk); Google News RSS per ticker.
- Confirmation layer (minutes to hours): GDELT every 15 minutes with tone and conflict coding, Alpha Vantage news sentiment, optional paid aggregators (CryptoPanic, Whale Alert), felo-search and felo-x-search for on-demand narrative checks.
- Verified in September 2026: DefiLlama free API (fees, revenue, TVL, DEX volumes, keyless, about 500 requests per minute; unlocks are Pro only at $300 a month); CryptoRank Sandbox API (free, 10k credits a month, 10 requests per minute, market data only — unlocks and vesting are Pro tier at $4,750 a year; correct base is https://api.cryptorank.io/v2/ with an X-Api-Key header); Grok X Search through OpenRouter's native web plugin ($5 per 1k posts from Sept 21, 2026, plus Grok tokens); CryptoPanic free API discontinued; Whale Alert paid only; Kraken OHLC endpoint limited to 720 candles.

## The forecasting lab

- Historical data. Kraken's downloadable OHLCVT CSV archives for full 1h history with quarterly updates, the Trades endpoint to fill gaps, the OHLC endpoint for hourly refresh; the second CEX through ccxt paging; CoinGecko as a gap filler for altcoins; GDELT archives; Twelve Data or Alpha Vantage for gold and the dollar. One DuckDB and Parquet store with one schema. Survivorship bias noted.
- Features. Returns at several horizons, realized and GARCH volatility, order book imbalance, volume z-scores, macro deltas for gold, dollar and the conflict index, filtered sentiment and social volume, usage deltas, AI category strength, fractional differentiation where memory matters, and the current fee tier.
- Labels. Volatility-scaled triple-barrier labels; a primary model sets the side and a meta-labeling model decides whether to act and how much.
- Validation. Purged k-fold with an embargo, combinatorial purged CV, walk-forward as the final check with retraining every 24 to 168 hours, sample weights by label uniqueness. Random shuffled k-fold is banned.
- Models. LightGBM or XGBoost as the primary classifier, AutoARIMA and GARCH as baselines, NumPyro Bayesian hierarchical logistic with the standing priors, River for online drift monitoring, neural forecasters only as challengers that must beat LightGBM out of sample on the VM's CPU budget.
- Calibration and intervals. Isotonic or Platt on purged out-of-fold predictions; MAPIE EnbPI conformal intervals.
- Sizing. Quarter Kelly from the calibrated probability, scaled by target over forecast volatility, capped by the Risk Gate.
- Optimization. Optuna TPE with pruning, 50 to 200 trials, objective equal to the mean out-of-sample metric across walk-forward folds after fees at the realistic tier; BoTorch or Ax only for expensive joint searches; deflated Sharpe and probability of backtest overfitting gate before paper.

## How the system improves itself (daily and weekly)

1. Reviewer (daily, 10:00 Toronto time): Hermes small profile drafts the metrics from the ledger, Sonnet-class Claude signs off, Shahrad reads it on Telegram. Net PnL after fees and after LLM cost, hit rate, Brier and calibration by band, attribution by sleeve, signal source, analyst, model family and regime, Adversary accuracy, veto value, cost per decision and per trade, fee drag, drawdown, prior flags.
2. Model updates: champion versus challenger under the lab's validation rules; promotion only on out-of-sample log-loss or Brier and fee-adjusted EV; rollback is one action; River updates hourly.
3. Reflector (weekly, Sonnet-class Claude, embargoed outcomes): drafts playbook updates and analyst belief updates with ledger evidence, plus the value-per-model-family estimate from counterfactual replay. Proposals require human approval before merging.
4. Prior flags: reported, never auto-changed.
5. Promotion board: Idea, Backtested (DSR and PBO gate), Paper, Shadow, Live, moved only through the daily review, with positive net EV after fees and LLM cost over at least four weeks or 200 cycles, calibration within tolerance, and Shahrad's approval.
6. Weekly strategy review: net EV versus target, regime breakdown, live versus paper calibration, cost versus benefit per role, feasibility gate, capital scaling decision.

## The pipeline as a reusable tool

One installable package, one CLI (ingest, forecast, decide, paper, review, retrain, backtest, propose-skill, ledger query, worldview get and flag, fund request) and one MCP server exposing the same commands as tools. Layout: config (settings, sources, worldview), data (ingest connectors including the early-signal listeners, DuckDB store), features, labels, models (registry, train, calibrate, intervals, tune), agents (brief builder, roles, harness adapters for Claude and Hermes with schema validation and cost metering), skills, risk (gate, kill switch, funding floor), execution (freqtrade strategy, signal bridge, order management), reports, tests (risk gate adversarial tests, leakage tests, schema tests). Editing behavior means editing a file and re-running the same tests.

## Coin universe and hard limits (initial parameters, tunable within bounds)

- Sleeve A, blue chip and stable: BTC, ETH, SOL, stablecoins as cash. Floor 40 percent of the book. Position cap 15 percent. Entry confidence 60.
- Sleeve B, AI-serving: VVV, DGRID-type projects, RENDER, TAO, AKT and similar, all on Kraken. Cap 35 percent. Position cap 15 percent. Entry confidence 65.
- Sleeve C, new and breakout: on the second exchange, only after A and B prove positive, must pass the usage filter and liquidity floor. Cap 20 percent. Position cap 5 percent. Entry confidence 75.
- 3 to 5 concurrent positions. Daily turnover cap. Daily loss halt at 3 percent. Weekly drawdown halt at 8 percent with human restart. Every position carries a stop on the exchange. Post-only by default. Minimum 24-hour volume and maximum spread thresholds. Cooldown after two consecutive losses. Ontario net-buy cap enforced. Go-live starts at 25 percent of capital.

## Tool map

- Execution and runtime: freqtrade (Kraken now, Bybit or OKX later, dry-run, stops on exchange, position adjustment, protections, REST force entry and exit, Telegram), kraken-cli MCP, ccxt, the Hermes VM (scheduler, DuckDB, MCP server, Telegram gateway).
- Agents: Sonnet-class Claude (Judge, one proposal sample, Reviewer sign-off, Reflector); Hermes Agent small profile via OpenRouter (five analysts, DeepSeek proposal sample, Adversary on Kimi or GLM, Reviewer draft); Grok through OpenRouter for X Search.
- Market data: CoinGecko MCP, Twelve Data, Alpha Vantage, gold-api.com, Kraken CSV archives and Trades endpoint.
- Early signals: Bybit and OKX announcement APIs, Kraken listings RSS and AssetPairs, Alchemy or Helius webhooks, GitHub release feeds, Google News RSS, Telegram previews.
- Macro and geopolitics: GDELT, Twelve Data and Alpha Vantage for gold and the dollar.
- Usage and unlocks: DefiLlama free API, CryptoRank Sandbox API.
- Social and qualitative: Grok X Search via OpenRouter, LunarCrush MCP (optional), felo-search and felo-x-search.
- Models and evaluation: LightGBM or XGBoost, scikit-learn, NumPyro or PyMC, River, arch, statsforecast, neuralforecast (challengers only), MAPIE, purgedcv or skfolio, Optuna, BoTorch and Ax, vectorbt, backtesting.py, freqtrade backtesting.
- Human loop and storage: Telegram, DuckDB and Parquet, git for skills, config and model versions.

## Task List

### Phase 0: Foundation (paper only)
- [ ] Task 1: Package skeleton, config, CLI and MCP server stub
- [ ] Task 2: Decision ledger, event schema and cost metering in DuckDB
- [ ] Task 3: Data layer connectors with a rate-limit budget
- [ ] Task 4: Early-signal listeners and primary-source feeds
- [ ] Task 5: Risk Gate rules engine, kill switch and funding floor
- [ ] Task 6: Paper-trading harness, freqtrade signal bridge and order management

### Checkpoint: Foundation
- [ ] A trivial strategy runs the full hourly loop in paper mode for 3 days on the VM with post-only orders and stops on the exchange, every cycle and every token is in the ledger, cost per cycle is measured, no API quota breaches, and the Risk Gate blocks every adversarial proposal in its test set.

### Phase 1: Forecasting lab
- [ ] Task 7: Kraken history backfill and unified market store
- [ ] Task 8: Feature pipeline with macro, geopolitics, usage and regime
- [ ] Task 9: Triple-barrier labels and meta-labeling
- [ ] Task 10: Purged validation harness and leakage tests
- [ ] Task 11: Verifier with primary-source handling
- [ ] Task 12: Primary and baseline models with calibration and intervals
- [ ] Task 13: Bayesian model with Shahrad's priors
- [ ] Task 14: Tuning loop with the deflated Sharpe gate

### Checkpoint: Forecasting lab
- [ ] Calibrated probabilities and conformal intervals on purged held-out data, the Verifier catches labelled pump events and ranks primary sources correctly, at least one sleeve shows positive net-of-fees EV in walk-forward backtest at the realistic fee tier, and that result passes the deflated Sharpe and PBO gate.

### Phase 2: Agent cascade
- [ ] Task 15: Worldview document and regime overlay rules
- [ ] Task 16: Harness layer for Claude and Hermes with schema validation and cost metering
- [ ] Task 17: Market Brief and the five analysts
- [ ] Task 18: Two proposal samples with the action vocabulary
- [ ] Task 19: Adversary agent on the small profile with base-rate tools
- [ ] Task 20: Judge selection, confidence fusion and Decision Card
- [ ] Task 21: Telegram veto gate, commands and funding requests

### Checkpoint: Decisions
- [ ] Two weeks of paper trading through the full cascade, calibration report reviewed, cost per cycle within budget, funding request flow exercised once, a sample of Decision Cards reviewed by Shahrad.

### Phase 3: Learning loops
- [ ] Task 22: Daily Reviewer at 10:00 Toronto time with cost accounting
- [ ] Task 23: Champion/challenger retraining and promotion gate
- [ ] Task 24: Reflector agent, skill approval flow and promotion board
- [ ] Task 25: Weekly strategy review, value per model family and feasibility gate

### Checkpoint: Feasibility gate
- [ ] Four weeks of paper results: measured daily EV distribution net of fees and LLM cost versus the $100 to $200 target, calibration holding, at least one promoted challenger and one approved skill flowed through end to end, value per model family reported. Decision: go live, add capital, cut trade count, or lower the target.

### Phase 4: Go-live and scaling
- [ ] Task 26: Live trading at 25 percent of capital with a shadow paper track
- [ ] Task 27: Second exchange onboarding (UAE residency) and speculative sleeve activation
- [ ] Task 28: Optional upgrades (multi-model Judge, paid sentiment tier, neural challengers, whitelisted sweep)

### Checkpoint: Complete
- [ ] Two profitable weeks at 25 percent with live calibration matching paper before scaling to full capital; all hard limits verified live.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Target is 1 to 2 percent per day on under $10k with Tier 1 fees of 0.40 and 0.80 percent | High | Post-only execution, EV after fees at the actual tier, paper measurement, explicit feasibility gate |
| Fee drag on hourly trading | High | Maker-only default, minimum expected move per trade, daily turnover cap, fee-hostile trade types excluded |
| Pump and dump or fake news moves the book | High | Verifier corroboration rules, primary sources ranked above social, sentiment as confirmation only, Adversary base-rate checks |
| Backtest overfitting and leakage | High | Purged and embargoed validation, leakage tests in CI, deflated Sharpe and PBO gate, anonymized backtests |
| Self-improvement drifts into overfitting or prompt churn | High | Champion/challenger gate, weekly embargoed reflection only, human approval for skill changes, priors never auto-edited |
| Cheap models hallucinate numbers or break format | Medium | Schema validation, HOLD fallback, numbers must exist in the brief, Sonnet-class Judge |
| LLM cost exceeds the benefit it brings | Medium | Per-call metering, PnL net of LLM cost, counterfactual value per family, downgrade rule, 130 CAD cap |
| Correlated errors across agents | Medium | Analysts across families, Adversary on a different family than the chosen sample |
| API rate limits, feed outages or scraper bans | Medium | Rate-limit budgets, caching, hold on stale data, no MTProto without a dedicated account |
| Second exchange access or listing changes | Medium | Open with UAE residency only when sleeve C is due, choose Bybit or OKX by emirate, keep sleeve C small |
| Key and account security | High | Trade-only keys, no withdrawal permission, secrets outside the repo, IP allow-listing, manual funding |
| Regulatory and tax treatment across Ontario and the UAE | Medium | Net-buy cap enforced by the Risk Gate; confirm tax treatment with a professional before going live; the plan does not give legal or tax advice |
| Calibration looks fine on paper but not live | Medium | Start live at 25 percent with the paper track in shadow, compare weekly before scaling |

## Open Questions

- Which emirate the UAE residency will be in, because it decides Bybit (outside Dubai) versus OKX (Dubai) for the second exchange. anything you think is good
- Which OpenRouter models to pin per analyst on the Hermes small profile at launch (DeepSeek, Kimi, GLM, Gemini Flash), and whether Kimi or GLM plays the Adversary. deepseek v4.1 flash
- Exact Kraken fee tier to assume in backtests for the first month (Tier 1 at 0.40 and 0.80 percent, or Tier 2 if 30-day volume clears $2.5k quickly). tier 2 is good
- Whether funding requests should have a standing monthly cap so the bot cannot ask repeatedly. sure
- Which transport serves the Sonnet-class roles (Judge, second proposal sample, Reviewer sign-off, Reflector). Recommended: OpenRouter `anthropic/claude-sonnet-5` with the project's own key, about $0.04 per Judge call and roughly $9 a month for the whole role set; alternatives are the direct Anthropic API (no key on the box) or the Claude Code CLI (not installed). Consequence for Task 16: if Sonnet arrives over OpenRouter, the `Harness` enum must be read as model family rather than transport, so one adapter serves every role.
- Who owns the Telegram receive path. The Hermes gateway already polls the bot token with getUpdates and Telegram allows one polling consumer per token, so the project's own telegram_bot.py cannot also poll it. Recommended: the project sends through the bot API without polling, and Hermes receives the commands and passes them to the project CLI; the alternative is a dedicated bot token for the project. Blocks Task 21.

## Appendix: verified infrastructure and corrections (added 2026-09-18)

Everything below was checked with live calls against the runtime box and the real accounts, not read from documentation.

**Infrastructure verified working**
- Python 3.11.16 via uv 0.12.13; `freqtrade>=2026.5,<2027` resolves cleanly on 3.11 (ta-lib wheel available, no source build). The box runs Ubuntu 26.04, 4 vCPU, 7.6 GB RAM, no swap, 133 GB free.
- kraken-cli 0.4.1 installed at `~/.local/bin/kraken`, with its MCP server registered in Hermes and scoped to `market,paper` (no order-placement tools exposed).
- Telegram: the existing Hermes bot and gateway are reused; delivery verified end to end.
- Grok X Search through OpenRouter verified: `x-ai/grok-4.3` with `plugins:[{"id":"web"}]` and `x_search_filter{allowed_x_handles,...}` returns real posts plus x.com citations at roughly $0.0096 per call. No xAI key needed.
- Kraken private API verified working: Balance, TradeVolume and OpenOrders all return data.
- Live fee tier: **Tier 1, maker 0.4000 percent**, next tier 0.3000 percent at $2,500 of 30-day volume; the account's 30-day volume is about $1,022.

**Corrections to this document**
1. **Kraken OHLCVT archives.** The download URL pattern implied above does not exist (404). Use `https://assets.kraken.com/marketing/institutions/Kraken_OHLCVT_2026Q2.zip` (538 MB quarterly incremental) or the five ~2 GB parts `Kraken_OHLCVT_Full_2026Q2.zip.part00` to `.part04`, with checksums in `OHLCVT_Full_PARTS_SHA256SUMS.txt`. Rows are `timestamp,open,high,low,close,volume,trades`, no header row, and intervals with no trades are omitted rather than zero-filled.
2. **CryptoRank.** Correct base is `https://api.cryptorank.io/v2/` with an `X-Api-Key` header (`/v0/*` hits a Cloudflare challenge, `/v1/*` returns 401, and `limit` must be 100/500/1000). Unlocks and vesting are **not** in the free Sandbox plan — those endpoints return `403 Endpoint is not available in your tariff plan` and sit in the $4,750/year Pro tier. DefiLlama's emissions endpoints are paid as well (`/emissions*` returns 402). This is why the usage bullet above now treats unlocks as best-effort.
3. **GDELT** returns 429 from this IP with a "one request every 5 seconds" limit and 10 to 13 seconds of latency. Confirmation layer only, with backoff, never a trigger.
4. **Keys required, not optional:** Twelve Data (401 without one), Alpha Vantage (error body without one), CryptoRank (403 without one). Keyless and verified: Bybit and OKX announcement APIs, Kraken public REST and AssetPairs, gold-api.com, DefiLlama fees/TVL, CoinGecko public, Google News RSS.
5. **Backtest fee assumption.** Tier 2 (0.30/0.60) was chosen for the first month, but the live account sits on Tier 1 (0.40/0.80) until 30-day volume clears $2,500. The Judge and the Risk Gate already price at the live tier read from TradeVolume; the conservative choice for the first backtests is Tier 1.
6. **Capital reality check.** The Kraken account held roughly $900 at verification (about $572 USD, 0.004 BTC and small alt balances), well below the under-$10k assumption in this plan. At that size, $100 to $200 per day is 11 to 22 percent per day rather than 1 to 2 percent, so the feasibility gate and sizing discipline matter even more than the plan states until capital is added.
7. **Key hygiene.** The API key was first created with no permissions at all (every private call denied). After that was fixed, `WithdrawMethods` returns data, which means the key currently carries withdrawal permission. This plan's rule is trade-only keys with withdrawals disabled — turn Withdraw Funds off at pro.kraken.com/app/settings/api.

**Decisions recorded 2026-09-18 (later the same day)**
- Package name: **`agentoquant`** (the addendum's `ctagent` was a placeholder). Read every `ctagent` in `tasks/schema_scaffold_addendum.md` as `agentoquant`.
- Integration branch is `main`; one branch per task named `task/NN-short-slug`, one worktree per parallel task under `~/work/agentoquant-wt/`. Details in `docs/HANDOFF.md`.
- Phase 0 runs in four waves: Task 1, then Task 2, then Tasks 3 + 4 + 5 in parallel, then Task 6. Later phases derive waves from each task's `Dependencies:` line.
- Agent operating rules live in `.hermes.md` (auto-loaded by Hermes) and `AGENTS.md` (portable); the assignment, verification and reporting protocol lives in `docs/HANDOFF.md`.
- Telegram: the project reuses the existing Hermes bot for outbound delivery; the inbound command path is still an open question above.
