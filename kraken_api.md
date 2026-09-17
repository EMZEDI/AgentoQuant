# Kraken and Exchange API Tooling for an AI Trading Agent (Sept 2026)

Scope: connecting Claude (via MCP, Claude Code, or plain Python) to Kraken specifically for account data and order placement, with fallbacks if Kraken tooling proves too thin.

## Kraken-specific MCP servers

The big development: **Kraken itself shipped an AI-native CLI with a built-in MCP server in 2026.**

- **krakenfx/kraken-cli** (github.com/krakenfx/kraken-cli, docs at docs.kraken.com/home/mcp) - a single binary exposing all 151 Kraken CLI commands as MCP tools over stdio, explicitly built to work with Claude, Cursor, Copilot, and Gemini. Covers market data (no auth needed), account and balances (read-only, needs an API key), and full trading: paper trading with live prices (no capital at risk), live spot and futures order placement, amendment, cancellation, staking, and subaccounts. Dangerous actions (real orders) require an explicit acknowledgement gate. 704 stars, 96 forks as of research, marked experimental by Kraken. Connects into Claude Code with `claude mcp add`.
- Community Kraken-only MCP servers are much thinner: `zacacollier/kraken-pro-mcp` (unauthenticated, read-only: ticker, asset info, system status, only 2 commits), plus smaller, largely unverified or abandoned entries (`xavierbeheydt/mcp-kraken` on mcpservers.org, a kraken-portfolio-mcp, a couple of Glama-listed variants). None of these support trading.

## Generic multi-exchange (ccxt-based) MCP servers, Kraken included

**ccxt** is the standard open-source library that gives unified programmatic access to 100+ crypto exchanges, including Kraken.

- **doggybee/mcp-server-ccxt** (138 stars, v1.2.1) - the most mature ccxt-based MCP found. Supports 20+ exchanges including Kraken by name, has real trading tools (place market/limit orders, cancel orders, futures/leverage), built-in caching and rate-limiting. API keys optional for public market data, required for private account/trading operations.
- A few forks and market-data-only variants exist (Obinox04/ccxt-mcp, lazy-dinosaur/ccxt-mcp, Nayshins/mcp-server-ccxt) if you want a narrower or differently licensed option.

## Kraken's own official APIs

- REST, WebSocket, and FIX APIs, with tiered rate limits (Starter/Intermediate/Pro tiers: counter caps of 15 to 20, decaying 0.33 to 1 point per second; some calls, like ledger queries, cost double).
- No full spot sandbox environment. Options for safe testing are: a limited qualified-client test environment, the `validate=true` dry-run flag on the AddOrder endpoint (validates an order without executing it), or trading with very small real position sizes.
- Kraken Futures does have a full demo environment at demo-futures.kraken.com, if futures ever become relevant.

## Official Anthropic or Kraken integration

Nothing beyond kraken-cli's own MCP support was found. Anthropic's "Claude for Financial Advisors" work (with Schwab, BlackRock, announced around September 2026) is wealth-management focused and unrelated to crypto or Kraken.

## Recommendation

For hourly Kraken trading with Claude today, the most practical path is **krakenfx/kraken-cli**'s built-in MCP server. It is Kraken's own, actively maintained relative to the alternatives, and its paper-trading mode is the natural place to validate the pipeline before risking real funds. As a fallback, or if that experimental tool proves unstable, Claude can write and run Python directly against Kraken's REST API using `krakenex` or `ccxt`, scheduled hourly, no MCP layer required at all.

If flexibility on exchange is acceptable, **doggybee/mcp-server-ccxt** is more battle-tested than any Kraken-only community server (though the Kraken-native CLI still looks like the better first choice), and **Coinbase Advanced Trade** has notably stronger official agent tooling (docs.cdp.coinbase.com/coinbase-for-agents, branded "Coinbase for Agents"/AgentKit) plus community MCPs like visusnet/coinbase-mcp-server. Alpaca's crypto API is also well documented and includes real paper trading.

## Sources
- https://docs.kraken.com/home/mcp
- https://github.com/krakenfx/kraken-cli
- https://github.com/zacacollier/kraken-pro-mcp
- https://mcpservers.org/servers/xavierbeheydt/mcp-kraken
- https://github.com/doggybee/mcp-server-ccxt
- https://github.com/ccxt/ccxt
- https://docs.kraken.com/api/docs/guides/spot-rest-ratelimits/
- https://support.kraken.com/hc/en-us/articles/360000919926-Does-Kraken-offer-an-API-test-environment-
- https://docs.cdp.coinbase.com/coinbase-for-agents/overview
- https://github.com/visusnet/coinbase-mcp-server
