# AGENTS.md

This repo is **AgentoQuant**: an agentic hourly crypto trading system (Kraken spot, paper-first, human veto via Telegram). Nothing is implemented yet; the spec is complete and verified.

If you are an agent working here, read these before writing anything:

1. `.hermes.md` — the operating rules, hard safety rules, verified infrastructure and branch convention. **Hermes loads this automatically; other agents must read it manually.**
2. `plan.md` — architecture, hourly loop, standing priors, risks, plus an appendix of verified facts and corrections (trust the appendix over the prose where they disagree).
3. `tasks/todo.md` — 28 tasks in 5 phases with acceptance criteria and verification steps.
4. `tasks/schema_scaffold_addendum.md` — literal file tree, pinned versions, canonical enums and record shapes. Follow it verbatim.
5. `docs/HANDOFF.md` — how work is assigned, verified, merged and reported.

Non-negotiable in one line each: one task per prompt · paper/dry-run only until Task 26 · never place or cancel a real order · never move funds · never print or commit a secret (credentials live in `~/.config/agentoquant/credentials.env`) · no Opus or "big" profile · the Risk Gate only shrinks or rejects · purged/embargoed validation only · the spec's enums and shapes are literal.
