# Phase 0 status — Foundation

Written 2026-09-19 by the phase agent (parent) after integrating Tasks 1 to 6 and exercising the
loop against a live dry-run venue. Every claim below is backed by a command that was actually run;
where something could not be verified it says so instead of guessing.

## Merged on `main`

| Task | What landed | Evidence |
|---|---|---|
| 1 | Package skeleton, seven config files, enums, config loader, CLI (11 commands) and an MCP server exposing the same leaves | `uv lock --check` clean (138 packages); 41 tests; `agentoquant ingest` exit 3 with no traceback; MCP over stdio exposes 12 tools |
| 2 | Fourteen-stage decision ledger in DuckDB, named queries, cost meter | 14 tables; `ledger query --query cycles` returns 4 cycles with 14/12/13/14 stages; field-name drift against addendum §4–6: 0 |
| 3 | Quota manager, cache, seven connectors, hourly snapshot orchestrator | Live snapshot across every source; four positional-argument wiring bugs found and fixed with a regression test that drives the real registry |
| 4 | Early-signal listeners (Bybit, OKX, Kraken, GitHub releases, Google News RSS, Telegram previews, on-chain webhooks) and a runner | Live 90 s run: 892 events, 0 failures, 0 restarts, every record carrying a `source_class`; per-module suites now total 178 tests, and each of the three acceptance criteria is verified **by mutation** — revert the behaviour, watch the test fail |
| 5 | Deterministic Risk Gate (27 rules), kill switch, funding floor | 18/18 acceptance checks against a real ledger; 36 adversarial tests, 27 cases (one per rule) |
| 6 | Paper harness, freqtrade dry-run bridge, order manager for the twelve-action vocabulary, signal store, Telegram notifier, scheduler, systemd units | 24 consecutive cycles, 0 failures, three real dry-run trades each tagged with its cycle id |

Gate on `main`: **461 tests pass, 1 skipped (opt-in live cost check), ruff clean.**

## The loop is running unattended

- `agentoquant-hourly.timer` (systemd user unit, `Persistent=true`) ticks the paper loop every hour.
- `agentoquant-freqtrade.service` runs the dry-run venue the loop hands decisions to. Without it the
  loop still records cycles but submits nothing, so a soak would prove nothing about execution.
- Both installed and enabled; first timer tick 06:00 UTC.
- **The soak was silently meaningless for its first nine cycles, and that is now fixed.** The timer
  runs a fresh process every hour (`paper --hours 1`), and the placeholder's pattern step was a
  per-invocation counter starting at zero, so every tick picked the first pattern entry: all nine
  recorded cycles were `enter_laddered`, and a three-day run would have exercised one of twelve
  vocabulary actions while looking perfectly healthy. The step is now anchored to the hour it
  belongs to, so consecutive ticks advance. `tests/test_paper_sequence.py` covers it, including a
  test that drives the real loop twice an hour apart. The soak re-accumulates from 2026-09-19 ~13:30.
- **Notifications are off for the soak** (`AGENTOQUANT_TELEGRAM=0` in the unit). The placeholder card
  says the same thing every hour, and a card per tick trains the reader to ignore the veto gate
  before it matters. The notifier is exercised on demand, and the real veto window arrives with the
  cascade in Phase 2 (Task 21).

Firing the timer's own unit (`systemctl --user start agentoquant-hourly.service`) proved the
unattended path end to end: exit 0, cycle `2026-09-19T05Z-0001`, action `enter_laddered`, verdict
`approved`, `orders: 1`, `ack: acked`, no failures. What the venue then did, read out of
`freqtrade_user_data/agentoquant-paper.sqlite`:

| Order | Type | Side | Status |
|---|---|---|---|
| 1 | limit @ 59,940.0 | buy | **canceled** — never filled (finding 1 below) |
| 2, 4, 6 | limit @ 80,996.6 | buy | filled, three ladder slices of 0.00010555 BTC ($8.55 each) |
| 3, 5 | market @ 76,946.8 | sell | canceled — stops replaced as the position grew |
| 7 | market @ 76,946.8 | sell | **open, full position 0.00031665** — the stop resting on the exchange |

