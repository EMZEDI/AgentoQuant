# X (Twitter) and News/Hype Lookup Tools (Sept 2026)

Scope: feeding an hourly trading pipeline with live news and X activity about companies, blockchain projects, and market hype.

## Already available to this account

- **felo-x-search** skill: searches X/Twitter users, tweets, trending topics, and tweet replies.
- **felo-search** skill: real-time web search for current events and trends.
- **felo-web-fetch** skill: extracts full webpage content from a URL.
- Claude's own built-in WebSearch tool is also always available with no extra setup.

## Official X API: not viable as a low-cost path

As of 2026, X removed any practical free tier for search-style access. Pricing moved to pay-per-use by default: roughly $0.001 to $0.20 per call depending on the operation (plain text read about $0.015, read-with-URL about $0.20), capped at 3M post reads/month before a forced Enterprise upgrade. The old Basic ($200/month, 15k reads) and Pro ($5,000/month, full-archive search) tiers were both retired or auto-migrated to pay-per-use during 2026. For an hourly bot polling many tickers, costs here are unpredictable and can spike fast.

**Better alternative: Grok/xAI's "X Search" tool** (docs.x.ai). Lets an agent natively search X (keyword, semantic, user, and thread search, plus image/video analysis, date/handle filters) without touching the official X API. Pricing shifts on September 21, 2026 to content-based billing: $5 per 1,000 posts fetched, $10 per 1,000 profiles, notably cheaper than official X reads and usable via the xAI SDK, the OpenAI Responses API, or the Vercel AI SDK.

## MCP servers and dedicated tools found

- **LunarCrush MCP** (official, listed on PulseMCP and mcpservers.org) - purpose-built crypto and stock social intelligence: Galaxy Score, AltRank, social volume, dominance, and sentiment across 50+ topics, drawing on X and other social platforms. Pricing: Individual $90/month, Builder $300/month, Scale $900/month (roughly 20% off annually), Enterprise custom. No meaningful free tier.
- **kukapay/crypto-sentiment-mcp** (open-source, self-hostable) - lighter-weight crypto sentiment MCP.
- **nirholas/cryptocurrency.cv** (GitHub) - free, no API key needed, crypto news aggregator with RSS/JSON output and an explicit Claude MCP server. Good free supplement for headlines.
- Several unofficial generic Twitter/X scraper MCPs exist on Glama (Kylejeong2, gkydev, serima, takiAA, rafaljanicki, taazkareem). These are fragile and likely violate X's terms of service, not recommended for a production pipeline.

## Crypto news aggregators worth knowing

- **CryptoPanic** - free (rate-limited, token-based) and paid Developer/Pro plans, combining news with community bullish/bearish votes. A good semantic fit for hourly hype tracking (verify current rate limits at signup).
- **NewsAPI.org**-style generic news APIs commonly cap around 100 requests/day on the free tier and delay articles about 24 hours, too stale for an hourly trading signal.
- **GDELT Project** - free, keyless, high-volume, with built-in tone and sentiment scoring (the GKG dataset). Good free macro-news signal, though noisy. No dedicated MCP wrapper found besides cryptocurrency.cv's general news feed.

## Where the built-in WebSearch/felo-search tools fit

Fine for qualitative "what's the narrative right now" checks and catching viral news, but they give no structured sentiment score, no volume or velocity metric, and inconsistent X coverage since most X content sits behind a login wall. A dedicated API or MCP (LunarCrush, CryptoPanic, GDELT) is clearly better wherever the pipeline needs a numeric, comparable, rate-limit-budgeted signal every hour.

## Pitfalls to keep in mind

X/Grok costs scale with poll frequency times number of assets tracked. Free news APIs' delay and rate caps make them unusable for genuine hourly freshness. Academic research (ScienceDirect, USC Viterbi, an arXiv paper called "Perseus") documents organized X-driven pump-and-dump groups, so an agent that trades raw sentiment spikes risks becoming exit liquidity for someone else's coordinated pump. Confirm any sentiment spike against price and volume data before acting on it.

## Recommendation

1. **LunarCrush MCP** as the core structured crypto social-sentiment layer, if the $90/month starting price is acceptable.
2. **Grok API X Search** for targeted live X mentions per ticker, cheaper than the official X API.
3. **CryptoPanic and GDELT** (free or cheap) for hourly news and hype headlines.
4. The existing **felo-x-search** and **felo-search** skills as a free, already-available supplement for qualitative context, not as the source of any numeric signal fed into the model.

## Sources
- https://www.blotato.com/blog/twitter-api-pricing
- https://postproxy.dev/blog/x-api-pricing-2026/
- https://docs.x.ai/developers/tools/x-search
- https://docs.x.ai/overview
- https://www.pulsemcp.com/servers/lunarcrush
- https://lunarcrush.com/pricing/
- https://www.pulsemcp.com/servers/kukapay-crypto-sentiment-santiment
- https://github.com/kukapay/crypto-sentiment-mcp
- https://github.com/nirholas/cryptocurrency.cv
- https://cryptopanic.com/developers/api/plans
- https://adanos.org/insights/blog/best-crypto-sentiment-apis-2026/
- https://thunderbit.com/blog/best-news-apis-compared
- https://www.sciencedirect.com/science/article/pii/S1057521924004113
- https://arxiv.org/html/2503.01686v1
- https://glama.ai/mcp/servers/Kylejeong2/twitter-mcp
