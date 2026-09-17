# Market Data Tools: Crypto, Stocks, Gold (Sept 2026)

Scope: hourly-cadence trend watching across crypto, stocks, and gold, feeding a day-trading decision pipeline. Not a real-time tick feed, just reliable prices/indicators once an hour, 24/7, for months.

## Already installable as one-click Claude MCP connectors (found in Anthropic's official registry)

- **CoinDesk** - live and historical crypto data: spot/index OHLCV, orderbooks, ticks, toplist. Good crypto primary source.
- **Crypto.com** - real-time crypto prices, orders, candlesticks. Notable: authless, no API key needed for market data.
- **Twelve Data** - real-time prices plus 100+ technical indicators for stocks, forex, crypto, and ETFs from one key, plain-English querying. Free tier: 800 calls/day, 8/min. Good single backbone covering stocks and crypto together, and gold indirectly via the XAU/USD forex pair (verify this pair is included on the free tier before relying on it).
- **Alpha Vantage MCP Server** - stocks, options, fundamentals, earnings, SEC filings, indices, exchange rates, commodities (includes dedicated Gold and Silver spot and history endpoints), news, and 118+ technical indicator tools. Free tier is tight: 25 requests/day, 5/min, shared across every endpoint on one key (gold, stocks, news all draw from the same pool).
- Also present but more institutional/expensive, likely overkill for a solo trader: viaNexus vAST, Bigdata.com, FactSet AI-Ready Data, Zacks Data, Moody's Credit MCP, Clear Street, Kpler (maritime/commodities, not gold-focused), Energy Aspects (energy commodities, not gold).

## Other strong options not in the one-click registry (need direct setup)

- **CoinGecko official MCP server** - `docs.coingecko.com/ai-integration/mcp-server`, npm package `@coingecko/coingecko-mcp`. Real-time and historical prices, market cap, volume, trending coins, on-chain DEX data. Free keyless tier is throttled and shared; registering a free Demo API key gives a stable 30 calls/min. This is arguably the best crypto data source available, official and well documented.
- **CoinMarketCap** - no official MCP. Free Basic REST tier gives 10,000 credits/month and 30 req/min, but no historical data on the free tier, and the license is personal-use only. Fine as an occasional cross-check called directly from Python, not as a primary hourly source.
- **gold-api.com** - free, keyless, effectively unlimited real-time gold price calls; historical/OHLC capped at 10 requests/hour, which is fine at one call per hour. Simplest dedicated gold source if you do not want to burn Alpha Vantage's 25/day cap on it.
- **GoldAPI.io, metals-api.com, metalpriceapi.com** - exist, but true free tiers are typically capped low enough (well under 720 calls/month) that continuous hourly polling can exceed them. Verify current limits before relying on one.
- **yfinance** (Python library) - free and unofficial (a Yahoo Finance scraper, not a real API). Multiple 2025 to 2026 GitHub issues on `ranaroussi/yfinance` show frequent rate-limit blocking (429 errors), especially from cloud-hosted IPs, which is exactly the profile of an unattended scheduled pipeline. Several community "yfinance MCP servers" exist (onori, barvhaim, narumiruna, 9nate-drake, Alex2Yang97) but none is official or clearly dominant. Fine as a free supplementary stock check with caching and backoff, never as the sole critical-path stock source.
- **Polygon.io** (now under massive.com) - free tier is only 5 calls/min and end-of-day delayed data, not intraday. Not usable for genuine hourly trend tracking without a paid plan.
- **Finnhub** - free tier around 60 calls/min, covers US stock quotes, forex, crypto, and news. Decent stock backup.

## Rate-limit and reliability gotchas for 24/7 hourly polling

- MCP is just a protocol wrapper. It does not relax the underlying provider's rate limit, so budget against the REST tier regardless of whether access goes through MCP or a direct API call.
- Alpha Vantage's 25/day free cap is the tightest constraint in this whole stack. One symbol per hour already uses a third of it if gold, stocks, and news share the same key.
- CoinGecko's unauthenticated rate limit is dynamic and shared across all anonymous users, so it can tighten unpredictably under load. Register the free Demo key for a stable, dedicated 30/min.
- yfinance carries real, documented blocking risk for unattended cloud-hosted scheduled jobs. Treat it as best-effort, not load-bearing.
- Polygon/massive's free stock tier is end-of-day only, not intraday, so it does not satisfy an hourly-freshness requirement.

## Recommended minimal stack (3 sources, all free-tier)

1. **Twelve Data** - backbone for stocks and crypto together, single key, generous 800/day cap, possibly gold via XAU/USD.
2. **CoinGecko official MCP** (with the free Demo key) - deeper, more reliable crypto data than Twelve Data alone.
3. **Alpha Vantage** - dedicated Gold and Silver spot/history endpoint, plus its NEWS_SENTIMENT endpoint doubling as a light news source. Budget its 25/day cap carefully, use it mainly for gold and periodic news checks rather than high-frequency polling.

Optional fourth: direct yfinance Python calls as a free supplementary stock check with caching and backoff, never as the sole source.

## Sources
- https://docs.coingecko.com/ai-integration/mcp-server
- https://mcp.api.coingecko.com/
- https://support.coingecko.com/hc/en-us/articles/4538771776153-What-is-the-rate-limit-for-CoinGecko-API-public-plan
- https://coinmarketcap.com/api/pricing/
- https://www.alphavantage.co/documentation/
- https://www.macroption.com/alpha-vantage-api-limits/
- https://apicostcalc.com/finnhub.html
- https://twelvedata.com/pricing
- https://massive.com/pricing
- https://gold-api.com/pricing
- https://github.com/ranaroussi/yfinance/issues/2422
- https://github.com/Alex2Yang97/yahoo-finance-mcp
- https://glama.ai/mcp/servers/isdaniel/mcp-metal-price