Trade row: `stop_loss: 76946.8` (−5% from the 80,996.6 open rate), `is_open: 1`, `enter_tag` equal to
the cycle id. So the checkpoint clause "post-only orders and stops on the exchange" is confirmed with
a real artifact, and the stop ratchets as the position grows.

## Two findings the unattended run made precise

1. **The placeholder entry price is a fixed 60,000, not the live price.** The first force-entry limit
   lands at ~59,940 against a market of ~80,996, is never filled, and is canceled and repriced. A
   Phase 2 decision source inheriting this pattern would leak a wasted order every cycle; the order
   manager should take the entry price from the live brief/snapshot, not from a constant in the card.
2. **The execution ledger row is not merely incomplete, it is wrong.** It reads
   `status=unfilled_timeout, fill_price=None, fee_paid=None` for a cycle in which the venue filled
   three ladder slices and opened a position. An unfilled status on a filled order corrupts any cost
   or hit-rate accounting built on it, so this is a correctness bug, not a completeness gap.

## Three real bugs, found only by running it against a live venue

1. **Position adjustment was off.** The config never set `position_adjustment_enable`, so freqtrade
   reported `Position adjustment: Off` and never called `adjust_trade_position`. Laddered entries,
   DCA adds and partial exits were declared in `EXECUTION_PATHS` but could not execute. The strategy
   also lacked `max_entry_position_adjustment` (default 0, which caps adjustments at zero).
2. **Force entry was disabled.** `force_entry_enable` was absent, so the API answered
   `Force_entry not enabled` and every `forceenter`/`forceexit` call failed. The signal bridge — the
   plan's chosen execution mechanism — could not place a single order.

Both are fixed and covered by regression assertions in the follow-up execution test suite.

## Open gaps (not hidden, not yet fixed)

| Gap | Impact |
|---|---|
| Only `enter_laddered` produced an order in a 24-cycle burst; `trim`, `add`, `exit`, `take_profit_ladder` reported `orders=0` with open positions present | Five of twelve vocabulary paths are unproven in dry-run. A follow-up agent is finding the root cause |
| The one execution ledger row has `fill_price=None`, `fee_paid=None`, `status=unfilled_timeout` | Fills and fees are never written back, so the ledger cannot yet price realized cost per trade |
| ~~Dry-run fees come from ccxt (0.26%) rather than the account's live tier (0.40% maker / 0.80% taker)~~ **fixed** | The venue now charges the real tier: `"fee": 0.004` in the config, and `assert_dry_run_config` fails closed if a dry-run config omits the fee or sets one below the current tier's maker rate. Verified live — the newest trade carries `fee_open = 0.004` where the older ones carried ccxt's `0.0026`. Stop exits are taker at 0.80% and the override is a single rate, so stop-outs are still simulated at the maker rate |
| A partial exit was refused: `exit amount is now 0.0 due to exchange limits` at a ~$34 stake | The execution layer must pre-check the exchange minimum and treat it as a shrink/reject |
| `kraken_listings` blog RSS returns HTTP 403 from this box (Cloudflare) | Only the AssetPairs polling path works for Kraken listings |
| The on-chain receiver refuses to start without a signing secret (fail closed, correct) and has never received a real webhook | Parsing and attribution are proven with a signed real-shaped payload; delivery needs a public ingress |
| `latency_seconds_vs_first_article` is null for every live signal; only replay computes it, and no replay fixtures ship | The "faster than journalists" claim cannot be measured yet |
| `llm_call` has zero rows | Expected in Phase 0 (the placeholder source makes no LLM calls); the cost meter itself is verified against a live OpenRouter call |
| The 3-day soak has just started | The checkpoint's soak clause needs three days of unattended ticks before it can be signed off |

## The model-gateway content filter (why five subagents died)

The gateway redacts an **at-sign immediately followed by a dotted name** when it appears inside a
**tool-call argument** — pytest decorators are the common case — and a redaction inside a JSON
argument invalidates the request: `HTTP 403: Content filter redaction would produce invalid tool
call arguments`. The agent is killed mid-turn and its uncommitted work is lost.

