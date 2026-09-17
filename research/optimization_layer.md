# ML and Bayesian Optimization/Inference Layer: Library Research (Sept 2026)

Scope: hourly (not HFT) buy/sell decisions for crypto/stock day trading, roughly $100/day target, agent-orchestrated. Needs simple predictive modeling on price and sentiment data, Bayesian uncertainty-aware decisions and/or strategy tuning, and backtesting.

## 1. Bayesian optimization and inference

- **Optuna** - the simplest, most practical choice for "tune a trading strategy's parameters." Its TPE sampler trivially wraps a backtest as the objective function, and it is used internally by freqtrade's hyperopt. Very actively maintained, most popular library in the trading-bot community.
- **PyMC** - probabilistic models, for example a hierarchical Bayesian logistic regression estimating P(price up given signals) with credible intervals. Mature and actively maintained (v5, PyTensor backend).
- **NumPyro** - the JAX-backed equivalent of PyMC, faster MCMC/SVI when scaling across many assets. Actively maintained.
- **Stan / CmdStanPy** - the gold-standard MCMC engine, rigorous but heavier setup (needs a C++ toolchain). A community Claude Code plugin wraps it: bayesian-statistician-plugin (github.com/sunxd3/bayesian-statistician-plugin), combining Stan and ArviZ into an explore/design/develop/report workflow.
- **BoTorch (Meta, PyTorch) with Ax** - Gaussian-process Bayesian optimization for expensive or noisy objectives, for example joint multi-parameter, multi-asset strategy tuning. Actively developed, Meta published a new Ax/BoTorch engineering post in November 2025.
- **scikit-optimize** - lightweight GP-based hyperparameter search. The original repo is largely dormant; a community fork (holgern/scikit-optimize) keeps it alive, usable but less future-proof than Optuna or Ax.
- **GPyOpt** - archived February 2023, avoid.
- **GPflow** - maintained but niche, not needed unless building custom Gaussian process models.

Recommendation: Optuna for strategy-parameter tuning as the default, hourly-cadence-friendly choice. PyMC or NumPyro for the uncertainty-aware "probability price rises given signals" layer. Reach for BoTorch/Ax only if the tuning problem becomes expensive or noisy enough to need principled GP-based optimization, such as joint multi-asset tuning.

## 2. Lightweight ML

- **scikit-learn** - baseline classifiers and regressors (logistic regression, random forest) for signal modeling.
- **statsmodels** - ARIMA for trend. Note that GARCH/volatility forecasting needs the separate `arch` package (Kevin Sheppard), not statsmodels itself.
- **River** - online/incremental ML, well suited to hourly incremental model updates without full retrains (logistic regression, Hoeffding trees, drift detection).
- **XGBoost / LightGBM** - gradient-boosted trees for combining tabular technical and sentiment features, fast enough to retrain hourly.

## 3. Backtesting and strategy simulation

