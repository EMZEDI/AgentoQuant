# Phase 0 — adversarial review, round 2 (post-fix)

Reviewer: adversary agent (round 2, delegated). Repo under review: `/home/shahrad/work/AgentoQuant`
`main` = `51d9ea2` (`c4807bb` + one docs-only commit). Report written on branch
`review/phase0-round2` in worktree `~/work/agentoquant-wt/review-round2`, which sits on `c4807bb`;
the only difference from `main` is a one-line test count in `docs/phase0_status.md`, so every
reproduction below is against the merged code.

Round 1's report is `docs/reviews/phase0_adversary.md` (18 findings). This round has two jobs:

- **Part A** — for each of the 18 round-1 findings, my own verdict (genuinely fixed / cosmetically
  fixed / still broken) with my own reproduction, never the fixer's test.
- **Part B** — new findings against the fixes themselves, severity-ranked.

Method: every claim below is backed by a command I ran and its real output, quoted. Anything I
could not reproduce is labelled **hypothesis**. I did not touch the soak's ledger or signal
directory; scratch work used `/tmp` ledgers and scratch signal dirs. No real order was placed.

*Status: skeleton — findings are appended and committed one at a time.*

---

## Part A — verdicts on the 18 round-1 findings

| # | Round-1 finding | Round-2 verdict | My reproduction |
|---|---|---|---|
| F1 | execution rows report fills as unfilled_timeout | _pending_ | |
| F2 | two risk_gate_verdict rows per cycle | _pending_ | |
| F3 | outcome stage has no writer | _pending_ | |
| F4 | exit / cancel_order TypeError at the venue | _pending_ | |
| F5 | plan price levels never reach hook paths | _pending_ | |
| F6 | soak evidence was nine identical cycles | _pending_ | |
| F7 | severity-5 objection approved | _pending_ | |
| F8 | gate pass-through for seven of twelve actions | _pending_ | |
| F9 | Ontario net-buy cap could never bind | _pending_ | |
| F10 | approved size is not the placed size | _pending_ | |
| F11 | nothing detected stale data | _pending_ | |
| F12 | dead config keys; 22 vs 23 rules | _pending_ | |
| F13 | BLOCKER: kill switch unwired, /flat closes nothing | _pending_ | |
| F14 | early signals bypassed the quota manager | _pending_ | |
| F15 | quota accounting was per process | _pending_ | |
| F16 | tests that cannot fail | _pending_ | |
| F17 | MCP name spelling; stale numbers | _pending_ | |
| F18 | fee floor is the maker rate | _pending_ | |

---

## Part B — new findings against the fixes

*Pending.*

---

## The two questions

*Pending.*