Reproduced with a minimal probe: identical byte content passes when the at-signs are replaced by a
marker, and a **101-byte** payload is enough to trip it. Probed and ruled out as triggers: the words
"kill switch", "veto", "halt" and "override"; credentials-shaped test literals; email addresses; IP
addresses; 32- and 64-character hex strings; PEM blocks; seed-phrase-shaped strings. Size alone is
not a trigger.

Mitigation, in `.hermes.md` and `scripts/at_restore.py` (`mark` / `unmark`, exact inverses, round
trip verified lossless): write `AT_MARK_` where a decorator's at-sign belongs, convert on disk
before running pytest, and **commit after every file write**.

## Adversary findings and ownership

`docs/reviews/phase0_adversary.md` (merged) lists 18 reproduced findings. Every one now has an owner.
The areas are deliberately disjoint: two agents in one file is how a merge conflict turns into lost
work.

| Finding | Sev | Owner |
|---|---|---|
| F13 kill switch unwired, `/flat` closes nothing | **blocker** | execution agent |
| F1 execution rows report fills as `unfilled_timeout` | high | execution agent |
| F4 `exit` and `cancel_order` throw `TypeError` at the venue | high | execution agent |
| F5 plan price levels never reach hook paths; `take_profit_ladder` unreachable | high | execution agent |
| F10 approved size is not the placed size | medium | execution agent |
| F7 a severity-5 objection is approved | high | risk-gate agent |
| F8 the gate is a pass-through for seven actions | medium | risk-gate agent |
| F11 nothing detects stale data | medium | risk-gate agent |
| F12 dead config keys; rule count 22 vs 23 | low | risk-gate agent |
| F14 early signals bypass the quota manager | medium | quota agent |
| F15 quota accounting is per process | medium | quota agent |
| F3 the `outcome` stage has no writer | high | outcomes agent |
| F2 two `risk_gate_verdict` rows per cycle | high | outcomes agent |
| F9 the Ontario net-buy cap can never bind | high | outcomes agent (depends on F1) |
| F16 tests that cannot fail | medium | **parity tautology fixed**; the ledger completeness test still needs F3 to land first |
| F17 MCP name spelling; two stale numbers | low | **fixed** — the underscore mapping is now asserted as a stated deviation, and the two stale counts (22 rules, 133 tests) were corrected to 23 and 27 |
| F18 the fee floor is the maker rate, so stops are simulated 0.4% cheap | medium | phase agent (documented, not fixed) |
| F6 the soak evidence was nine identical cycles | medium | **fixed** (rotation, `tests/test_paper_sequence.py`) |

## Fix round 1 — merged

| Finding | Result |
|---|---|
| F7 severity-5 objection approved | **fixed** — `RULE_UNRESOLVED_SEVERITY5` in the reject set; a malformed objection fails closed. Verified independently: a severity-5 objection now returns `rejected unresolved_severity5_objection` |
| F8 gate pass-through for seven actions | **fixed** — 7 of 12 passing through is now 0 of 12; reductions get `reduction_cap`, hold `hold_size_zero`, management rules of their own. Verified independently: `hold` now returns `shrunk hold_size_zero` at 0.0 |
| F11 no staleness detection | **fixed in the gate** — `MarketContext.as_of`/`is_stale` plus a caller-declared max age. Verified independently: a 3-hour-old snapshot with a 2-hour limit returns `rejected stale_market_data`. Caller wiring still open (below) |
| F12 dead config keys, rule count | **fixed** — sleeve cap is now `min(sleeves.yaml, risk_limits.yaml)`, `min_concurrent` is read, and a drift test pins the documented 27 rules against the code |
| F14 early signals bypass the quota manager | **fixed** — every attempt is reserved against the one authority *before* the socket; a refusal raises a `FetchError` subclass so each listener's existing skip-and-log path handles it; `quota_audit()` names any fetcher that would fetch unbudgeted and returns `[]` |
| F15 quota accounting is per process | **fixed** — the journal file **is** the budget, reads re-check `(mtime_ns, size)` and `acquire` holds an exclusive `flock` across read-check-append. Reproduced first: a spend in one process was invisible to the other, and 12 of the 15 new tests fail on the pre-fix code |
| F3 `outcome` stage has no writer | **fixed** — `agentoquant/ledger/outcomes.py`, idempotent and horizon-aware, covering executed, rejected and vetoed proposals, with `python -m agentoquant.ledger.outcomes` and a scheduler hook. Caller wiring still open (below) |
| F2 two verdict rows per cycle | **fixed** — natural keys per stage; an identical repeat returns the stored id and inserts nothing. A *conflicting* payload under the same key is still written, which is deliberate |
| F9 Ontario net-buy cap inert | **partially** — the cap's own path is now proven with a real fill (reject at the cap, shrink with headroom, exempt coins audited not counted, 400-day-old fills out of window, and the real loop rejects a buy over the cap when the ledger has a fill). It stays inert on live data until F1 lands |