- **backtesting.py** - lightweight, simple API, good for quick single-asset hourly-strategy prototyping.
- **vectorbt** - vectorized (numpy/numba), very fast for large parameter sweeps, pairs well with Optuna for optimization loops.
- **Zipline-reloaded** - full event-driven backtester (community continuation of Quantopian's Zipline), heavier and more equities/portfolio-oriented, less crypto-native.
- **freqtrade** - open-source crypto trading bot framework. Confirmed to support Kraken for both spot and futures, alongside Binance, Bybit, OKX, Bitget, Gate, HTX, Hyperliquid, and others via CCXT. Ships built-in Hyperopt (Bayesian/TPE-style search) and FreqAI, an ML feature-engineering plus adaptive model plus RL integration layer. Most directly aligned with this project's exact use case.
- **Qlib (Microsoft)** - AI-oriented quant research platform with ML/RL pipelines and workflow automation, now paired with RD-Agent for automated research. Powerful but heavier setup, historically equities and China-A-share-centric. Usable for crypto with custom data adapters, but more overhead than freqtrade for a crypto-only pipeline.

## 4. Claude-specific tooling

No Anthropic-official trading or backtesting skill exists. The relevant built-in skills are the generic `data:analyze` and `data:statistical-analysis` skills, general-purpose statistics rather than trading-specific. Third-party and community options exist but are unverified, worth evaluating carefully before trusting them with real money:
- bayesian-statistician-plugin, a Claude Code plugin for Stan+ArviZ Bayesian workflows.
- A `tradingview-mcp` and a QuantConnect MCP server (on Docker Hub) for backtesting via Claude Code.
- Marketplace skill listings on mcpmarket.com ("quant-trading-strategy-optimization", "trading-backtesting-frameworks") and mcp.so's "backtesting" tag, roughly 11 servers.

Practical takeaway: the safest path is running the standard pip-installable Python libraries above directly in Claude's own code-execution environment for the actual math and backtesting, rather than depending on a niche or unverified MCP server for the correctness of the quant logic. An MCP or skill is more useful for data retrieval or workflow scaffolding than for the quantitative core.

## 5. LLM and quant trading research (architecture inspiration)

- **TradingAgents** (Tauric Research, github.com/TauricResearch/TradingAgents) - a multi-agent LLM framework with fundamental, sentiment, and technical analyst agents, bull and bear researchers, a trader, and a risk manager, converging on a trade decision. A strong architecture reference for role-based agent orchestration.
- **FinRL / FinRL-X** (AI4Finance Foundation) - deep-RL library and environments for algorithmic trading across stocks and crypto, still active (FinRL Contests 2025, FinRL-X infrastructure paper in 2026). More relevant if reinforcement learning gets added later.
- **FinRL-DeepSeek** (arXiv 2502.07393) - LLM-infused risk-sensitive RL trading agents, inspiration for combining LLM judgment with quantitative risk control.

## Recommended minimal stack

- Signal modeling: scikit-learn plus XGBoost/LightGBM (combining technical and sentiment features), River for hourly incremental updates, `arch` for GARCH volatility, statsmodels for ARIMA trend.
- Bayesian uncertainty layer: PyMC (or NumPyro for speed), a small hierarchical Bayesian logistic model estimating P(price up given signals) with credible intervals, feeding a risk-aware position-sizing rule.
- Strategy-parameter optimization: Optuna (TPE) wrapping a vectorbt or backtesting.py objective function, simplest to run inside an agent loop. Reach for BoTorch/Ax only for expensive or noisy joint multi-parameter searches.
- Backtesting and execution shell: freqtrade (Kraken-supported, built-in Hyperopt and FreqAI) to minimize custom exchange and execution plumbing.

All of the above are pip-installable and runnable directly in a Python execution environment. No deep-learning infrastructure is required given the "simple ML plus Bayesian" scope and the hourly, non-HFT cadence.

## Sources
- https://botorch.org/
- https://github.com/facebook/Ax
- https://engineering.fb.com/2025/11/18/open-source/efficient-optimization-ax-open-platform-adaptive-experimentation/
- https://optuna.org/
- https://github.com/optuna/optuna
- https://github.com/scikit-optimize/scikit-optimize
- https://github.com/holgern/scikit-optimize
- https://github.com/SheffieldML/GPyOpt
- https://www.gpflow.org/
- https://riverml.xyz/
- https://github.com/online-ml/river
- https://machinelearningmastery.com/develop-arch-and-garch-models-for-time-series-forecasting-in-python/
- https://www.freqtrade.io/en/stable/
- https://www.freqtrade.io/en/stable/freqai-running/
- https://github.com/microsoft/qlib
- https://github.com/sunxd3/bayesian-statistician-plugin
- https://mcp.so/tags/backtesting
- https://mcpmarket.com/tools/skills/quant-trading-strategy-optimization
- https://github.com/TauricResearch/TradingAgents
- https://github.com/AI4Finance-Foundation/FinRL
- https://arxiv.org/html/2603.21330v1
- https://arxiv.org/abs/2502.07393