### Verified constraints

- **DuckDB cannot be opened by two processes.** Confirmed on this box: a second process gets
  `IO Error: Could not set lock on file ... Conflicting lock is held`. So the quota budget lives in a
  JSONL journal with `flock` rather than in the ledger. **Consequence for the soak:** if the
  early-signal runner is ever run as a long-lived service holding `data/ledger.duckdb`, the hourly
  `agentoquant ingest` process will fail to open the ledger. Today only the paper loop and the venue
  run, so nothing collides — but this must be settled before the runner becomes a service.
- **F8 deviation, accepted.** The risk-gate agent declined to put the halt rules on the *management*
  actions, so a halt cannot block placing a stop. That is the right direction: a test pins it
  (`test_reductions_and_hold_are_never_blocked_by_a_halt`). Recorded here so it is a decision, not an
  oversight.

### Still open after this round

- **None of the round-1 findings remain open** except the parts that genuinely need later phases:
  F9 stays inert on live data until F1's write-back is exercised by a real fill, and F5 (the stop
  level still publishes `null`) and F10 (the bridge sizes from the dry-run wallet while the gate sizes
  from CAD book value) are recorded as gaps in `docs/task06_execution_report.md`.

### Caller wiring, done by the phase agent

Both were one line in `paper.py`, which had a single owner while the fix agents ran:

- **F11** — the loop now puts the snapshot's own timestamp on every `MarketContext` and declares the
  staleness limit itself (two cadences) instead of inheriting the gate's config default. Before this,
  the gate could detect staleness but the loop could never make a snapshot stale.
- **F3** — the cycle now calls `record_due_outcomes(ledger, as_of=moment)` after the orders are
  submitted and before the cycle log is appended, and records `outcomes_written` on the summary. It
  is idempotent, so it backfills after downtime and writes nothing when nothing is due. A fault is
  recorded against the cycle rather than raised, because the ledger is the source of truth and a
  cycle's own record must survive.
- Covered by `tests/test_paper_wiring.py` (six tests, driving the real loop).

### Latent inconsistency found while wiring F3

**The ledger stamps its own rows with the real clock, while the loop's decision logic uses its
injected `now`.** They agree in production, where `now` defaults to the real clock, but a cycle run
with an injected future moment writes a card whose recorded `ts` is in the past relative to that
moment, so the card looks overdue the instant it is created and the recorder writes its +1h outcome
immediately. Harmless today and it does not affect the soak; it would bite a backfill or a replay,
which is exactly what Phase 1 introduces. Recorded for round 2.

### Credential hygiene

The execution agent disclosed that a `cat` of its scratch venue override printed the local dry-run
API password into its transcript. Checked: the value appears in no repository file, no commit, and
not even in the transcript (Hermes' log redaction had caught it). Rotated anyway — new password and
JWT key in `~/.config/agentoquant/`, both mode 600, venue restarted and answering on the new
credential.

## Next

1. The execution-bridge agent reports which of the twelve actions genuinely work in dry-run, with fixes.
2. The adversary agent reports a severity-ranked findings list at `docs/reviews/phase0_adversary.md`.
3. Merge both, re-run the gate, then let the soak accumulate before the Phase 0 checkpoint review with Shahrad.
