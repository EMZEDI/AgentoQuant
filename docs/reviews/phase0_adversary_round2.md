# Phase 0 — adversarial review, round 2 (post-fix)

Reviewer: adversary agent (round 2, delegated). Repo under review: `/home/shahrad/work/AgentoQuant`.
Report written on branch `review/phase0-round2` in worktree `~/work/agentoquant-wt/review-round2`.

Revision reviewed: my branch sits on `c4807bb`; `main` had advanced to `184fed9` while I worked, and
`git diff c4807bb..main` touches **only docs** (`docs/phase0_status.md`, the test count 461 → 565), so
every reproduction below is against the merged code. Suite on this tree: `565 passed, 1 skipped in
130.32s`, `ruff check` clean — I re-ran it rather than taking the status doc's word for it.

Round 1's report is `docs/reviews/phase0_adversary.md` (18 findings). This round has two jobs:

- **Part A** — for each of the 18 round-1 findings, my own verdict (genuinely fixed / cosmetically
  fixed / still broken) with my own reproduction, never the fixer's test.
- **Part B** — new findings against the fixes themselves, severity-ranked.

Method: every claim below is backed by a command I ran and its real output, quoted. Anything I could
not reproduce is labelled **hypothesis**. All scratch work used `/tmp` ledgers, `/tmp` signal dirs and
`/tmp` quota journals; the soak's `data/ledger.duckdb` and `data/signals` were read read-only. No real
order was placed: against the live dry-run venue every write call (`forceenter`, `forceexit`,
`cancel_open_order`) was intercepted by a stand-in with the installed client's exact signatures, which
records the call instead of forwarding it, while the read-only calls (`ping`, `status`, `trade`,
`pair_candles`) went to the venue for real.

Severity legend: **blocker** (Phase 0 checkpoint cannot be signed off on this), **high** (a claimed
acceptance criterion is false or a safety invariant is unenforced), **medium** (real defect,
contained), **low** (hygiene / precision).

---

## Part A — verdicts on the 18 round-1 findings

| # | Round-1 finding | Round-2 verdict | Reproduction |
|---|---|---|---|
| F1 | execution rows report fills as `unfilled_timeout` | **genuinely fixed in code; unproven in the soak** | scratch loop writes `status=filled, fill_price=81000.0, fee_paid=0.0972`; the soak's nine rows all predate the fix |
| F2 | two `risk_gate_verdict` rows per cycle | **genuinely fixed** | 10 pre-fix cards have 2 verdicts each, the 2 post-fix cards have 1; identical repeats collapse, conflicting ones still write |
| F3 | the `outcome` stage has no writer | **genuinely fixed** | 17 live outcome rows (10 × +1h, 7 × +4h); a scratch cycle's +1h row written at `as_of=now+1h` |
| F4 | `exit`/`cancel_order` raise `TypeError` at the venue | **genuinely fixed** | against the live venue: `forceexit(tradeid=3, ordertype='market')` reached the client |
| F5 | plan price levels never reach the hook paths | **partially fixed** | ladder offsets and `minimal_roi` now travel; `stop_price` is still `null` in every published document |
| F6 | the soak evidence was nine identical cycles | **genuinely fixed** | live log advances hour by hour: `14Z` add, `15Z` exit, where the pre-fix `13Z` tick was `enter_laddered` again |
| F7 | a severity-5 objection was approved | **genuinely fixed** | `rejected unresolved_severity5_objection`; severity 4 still approves |
| F8 | the gate was a pass-through for 7 of 12 actions | **genuinely fixed** | 0 of 12 pass through a hostile card; each is stopped by a named rule |
| F9 | the Ontario net-buy cap could never bind | **fixed in mechanism; inert on live data until F1 has a fill** | seeded fill: `rejected ontario_net_buy_cap` at 31,000 CAD, `shrunk` to 1.111% at 29,990 |
| F10 | the approved size is not the placed size | **still broken** | soak ack: 25.65003 USD placed against 27.0 CAD approved (4.999% short) |
| F11 | nothing detected stale data | **fixed in the gate; the loop's wiring is tautological** | a 3-hour-old snapshot rejects; the loop stamps `as_of=now`, so the age is always 0 |
| F12 | dead config keys; 22 vs 23 rules | **genuinely fixed** | 27 rules, both former dead keys now move a verdict, the status doc is pinned by a test |
| F13 | **BLOCKER**: the kill switch was on no production path | **partially fixed — NOT closed** | the close now reaches the transport; a failed venue read silently closes nothing and reports success; no production trigger exists |
| F14 | early signals bypassed the quota manager | **genuinely fixed** | `quota_audit()` returns `[]`; an exhausted source is refused *before the socket* |
| F15 | quota accounting was per process | **genuinely fixed** | 4 processes, one journal, a 15-call ceiling: exactly 15 granted |
| F16 | tests that cannot fail | **parity tautology fixed; the ledger completeness test is still self-referential** | `registered_tool_names()` is still literally `cli.mcp_tool_names()`, but the cross-check now drives a real stdio session |
| F17 | MCP name spelling; stale numbers | **genuinely fixed** | underscore mapping asserted against the plan's literal spellings; the doc counts read 27 rules / 565 tests |
| F18 | the fee floor is the maker rate | **still broken (now documented, not fixed)** | venue `fee: 0.004` (maker) while `stoploss`/`emergency_exit` are `market`; ledger charges 0.317243 where the venue charges 0.158621 |

Everything in the table is reproduced in the sections below. Two of them (F5, F10) are declared as
gaps by the fixers themselves; F13's and F18's residuals are the ones I would act on.

---

### F1 — `execution` rows report fills as `unfilled_timeout` with null fill and fee

**Verdict: genuinely fixed in code; not yet proven by a live fill.**

Round 1's reproduction was "every REST-submitted order is recorded `unfilled_timeout`". One real entry
cycle through the real `run_cycle` against a scratch ledger:

```
$ .venv/bin/python /tmp/adv2/loop_checks.py     # entry cycle, fake transport that reports a fill
  summary: {"cycle_id": "2026-09-19T13Z-0001", "action": "enter_laddered", "verdict": "approved",
            "intents": 3, "orders": 3, "fills": 3, "fees_paid": 0.2916, ...}
  execution rows: [{'cycle_id': '2026-09-19T13Z-0001', 'status': 'filled', 'fill_price': 81000.0,
                    'fill_qty': 0.0003, 'fee_paid': 0.0972, 'order_type': 'post_only_limit'},
                   {...}, {...}]
```

`status=filled`, a real `fill_price`, a real `fee_paid` — three orders, three rows, and the fee is
priced at the maker rate (0.4% of 24.3 = 0.0972). The `_normalise`/`_trade_report` write-back and the
`forceenter` path both work.

**What is not fixed is the evidence.** The soak's own ledger, read read-only:

```
$ python3 -c "... query /tmp/adv2/soak_snapshot.duckdb ..."
execution rows total: 9
by status: [('unfilled_timeout', 9, 0, 0)]      # (status, count, non-null fill_price, non-null fee)
```

All nine rows still read `unfilled_timeout` with a null fill and a null fee, exactly as in round 1 —
and all nine predate the fix. No cycle since the fix has filled: the venue holds one open ETH/USD trade
and every cycle since has refused (`no_open_position_for_pair`) or been a hold. So F1 is
**fixed-in-tests and unproven-in-soak**, and it should be reported that way rather than as closed.
The knock-on is that F9's compliance cap is still inert on live data for the same reason (below).

---

### F2 — every cycle wrote two `risk_gate_verdict` rows

**Verdict: genuinely fixed.**

Round 1 counted per cycle, which is the wrong unit and is exactly what makes the fix look broken (the
15:00 tick and a manual 15:08 fire ran the *same* cycle id twice, with two cards). Counting per
decision card, on the soak's ledger:

```
$ python3 -c "... join risk_gate_verdict to decision_card on record_id ..."
('01M2W3D783AXSF2NNXVZ72K321', 2, 2026-09-19 05:49:42)   <- ten cards written before the fix
...                                                        each has 2
('01M2WZFPX09PE4PS0VNDHREBPR', 2, 2026-09-19 14:00:23)
('01M2X2XJEPR8JRHBHZZE6RNRQN', 1, 2026-09-19 15:00:23)   <- the two cards written after it
('01M2X3CZKX9FXWESAH5NBQ108C', 1, 2026-09-19 15:08:48)      each has 1
risk_gate_verdict rows total: 22  (10x2 + 2x1)
```

The double write stops exactly at the card where the fix landed. In my scratch loop a cycle writes one
verdict row for one card. And the question the brief asks — can the dedupe mask a genuinely conflicting
second verdict? — no:

```
$ .venv/bin/python ... (store-level)
ids: True <id-A> <id-C> <id-A>          # identical repeats collapse, d returns a's id
rows: [{'verdict': 'approved', 'rule_fired': None},
       {'verdict': 'rejected', 'rule_fired': 'kill_switch_flat'}]
count: 2
```

An identical repeat returns the stored id and inserts nothing; a **conflicting** payload under the same
key is still written as its own row. Nothing is masked. The residual (a conflicting repeat still yields
two rows for one card) is Part B B10.

---

### F3 — the `outcome` stage had no writer

**Verdict: genuinely fixed.**

Round 1's reproduction was `outcome rows: 0` and no code path that could write one. Now:

```
$ python3 -c "... soak snapshot ..."
outcomes: [('1h', 10), ('4h', 7)]
outcome latest: [('4h', False, None, '2026-09-19T11Z-0001'), ('1h', False, None, '2026-09-19T14Z-0001')]
```

and the cycle summary carries the count (`outcomes_written: 15` on the 15:00 tick — the backfill of
everything that had elapsed — and `2` on the 15:08 one). In my scratch run, a cycle writes nothing
while nothing is due, and the pass writes the +1h row the moment it is due:

```
  as_of = now+1.0h: due=1 written=1
  as_of = now+2.0h: due=1 written=1     (idempotent: the +1h row is not rewritten)
```

The `still_open`/`pnl` values are honest nulls/False (Phase 0 has no price source), which the fix
report states. Note that `still_open=False` for *every* row is a consequence of F1 being unproven —
the recorder's "was it filled" signal is the same non-null fill the soak does not have yet.

---

### F4 — `exit` and `cancel_order` threw `TypeError` at the venue

**Verdict: genuinely fixed.**

Round 1 bound the *manager's* kwargs to `FtRestClient` and got three `TypeError`s. The fix moves the
identifier resolution into the transport's `submit`, so the honest test is the one that goes through
`submit` — which is what I did, against the live venue, with every write intercepted:

```
$ set -a; . ~/.config/agentoquant/freqtrade.env; set +a; .venv/bin/python /tmp/adv2/venue_f13_f4.py
installed signatures:
  forceenter(self, pair, side, price=None, *, order_type=None, stake_amount=None, leverage=None, enter_tag=None)
  forceexit(self, tradeid, ordertype=None, amount=None)
  cancel_open_order(self, trade_id)
  venue reachable: True
  transport.open_positions() (from the live venue): [{'coin': 'ETH', 'pair': 'ETH/USD', ..., 'trade_id': 3}]
  calls the client received: [('forceexit', {'tradeid': 3, 'ordertype': 'market', 'amount': None})]
```

`forceexit` is called with the venue's own real `trade_id=3`, resolved from `status()`, and no `pair`.
`cancel_open_order` gets its `trade_id` the same way, and the cancel-and-replace `forceenter` carries
`side` (`{'side': 'long', 'enter_tag': 'reprice:13Z-f4', ...}` binds). A raw-kwargs binding of a
`forceexit` intent still shows `missing=['tradeid']` — that is by design now, because `submit` supplies
it, and my binding shortcut simply skipped that step. The live run above is the proof.

---

### F5 — the plan's price levels never reached the hook paths

**Verdict: partially fixed.** The ladder half is genuinely fixed; the stop half is still broken.

Round 1 quoted a published document with `stop_price: null, target_price: null`, and showed
`take_profit_ladder` was unreachable *by construction*. Now, from `signal_document` for each action:

```
  enter_laddered       stop_price=None target_price=None ladder_slices=3 offsets=[0.1, 0.25, 0.4] minimal_roi={}
  take_profit_ladder   stop_price=None target_price=None ladder_slices=1 offsets=[] minimal_roi={'0': 0.04, '30': 0.02, '60': 0.01, '120': 0.0}
  trail_stop           stop_price=None target_price=None ladder_slices=1 offsets=[] minimal_roi={}
  set_stop             stop_price=None target_price=None ladder_slices=1 offsets=[] minimal_roi={}
  exit                 stop_price=None target_price=None ladder_slices=1 offsets=[] minimal_roi={}
```

What is fixed: the per-slice ladder offsets travel (`ladder_offsets_pct`), the bridge prices each slice
from the venue's own rate plus that offset, and `AgentBridgeStrategy.custom_exit` now falls back to
`roi_step(execution['minimal_roi'], ...)` when `target_price` is falsy — so the take-profit branch is
reachable and no longer impossible by construction. That is a real fix.

What is not: **`stop_price` is still `null` in every published document**, so for `set_stop` and
`trail_stop` the bridge ignores the plan and invents its own level
(`AgentBridgeStrategy.py:530-533`: `stop_price = current_rate * (1 - agent_trail_percent/100)`). The
Risk Gate sized and approved a stop price that never reaches the venue. A `set_stop` card's whole
purpose is the level it names; the level is dropped on the floor. This is F5's surviving half and the
fixers declare it open in `docs/task06_execution_report.md` ("Not done" 3).

---

### F6 — the soak evidence was nine identical `enter_laddered` cycles

**Verdict: genuinely fixed** (the defect), **with thin coverage** (the evidence).

Round 1's defect was that the pattern step was a per-process counter, so every hourly tick restarted at
the first action. The step is now anchored to the hour (`sequence_for(moment) = int(moment.timestamp() // 3600)`),
and the soak's live log proves consecutive ticks advance:

```
$ .venv/bin/python ... (logs/paper/cycles.jsonl from the main clone)
2026-09-19T13:00:23  2026-09-19T13Z-0001  enter_laddered  approved   <- ran before the fix landed (~13:21)
2026-09-19T14:00:23  2026-09-19T14Z-0001  add             approved   497174 % 8 = 6 -> pattern[6] = add
2026-09-19T15:00:23  2026-09-19T15Z-0001  exit            approved   497175 % 8 = 7 -> pattern[7] = exit
2026-09-19T15:08:48  2026-09-19T15Z-0001  exit            approved   (the manual fire)
```

Two consecutive unattended ticks landed on two different actions, which is the rotation working. The
residual is scope: the live soak has now exercised `enter_laddered`, `add` and `exit` — three of the
eight pattern actions and three of the twelve vocabulary actions. The "at least one cycle per
vocabulary action" bar round 1 suggested is still not met.

---

### F7 — a severity-5 adversary objection was approved

**Verdict: genuinely fixed.**

```
$ .venv/bin/python /tmp/adv2/gate_checks.py
F7 — a severity-5 objection on an otherwise clean entry
  verdict=rejected rule=unresolved_severity5_objection final=0.0
  severity 4 (control)      verdict=approved rule=None final=3.0
```

`RULE_UNRESOLVED_SEVERITY5` is in `REJECT_RULES` (`gate.py:158`) and read through `objection_severity`
in `_first_rejection` (`gate.py:685-690`), with the reason stated in the code. A severity-4 objection is
untouched, which is the right boundary.

---

### F8 — the gate was a pass-through for seven of the twelve actions

**Verdict: genuinely fixed.**

Round 1's reproduction: seven actions returned `approved / rule=None / final == original` for a card
that violated every rule at once. The same hostile card now:

```
$ .venv/bin/python /tmp/adv2/gate_checks.py
  enter_laddered         verdict=rejected  rule='incomplete_card'   orig=99.0 final=0.0
  add                    verdict=rejected  rule='incomplete_card'
  trim                   verdict=rejected  rule='incomplete_card'
  exit                   verdict=rejected  rule='incomplete_card'
  rotate                 verdict=rejected  rule='incomplete_card'
  set_stop               verdict=rejected  rule='incomplete_card'
  trail_stop             verdict=rejected  rule='incomplete_card'
  take_profit_ladder     verdict=rejected  rule='incomplete_card'
  event_trade            verdict=rejected  rule='incomplete_card'
  rebalance              verdict=rejected  rule='incomplete_card'
  hold                   verdict=shrunk    rule='hold_size_zero'    orig=99.0 final=0.0
  cancel_order           verdict=rejected  rule='incomplete_card'
  actions that still pass through: 0 []
```

And the other half of the question — can the new rules false-reject a *legitimate* card? On clean
contexts, no:

```
  trim, coin present, no position in context     verdict=approved  rule=None  final=1.0
  exit, coin present, no position in context     verdict=approved  rule=None  final=3.0
  hold, clean                                    verdict=shrunk    rule='hold_size_zero' final=0.0
  set_stop / trail_stop / cancel_order, clean    verdict=approved  rule=None
  take_profit_ladder, clean                      verdict=approved  rule=None  final=3.0
```

`hold` reports `shrunk` rather than `approved`, which is a cosmetic verdict-value change, not a
functional one (a hold places nothing either way). The never-enlarge sweep still holds: 540 verdicts
across 12 actions × 9 sizes × 5 contexts, `enlargements or raises: 0`.

---

### F9 — the Ontario net-buy cap could never bind

**Verdict: fixed in mechanism; still inert on live data (F1's dependency), plus a fail-open residual.**

Round 1's reproduction was that the cap's own query found no fills. Seeding one real fill into a
scratch ledger (SOL, 130.0 × 230.0 = 29,900 USD, on a non-exempt coin — BTC/ETH/LTC/BCH are exempt in
the gate and would never have demonstrated this):

```
$ .venv/bin/python /tmp/adv2/final_checks.py
  ontario_net_buys_cad_12m from the ledger: {'SOL': 29900.0} (= 130.0 x 230.0 USD)
  cap: 30000.0 CAD
  seeded fill, ledger-derived total                  verdict=approved  rule=None
  context says SOL at 29,000 CAD                     verdict=approved  rule=None
  context says SOL at 29,990 CAD (10 CAD headroom)   verdict=shrunk    rule='ontario_net_buy_cap' final=1.1111111111111112
  context says SOL at 31,000 CAD                     verdict=rejected  rule='ontario_net_buy_cap' final=0.0
  non-empty mapping WITHOUT SOL (fail-open probe)    verdict=approved  rule=None
```

The cap binds, shrinks with headroom, and rejects over the limit. Round 1's "can never bind" is
answered. On the live soak it is still inert, because the live ledger still has no non-null fill (F1)
— `execution rows with a non-null fill: 0`. And the last line is the residual round 1's own fix report
flagged: a non-empty `ontario_net_buys_cad_12m` mapping that does not name the coin returns 0.0
(`gate.py:792-805`), i.e. unknown is read as zero and the buy is approved. Part B B6.

---

### F10 — the size the gate approves is not the size the venue places

**Verdict: still broken.**

```
$ .venv/bin/python /tmp/adv2/final_checks.py
  ack: {'cycle_id': '2026-09-19T13Z-0004', 'result': {'action': 'ladder_slice', 'of': 3,
        'pair': 'ETH/USD', 'slice': 3, 'stake': 8.550010091565}}
  three slices: 25.65003 USD
  the gate approved 3.0% of a 900.0 CAD book = 27.0
  shortfall: 1.34997
```

Unchanged from round 1: the gate sizes from `settings.capital.starting_capital` (900.0, a CAD figure
treated as USD because `usd_cad_rate` defaults to 1.0, `gate.py:309`), while
`AgentBridgeStrategy._stake_for` sizes from `self.wallets.get_total_stake_amount()` × the venue's
`tradable_balance_ratio` (0.95). The 5% gap in the soak is the ratio; the dangerous direction is the
other one — if the wallet ever exceeds the config constant (profit, a manual top-up), the venue places
more than the gate approved and nothing downstream re-checks the 15% position cap or the sleeve cap.
`docs/task06_execution_report.md` records this as an open gap; nobody fixed it.

---

### F11 — nothing in the gate could detect stale data

**Verdict: genuinely fixed in the gate; the loop's wiring is tautological.**

The gate now rejects an old snapshot and honours the ledger's own flag:

```
$ .venv/bin/python /tmp/adv2/gate_checks.py
  3-hour-old snapshot, 2h limit  verdict=rejected rule=stale_market_data
  59-minute-old snapshot         verdict=approved rule=None
  is_stale=True, no as_of         verdict=rejected rule=stale_market_data
  exit under stale data           verdict=approved rule=None
```

That is a real fix, and the reduction pass-through is the right call.

The caller side is where it becomes cosmetic. `placeholder_context` now passes
`market_data_max_age_s=market_data_max_age_s()` **and** `as_of=now` for every market it builds
(`paper.py:137-142`), where `now` is the cycle's own decision moment. So the snapshot's age is always
exactly zero and the loop can never produce a stale verdict. It is labelled as such in the code
comment, and in Phase 0 there is no other snapshot to age — but the checkpoint clause it was meant to
support ("fail closed on stale data") is still not exercised by anything that runs. Additionally, a
declared age limit plus a market with no `as_of` is *rejected* (see B7), which is the opposite of the
gate's own stated principle for an unknown age.

---

### F12 — dead config keys in `risk_limits.yaml`, and "22 rules" against a real 23

**Verdict: genuinely fixed.**

```
$ .venv/bin/python /tmp/adv2/gate_checks.py
  ALL_RULES: 27
  status doc line: ['| 5 | Deterministic Risk Gate (27 rules), kill switch, funding floor | 18/18 acceptance checks against a real ledger; 36 adversarial tests, 27 cases (one per rule) |']
  sleeves.yaml caps: {'A': 100.0, 'B': 35.0, 'C': 20.0}
  risk_limits positions: min_concurrent=3 max_concurrent=5 max_position_pct=15.0 sleeve_caps_pct={A: 100.0, B: 35.0, C: 20.0}
```

Both former dead keys have readers (`_sleeve_cap_pct` takes the tighter of the two files;
`_diversification_cap_pct` reads `min_concurrent`), the count matches the code, and a test parses the
status-doc line so the drift cannot come back silently. The residual is that the same cap is still
stated in two files, so "one source, named once" is not achieved — the fix report says so, and the
tighter-of-two rule does remove the silent-failure trap.

---

### F13 — the kill switch is not on any production path, and `/flat` closes nothing

**Verdict: partially fixed. The blocker is NOT closed.** See Part B B1, B2 and the final answer.

Round 1's two halves, checked separately.

**Half one — does a halt reach the gate?** Yes, and it survives a process restart:

```
$ .venv/bin/python /tmp/adv2/f13_checks.py
  a new process restores: HaltState(flat=True, daily_halted=False, weekly_halted=False)
  healthy venue: {"action": "enter_laddered", "verdict": "rejected", "intents": 0, "orders": 1,
                  "closes": 1, "errors": 0, "venue_error": null, "venue_available": true}
    intents the transport received: [('forceexit', 'exit', 'ETH/USD')]
```

`paper.py:245-252` builds the switch and passes `switches.halt_state()` into every `PortfolioContext`,
`build_kill_switch` persists to `data/kill_switch.json`, a new process restores it, the gate returns
`kill_switch_flat` (three live soak cycles show exactly that rule), and the close goes through the same
order manager and transport as every other exit. Against the live venue with a healthy read, the
`forceexit` carried the venue's real trade id. That is a large amount of real work and it is real.

**Half two — does it still report success while doing nothing?** Yes, under a condition the repo has
already observed on this venue. `paper.py:306-314` builds the close list from
`orders.open_positions()`, and `OrderManager.open_positions` (`order_manager.py:1014-1027`) catches
every exception and returns `[]`, as does `_FreqtradeDryRunTransport._open_trades`
(`order_manager.py:526-532`). With the position read failing the way the live API fails:

```
$ .venv/bin/python /tmp/adv2/f13_checks.py
A /flat, venue healthy: the close reaches the transport
  healthy venue: {"intents": 0, "orders": 1, "closes": 1, "errors": 0, "venue_available": true}
    intents the transport received: [('forceexit', 'exit', 'ETH/USD')]
A /flat, venue read times out: the close is skipped and NOTHING reports it
  venue read failed: {"intents": 0, "orders": 0, "closes": 0, "errors": 0, "refused": 0,
                      "venue_available": true, "venue_error": null}
    intents the transport received: []
```

Exit code 0, `errors: 0`, `venue_error: null`, `degraded` false, every position still open. And this is
not a hypothetical condition on this box — the venue answered `status()` **once in six attempts**:

```
$ .venv/bin/python - <<'EOF'   (live venue, read-only)
  attempt 0: status ok in 0.08s trades=[('ETH/USD', 3)]
  attempt 1: status FAILED in 10.01s ReadTimeout
  attempt 2: status FAILED in 10.01s ReadTimeout
  attempt 3: status FAILED in 10.01s ReadTimeout
  attempt 4: status FAILED in 10.01s ReadTimeout
  attempt 5: status FAILED in 10.00s ReadTimeout
open trades visible: 1/6 attempts
transport.open_positions() now: []
```

The one case where the healthy-read close did fire, it fired correctly; the failure mode is the same
one round 1 found — **a safety control that reports success and does nothing** — narrowed but not
closed. There is also still no production trigger at all: nothing in `agentoquant/` calls
`trigger_flat` (`grep` finds it only in `tests/` and `scripts/task5_acceptance.py`), so the only ways a
halt is ever raised are a hand-written `data/kill_switch.json`, the acceptance script, or the daily
halt — which nothing calls either (B2).

---

### F14 — the early-signal layer bypassed the repo-wide quota manager

**Verdict: genuinely fixed.**

```
$ .venv/bin/python /tmp/adv2/quota_checks.py
  listeners built: ['bybit_listings', 'okx_listings', 'kraken_listings', 'github_releases', 'google_news_rss', 'telegram_previews']
  quota_audit() unbudgeted fetchers: []
  fetchers with no quota manager attached: []
F14 — a fetch refused before the socket (offline transport, budget at zero)
  bybit_announcements remaining before the fetch: 0
  refused before the socket: FetchQuotaExceeded: bybit_announcements: quota refused before the request: quota for 'bybit_announcements' would be breached: calls_per_minute is 10/10, next call allowed in 60s
  requests actually sent: []
  breaches now: {'bybit_announcements': 1}
```

Every fetching listener is wired to one manager, the audit function names any that is not, and a fetch
with the budget exhausted is refused before the socket is touched (`requests actually sent: []` against
a transport that raises if used). Round 1's "quota.breaches() is structurally zero for those seven
sources" is answered.

---

### F15 — quota accounting was per process, so a budget could be spent twice

**Verdict: genuinely fixed.**

```
$ .venv/bin/python /tmp/adv2/quota_checks.py
F15 — four processes, one journal, a 15-calls-per-minute ceiling for kraken_rest
  {"workers": 4, "granted_across_processes": 15, "refused_across_processes": 145,
   "journal_lines": 160, "journal_lines_that_are_calls": 15, "ceiling": {"calls_per_minute": 15}}
F15 — two processes released at the same instant
  {"workers": 2, "granted_across_processes": 15, "refused_across_processes": 65, ...}
F15 — a long-lived instance that loaded its view first (the round-1 shape)
  B's first check (loads its view): remaining = 15
  A spent: 15
  B remaining AFTER A spent the budget: 0
  B refused: quota for 'kraken_rest' would be breached: calls_per_minute is 15/15, next call allowed in 60s
F15 — does the lock actually exclude a second process at the same moment?
  three children each hold the lock 1.5s: ['child holds', 'child holds', 'child holds'], wall 4.74s
```

Exactly 15 calls granted against a 15-call ceiling across four processes and across two processes
released simultaneously; the round-1 shape (B loads first, A spends, B asks again) now refuses; and the
`flock` genuinely serialises (4.74 s wall for three 1.5 s hold critical sections, i.e. ~3 × 1.5, not
~1.5). Two processes writing at the same instant cannot both take the last unit. The residual — the
journal grows one line per refused attempt (145 lines for one exhausted minute here) — is B12.

---

### F16 — tests that cannot fail

**Verdict: the parity tautology is genuinely fixed; the ledger completeness test is unchanged.**

`registered_tool_names()` is *still* `return cli.mcp_tool_names()` (`mcp_server.py:24-26`), but that is
no longer the assertion: the test compares the helper against a real stdio session and asserts the
plan's literal spellings through `plan_command_to_tool_name`:

```
$ grep -n "def test_the_registry_helper_agrees_with_the_real_server_session" -A 12 tests/test_cli_mcp_parity.py
def test_the_registry_helper_agrees_with_the_real_server_session() -> None:
    """``registered_tool_names()`` delegates to the CLI helper, so check it against a real session.

    This replaces ``assert registered_tool_names() == cli.mcp_tool_names()``, which compared a
    function to itself ... and could never fail (adversary finding F16).
```

The second half is not fixed. `tests/test_ledger_roundtrip.py:588-595` still asserts
`counts["outcome"] == 12` over a synthetic day the test file itself writes, so it proves the table and
the store helper work and says nothing about whether the loop writes outcomes. It is named for a
property it does not test; F3 made the underlying claim true, but the test is still the wrong shape.

---

### F17 — MCP name spelling; two stale numbers

**Verdict: genuinely fixed.**

```
$ .venv/bin/python -c "from agentoquant import mcp_server, cli; print(mcp_server.registered_tool_names()); print(cli.cli_command_names())"
['ingest', 'forecast', 'decide', 'paper', 'review', 'retrain', 'backtest', 'propose_skill', 'ledger_query', 'worldview_get', 'worldview_flag', 'fund_request']
['ingest', 'forecast', 'decide', 'paper', 'review', 'retrain', 'backtest', 'propose-skill', 'ledger query', 'worldview get', 'worldview flag', 'fund request']
```

The underscore mapping is now an asserted, stated deviation rather than an untested accident, and the
two stale numbers are corrected (27 rules; the test count, which I re-ran as 565 and which `main` also
states).

---

### F18 — the dry-run fee floor is the maker rate

**Verdict: still broken, now documented rather than hidden.**

```
$ .venv/bin/python -c "... config.json ..."
fee: 0.004 order_types: {'entry': 'limit', 'exit': 'limit', 'emergency_exit': 'market',
                         'force_entry': 'limit', 'force_exit': 'limit', 'stoploss': 'market', ...}
tier_1 maker_pct=0.4 taker_pct=0.8
```

Round 1's exact state survives: one flat venue fee at the maker rate while the same config declares
stops and emergency exits as market orders. What changed is honesty — `venue_fee_optimism()` exposes
the gap and a test pins it — and the **ledger** now prices each fill at its own order type's rate.

That last part is a new mismatch rather than a fix, and it is the answer to the brief's question about
the two-rate accounting. The ledger is not pricing what the venue charges:

```
$ .venv/bin/python - <<'EOF'
fee_pct market/emergency_exit: 0.8
fee_pct post_only_limit/entry: 0.4
fee_for(2635.74, 0.01504526, market, emergency_exit): 0.317243
notional: 39.655394
the venue's own fee on the live trade (fee_open from the venue status): 0.004
```

The same emergency exit is charged **0.317243** in the ledger and **0.158621** by the venue (0.4% of
39.655), a factor of two. So `fee_paid` in the ledger is a modelled number, not the venue's, and the
venue's simulated P&L is optimistic by 0.4% on every stop. Both numbers are wrong in opposite
directions and nothing reconciles them. B9.

---## Part B — new findings against the fixes

Severity is judged against the same bar as round 1: **blocker** stops the checkpoint, **high** means a
claimed acceptance criterion is false or a safety invariant is unenforced, **medium** is a real
contained defect, **low** is hygiene or precision. Each finding names the file and line, the command I
ran, its real output, the spec line it violates, and a suggested fix. Nothing here repeats a round-1
finding; those are in Part A.

Ranked index:

| # | Finding | Severity |
|---|---|---|
| B1 | A `/flat` whose venue read times out closes nothing and reports a clean cycle | **blocker** |
| B2 | Nothing in production can raise a halt: `/flat` has no trigger and the automatic halts are never driven | **high** |
| B3 | Two processes cannot open the ledger at the same instant (`Conflicting lock is held`, uncaught) | **high** |
| B4 | A total venue failure no longer marks the cycle degraded: `errors: 3, degraded: False`, exit 0 | **high** |
| B5 | Strategy-level refusals (`trim_refused`, `add_refused`) never reach the cycle summary | medium |
| B6 | The Ontario cap fails open on a coin the (non-empty) context mapping does not name | medium |
| B7 | The staleness rule rejects an *unknown* age under a declared limit, and the loop's own age is always zero | medium |
| B8 | Outcomes are measured from the ledger's write clock, so a replay/backfill cannot produce a historical series | medium |
| B9 | The ledger's two-rate fee does not match what the venue charges (2x on market orders) | medium |
| B10 | An unfilled exit records the still-open trade's amount, rate and a fee as if it had filled | medium |
| B11 | An exit resolves its target from the card's coin, not from the venue's open position | medium |
| B12 | The verdict-dedupe only collapses identical payloads; a conflicting repeat still writes two rows for one card | low |
| B13 | The `reduction_cap` shrink is unobservable for `exit`, whose order is a full close whatever size is approved | low |
| B14 | The soak's cycle log is the repo's default log path, so dev and test runs append to the acceptance evidence | low |
| B15 | The quota journal is a second durable store, grows one line per refusal, and no tool can query it | low |

---

### B1 — BLOCKER: a `/flat` whose venue read times out closes nothing and reports a clean cycle

**File/lines**
- `agentoquant/execution/paper.py:302-306` — the close list is built from
  `orders.open_positions() or [{"coin": coin} for coin in switches.status().positions_to_close]`.
- `agentoquant/execution/order_manager.py:1014-1027` — `OrderManager.open_positions` catches
  `Exception` and returns `[]`.
- `agentoquant/execution/order_manager.py:526-532` — `_FreqtradeDryRunTransport._open_trades` catches
  `Exception` and returns `[]`.

**What I did.** Ran a `/flat` end to end twice against the real loop and a real `KillSwitch` whose
state file a *fresh process* restored: once with a healthy position read, once with the read failing
the way this venue's API actually fails (`ReadTimeout`). Then measured how often the live venue
answers that read.

**What I observed.**

```
$ .venv/bin/python /tmp/adv2/f13_checks.py
A /flat, venue healthy: the close reaches the transport
  healthy venue: {"intents": 0, "orders": 1, "closes": 1, "errors": 0, "venue_available": true}
    intents the transport received: [('forceexit', 'exit', 'ETH/USD')]
A /flat, venue read times out: the close is skipped and NOTHING reports it
  venue read failed: {"intents": 0, "orders": 0, "closes": 0, "errors": 0, "refused": 0,
                      "venue_available": true, "venue_error": null}
    intents the transport received: []
order_manager.open_positions() swallows the same exception
  fail_positions=False: open_positions() -> [{'coin': 'ETH', 'pair': 'ETH/USD', 'trade_id': 3, 'amount': 0.015}]
  fail_positions=True: open_positions() -> []
```

```
$ .venv/bin/python - <<'EOF'       (live dry-run venue on localhost:8080, read-only status() calls)
  attempt 0: status ok in 0.08s trades=[('ETH/USD', 3)]
  attempt 1: status FAILED in 10.01s ReadTimeout
  attempt 2: status FAILED in 10.01s ReadTimeout
  attempt 3: status FAILED in 10.01s ReadTimeout
  attempt 4: status FAILED in 10.01s ReadTimeout
  attempt 5: status FAILED in 10.00s ReadTimeout
open trades visible: 1/6 attempts
transport.open_positions() now: []
```

**Why it matters.** This is F13's failure mode surviving the fix. With the halt raised and the venue
read failing, `closes` is empty, no intent is submitted, `errors` is 0, `venue_error` is null,
`degraded` is false and the process exits 0 — while every position stays open. The operator's next
action is predicated on the flat having worked, and nothing anywhere distinguishes this cycle from a
clean one. The condition is not exotic: the venue answered the position read **once in six attempts**
while I was measuring, and `docs/task06_execution_report.md`'s "Not done" 4 already records a
`ReadTimeout` during a 24-cycle burst. Round 1 asked for a control whose failure is loud; this one is
still silent.

**Spec line.** `tasks/todo.md:90` — "*Kill switch (`/flat` and daily halt) closes positions and blocks
new entries*". Here it blocks new entries and closes nothing, and reports success.

**Suggested fix.** Do not read the close list through a swallow-everything getter. Three changes, in
order of value: (1) let the failure be visible — `open_positions()` should raise a named error (or
return a sentinel) rather than `[]`, and `run_cycle` should record `closes_error` and keep
`entries_blocked` true while re-trying next tick; (2) make the halt's own position list authoritative:
`trigger_flat(positions=...)` already persists `pending_closes`, so the close list should be the union
of the venue's open positions *and* the persisted pending closes, never only the venue's answer;
(3) clear `flat` only after `close_all_orders` produced zero plans **and** a venue read that succeeded
— a halt that has not confirmed a flat must stay latched, and its status must say `closes_unconfirmed`.

---

### B2 — HIGH: nothing in production can raise a halt

**File/lines**
- `agentoquant/execution/paper.py:245-252` — the loop builds the switch and reads `halt_state()`.
- `agentoquant/risk/kill_switch.py:378-402` — `KillSwitch.evaluate`, the automatic daily/weekly halt.
- `agentoquant/execution/paper.py:141-171` — `placeholder_context` never sets `daily_loss_used_pct` or
  `drawdown_used_pct`, so the gate's own halt rules see 0.0.

**What I did.** Searched the whole package for a caller of the kill switch's triggers.

**What I observed.**

```
$ grep -rn "trigger_flat|build_kill_switch|kill_switch" --include=*.py agentoquant/ tests/ scripts/ | grep -v risk/kill_switch.py
agentoquant/execution/paper.py:245:    switches = kill_switch or build_kill_switch(limits, ledger=ledger)
agentoquant/execution/paper.py:306:        closes = switches.close_all_orders(targets, ...)
tests/test_execution_bridge.py:1233:    switch.trigger_flat(positions=[{"coin": "BTC"}], cycle_id="flat", actor="telegram:/flat")
scripts/task5_acceptance.py:159:plan = ks.trigger_flat(positions=positions, cycle_id=CYCLE, actor="telegram:/flat")
$ grep -rn "\.evaluate(" --include=*.py agentoquant/ | grep -v risk/kill_switch.py
agentoquant/execution/paper.py:254:    verdict = RiskGate(limits, ledger=ledger).evaluate(card, context)   # the gate, not the switch
```

`KillSwitch.evaluate` has no caller anywhere. `trigger_flat` is called only from a test and the
acceptance script. The daily and weekly halts therefore cannot fire in the running system, and the
gate's own `daily_loss_halt` / `weekly_drawdown_halt` rules cannot fire either, because the loop's
context leaves both counters at their 0.0 defaults.

**Why it matters.** Task 5's acceptance criterion names **two** triggers — "/flat **and daily halt**"
— and the fix wired the consumption of a halt nobody can raise. The soak's three `kill_switch_flat`
cycles in `logs/paper/cycles.jsonl` came from an agent's test burst (`cycle-flat` rows in the same
file), not from the timer: the unattended loop has never been halted and never could be. A daily loss
halt that no code path can trigger is a safety control that exists only in its own unit test.

**Spec line.** `tasks/todo.md:90` — "*Kill switch (/flat and daily halt) closes positions and blocks
new entries*".

**Suggested fix.** Call `switches.evaluate(daily_loss_used_pct=..., drawdown_used_pct=..., positions=...,
cycle_id=cycle_id)` once per cycle before the Risk Gate, and feed the same numbers into the
`PortfolioContext` so the gate's halt rules and the switch cannot disagree; then wire the human path
(`/flat`) to it — the recorded decision is that Hermes relays an inbound command to the project CLI and
that relay writes a human action, so the smallest honest version is an `agentoquant flat` entry point
(or a `--flat` flag on `paper`) that calls `trigger_flat` with the venue's open positions. Until one of
these exists, `docs/phase0_status.md`'s Task 5 line should not claim the criterion is met.

---

### B3 — HIGH: two processes cannot open the ledger at the same instant

**File/lines**
- `agentoquant/ledger/store.py:235-239` — `_connect()` opens a fresh DuckDB connection per operation.
- `agentoquant/ledger/store.py:229-233` — `LedgerStore.__init__` calls `migrate()`, i.e. it *opens* a
  connection on construction, and every write/query opens another.

**What I did.** Three genuinely independent processes (no fork inheritance: each is a `subprocess`
started by a parent that holds no DuckDB handle) each write 40 rows at 1 ms intervals against one
ledger.

**What I observed.**

```
$ .venv/bin/python /tmp/adv2/conc_lock2.py
   p0: ok=40 fail=0 first=
   p1: CONSTRUCT FAILED IOException: IO Error: Could not set lock on file
       "/tmp/adv2/conclock2/ledger.duckdb": Conflicting lock is held in .../python3.11 ...
   p2: CONSTRUCT FAILED IOException: IO Error: Could not set lock on file
       "/tmp/adv2/conclock2/ledger.duckdb": Conflicting lock is held in .../python3.11 ...
```

Two of three independent processes could not even construct a `LedgerStore` while the third was
writing, with `_duckdb.IOException` propagating out of `migrate()` uncaught. (My first attempt at this
test used `multiprocessing`, which forks the parent's open handle and fails for a different reason —
this run is the clean one.)

**Why it matters.** The ledger is about to be shared by design: `docs/phase0_status.md:151-156` records
that the early-signal runner holds `data/ledger.duckdb` for its whole life and asks that this be
settled before the runner becomes a service; the hourly ingest opens the same file on the same hourly
boundary as the paper loop. Today the soak survives only because nothing else opens it. The failure is
also badly shaped: it is an uncaught `IOException` at construction, so the paper loop's `run_cycle`
would die before its own error handling, and the "one hourly snapshot" acceptance criterion would fail
non-deterministically depending on tick alignment.

**Spec line.** `plan.md:70` calls it "the ledger as the single source of truth"; a store that two
production processes cannot open concurrently contradicts the role it is given.

**Suggested fix.** Wrap `_connect()` in a bounded retry with jitter (DuckDB's lock is released as soon
as the short-lived connection closes, so a few retries over ~2 s would absorb the collision), and do
not open a connection in `__init__` at all — `migrate()` should be lazy or explicitly called once at
startup. Longer term, do for the ledger what F15 did for quotas: a small `flock`-guarded write queue,
or an explicit single-writer process. Whatever is chosen, add a test that starts two real processes and
writes from both.

---

### B4 — HIGH: a total venue failure no longer marks the cycle degraded

**File/lines**
- `agentoquant/execution/order_manager.py:1541-1636` — `execute` isolates every intent, so it no longer
  raises for a venue error.
- `agentoquant/execution/paper.py:286-297` — `venue_error` is only set when `orders.execute` raises.
- `agentoquant/execution/paper.py:346` — `"degraded": not venue_available and bool(plan.intents)`.

**What I did.** Ran a real entry cycle whose transport raises the venue's own `ReadTimeout` on every
submit.

**What I observed.**

```
$ .venv/bin/python - <<'EOF'   (entry cycle, transport raises ReadTimeout on every submit)
{'action': 'enter_laddered', 'verdict': 'approved', 'intents': 3, 'orders': 0, 'errors': 3,
 'refused': 0, 'degraded': False, 'venue_available': True, 'venue_error': None, 'ack': 'pending'}
```

**Why it matters.** Per-intent isolation is the right fix for round 1's F4 (one bad intent must not
discard the ladder), but it removed the only signal that reported a venue outage. A cycle in which all
three legs failed to reach the venue now reports `degraded: False`, `venue_error: null` and exit 0;
the only trace is an `errors` counter that nothing reads — the systemd unit checks the exit code, and
`docs/phase0_status.md`'s unattended-run evidence quotes `failures: 0`. An outage and a quiet hold look
the same to every consumer.

**Spec line.** `tasks/todo.md` Task 6's acceptance covers the loop reporting its own state honestly;
`.hermes.md`'s operating rule for this repo is that a cycle's faults are recorded against the cycle
rather than swallowed.

**Suggested fix.** Make `degraded` a derived fact rather than a flag: `degraded = bool(errors) or
(not venue_available and bool(plan.intents))`, and record `venue_error` from the first per-intent
error so the message survives. Then let the soak's own report count degraded cycles.

---

### B5 — MEDIUM: strategy-level refusals never reach the cycle summary

**File/lines**
- `agentoquant/execution/paper.py:320` — `ack = _await_ack(store, cycle_id, ack_wait_s)`.
- `agentoquant/execution/paper.py:335,341` — `refused` counts only `reports` (REST-level), and the ack
  is recorded as the string `"acked"`/`"pending"` without reading its `result`.
- `freqtrade_user_data/strategies/AgentBridgeStrategy.py:376,400,417` — the bridge writes
  `add_refused` / `trim_refused` into the ack, with a reason.

**What I did.** Wrote the ack the strategy writes for a refused trim into a scratch signal directory,
then ran the cycle that reads it.

**What I observed.**

```
$ .venv/bin/python - <<'EOF'
cycle action: trim
summary refused: 0 errors: 0 orders: 0 strategy_paths: 1 ack: acked
the ack the cycle actually read: {'cycle_id': 'adv-refuse', 'acked_at': '...',
  'result': {'action': 'trim_refused', 'pair': 'SOL/USD',
             'reason': 'the trade holds no position to reduce'}}
any summary key mentioning the refusal: ['cycle_id']
```

**Why it matters.** `docs/task06_execution_report.md`'s "Not done" 1 records this, and it is the same
class of defect as F1: the cycle's own record says nothing happened and says nothing about why. Four
of the twelve vocabulary paths run inside the strategy, so for those four the ack is the *only* report
the loop gets — and it discards the payload. A trim that the venue refuses silently, every hour, looks
exactly like a hold.

**Spec line.** `tasks/todo.md` Task 6: orders, fills and fees logged per cycle with the cycle id; a
refused order is not logged at all.

**Suggested fix.** Classify the ack's `result.action` for the known refusal names
(`trim_refused`, `add_refused`, and the venue-refusal shape), count them into a new
`refused_by_strategy` key, and append the reason to the cycle record. It is a five-line change in
`run_cycle` after `_await_ack`.

---

### B6 — MEDIUM: the Ontario cap fails open on a coin the context mapping does not name

**File/lines**
- `agentoquant/risk/gate.py:792-805` — `_ontario_cumulative_cad`: `if not supplied and self.ledger is
  not None: ... return 0.0`.

**What I did.** Evaluated a clean `enter_laddered` on SOL with a non-empty
`ontario_net_buys_cad_12m` mapping that names a different coin.

**What I observed.**

```
$ .venv/bin/python /tmp/adv2/final_checks.py
  seeded fill, ledger-derived total              verdict=approved  rule=None
  context says SOL at 31,000 CAD                 verdict=rejected  rule='ontario_net_buy_cap' final=0.0
  non-empty mapping WITHOUT SOL (fail-open probe) verdict=approved  rule=None
```

**Why it matters.** A caller that supplies a cumulative mapping — which is exactly what a Phase 2
ingest-fed brief would do, and what the fix report's own F9 note predicted — turns the cap off for
every coin the mapping forgets. Unknown is read as zero, which is the same shape round 1 objected to in
the gate's liquidity rule. Nothing in Phase 0 supplies such a mapping, so this is latent, and I state
it as such: the *behaviour* is reproduced, the *exposure* is not currently live.

**Spec line.** `config/risk_limits.yaml:48-52` states the cap is "enforced by the gate while Shahrad is
a Canadian resident"; `tasks/todo.md:542` requires candidates to fail closed when data is missing.

**Suggested fix.** When the context supplies a mapping, a coin absent from it is *unknown*, not zero:
fall through to the ledger-derived total if a ledger is attached, and otherwise fail closed with a
named rule (`ontario_net_buys_unknown`) rather than approving at zero.

---

### B7 — MEDIUM: an unknown snapshot age is rejected under a declared limit, and the loop's own age is always zero

**File/lines**
- `agentoquant/risk/gate.py:763-767` — `_market_data_max_age_s` (the context override).
- `agentoquant/risk/gate.py:274-279` — `MarketContext.as_of` documents "the gate will not invent a
  timestamp: an age it cannot know is not called stale".
- `agentoquant/execution/paper.py:133-142` — `placeholder_context` sets `as_of=now` and
  `market_data_max_age_s=market_data_max_age_s()`.

**What I did.** Evaluated the same clean entry with three snapshot shapes: no declared limit and no
timestamp, a declared limit and no timestamp, and a declared limit with a fresh timestamp.

**What I observed.**

```
$ .venv/bin/python - <<'EOF'
  no declared limit, no as_of                          verdict=approved  rule=None
  declared limit, no as_of (Phase-2 partial snapshot)  verdict=rejected  rule=stale_market_data
  declared limit, as_of=now                            verdict=approved  rule=None
```

**Why it matters.** Two halves of the same defect. (a) The gate's stated principle — an age it cannot
know is not stale — holds only when the caller declares nothing; the moment a caller declares a limit
(a Phase 2 brief that knows its cadence), *missing* timestamps become a hard reject for every coin in
the snapshot. That is a false-reject generator aimed at exactly the caller the rule was written for.
(b) In the loop it is the opposite: `as_of=now` is the cycle's *decision* moment, not the snapshot's
own timestamp, so the age is identically zero and F11 can never fire in the running system — the fix
is real in the gate and decorative in production. Neither half is a live outage today; both change
behaviour the first time a real snapshot arrives.

**Spec line.** `agentoquant/risk/gate.py`'s own `MarketContext` contract (quoted above), and Task 3's
"every reading carries its own ts" which the brief is supposed to feed through.

**Suggested fix.** Make the rule distinguish *unknown* from *old*: reject only when `as_of` is known and
too old, or when `is_stale` is set — and if the policy is to fail closed on an unknown age, do it with
its own named rule so an operator can tell "my data is stale" from "my data has no timestamp". On the
loop side, stop passing the decision moment as the snapshot's timestamp: the placeholder should carry
the timestamp of the data it actually represents, even if that means a visible fixed age, so the rule
is exercised by something.

---

### B8 — MEDIUM: outcomes are measured from the ledger's write clock, so a replay cannot produce a historical series

**File/lines**
- `agentoquant/ledger/store.py:312-320` — the envelope's `ts` is `datetime.now(UTC)` at write time,
  whatever moment the caller passed.
- `agentoquant/ledger/store.py:469-499` — `due_outcomes` selects on `c."ts" <= cutoff`.
- `agentoquant/execution/paper.py` — `run_cycle(now=...)` drives every *decision* from the injected
  moment.

**What I did.** Ran a cycle with an injected `now`, compared the card's stored `ts` to that moment, then
ran the recorder at `now+1h`, `now+2h`, and finally with the card's `ts` backdated (a replay shape).

**What I observed.**

```
$ .venv/bin/python /tmp/adv2/final_checks.py
  real clock now: 2026-09-19T15:32:05.359461+00:00
  the card's stored ts (real clock at write time): 2026-09-19 15:32:05.363501+00:00
  the cycle's own moment (injected now)          : 2026-09-19 15:32:05.359461+00:00
  as_of = now+1.0h: due=1 written=1
  as_of = now+2.0h: due=1 written=1
  and with a backdated card ts (a replay: card ts = two days ago, as_of = one day ago)
  backdated pass: due=2 written=2
```

The stored `ts` is 4 ms *after* the cycle's own moment in production (both come from the real clock, at
different instants), which is harmless. It stops being harmless the moment they are not the same
clock: with `now` injected — a backfill or a replay, which Phase 1 introduces — the card is stamped
*now* while the decision pretends to be historical, and `due_outcomes` then measures the horizon from
the stamp. `docs/phase0_status.md:183-190` flags this as a latent inconsistency "recorded for round 2";
my reproduction shows the consequence, namely that the *only* way to get historical outcomes out of a
replay is the out-of-band `UPDATE` the fix report's deviation 3 admits its own tests use.

**Why it matters.** Production is unaffected today (`now` defaults to the real clock and the recorder
is right to measure from a row's write time). Phase 1's replay/backfill is exactly where it bites: a
backfill run today would stamp every replayed card with today's date, so its "+1h outcome" is written
the instant it is created and its P&L horizon is meaningless — silently, with a plausible-looking row.

**Spec line.** `tasks/todo.md` Task 2's ledger is the decision record; `tasks/schema_scaffold_addendum.md`
section 4's `outcome` shape carries `horizon: (1h | 4h | 24h | exit)` — a horizon relative to the
*decision*, not to the write.

**Suggested fix.** Let the caller supply the envelope timestamp (`LedgerStore.write(..., ts=...)`,
defaulting to the real clock so every existing call site is unchanged) and have `run_cycle` pass its own
`moment`. Then `due_outcomes` measures a real horizon in both production and replay, and the
backdate-by-UPDATE test helper disappears.

---

### B9 — MEDIUM: the ledger's two-rate fee does not match what the venue charges

**File/lines**
- `agentoquant/execution/order_manager.py:752-766` — `fee_pct`, taker for `order_type in
  TAKER_EXECUTION_TYPES or purpose in {stop_loss, emergency_exit}`.
- `freqtrade_user_data/config.json:7` — `"fee": 0.004`, one flat rate for every simulated fill.
- `agentoquant/execution/freqtrade_strategy.py:128-158` — `_assert_venue_fee_not_flattering` accepts
  the maker floor and documents the exposed optimism.

**What I did.** Priced the same emergency exit both ways: through the manager (what the ledger writes)
and against the fee the venue actually charges on its own live trade.

**What I observed.**

```
$ .venv/bin/python - <<'EOF'
fee_pct market/emergency_exit: 0.8
fee_pct post_only_limit/entry: 0.4
fee_for(2635.74, 0.01504526, market, emergency_exit): 0.317243
notional: 39.655394
the venue's own fee on the live trade (fee_open from the venue status): 0.004
```

The ledger charges **0.317243** for a fill the venue charges **0.158621** (0.4% of 39.655). The venue
under-charges relative to reality (a real taker stop costs 0.8%) and the ledger over-charges relative
to the venue, by exactly the same factor, and nothing reconciles them. So `fee_paid` is a model, not
the venue's number, and any P&L that mixes the two is wrong in a direction that depends on which one it
read.

**Spec line.** `tasks/schema_scaffold_addendum.md:287` defines `fee_paid` as the fee on the execution;
`config/fee_tiers.yaml`'s Tier 1 note says "Market orders are reserved for stops and emergency exits,
where the taker fee is the price of getting out."

**Suggested fix.** Pick one source of truth for a fee and state it. The cleanest is to let the venue be
authoritative for venue fills (read `fee_open`/`fee_close` back with the trade, as the write-back
already reads `open_rate`) and fall back to `config/fee_tiers.yaml` only when the venue does not report
one — then `fee_paid` is the fee that was charged, and the two-rate model is a fallback rather than a
parallel truth.

---

### B10 — MEDIUM: an unfilled exit records the still-open trade's amount, rate and a fee

**File/lines**
- `agentoquant/execution/order_manager.py:645-677` — `_trade_report` fills `fill_qty`/`fill_price` from
  the *still-open* trade and returns `status=unfilled_timeout`.
- `agentoquant/execution/order_manager.py:1644-1690` — `_record` computes `fee_paid` unconditionally
  from whatever `fill_price`/`fill_qty` carry.

**What I did.** Ran a `/flat` close against the live dry-run venue (write call intercepted, so no order
was placed) and read the execution row it wrote.

**What I observed.**

```
$ set -a; . ~/.config/agentoquant/freqtrade.env; set +a; .venv/bin/python /tmp/adv2/venue_f13_f4.py
  cycle: {"action": "trail_stop", "verdict": "approved", "intents": 1, "orders": 1, "closes": 1, "errors": 0}
  execution rows: [{'cycle_id': '13Z-flat-real', 'status': 'unfilled_timeout',
                    'fill_price': 2635.74, 'fill_qty': 0.01504526, 'fee_paid': 0.317243...}]
  calls the client received: [('forceexit', {'tradeid': 3, 'ordertype': 'market', 'amount': None})]
```

The exit was submitted; the trade is still open; the row nevertheless carries a fill price, a fill
quantity and a fee, under a status that says it did not fill. The fee is also charged again on every
retry of the same position.

**Why it matters.** Round 1's F1 was "a filled order recorded as unfilled". This is its mirror: an
unfilled order recorded with fill fields and a cost. The row is internally contradictory — which of
`fill_price`/`fee_paid`/`status` a consumer believes changes the answer — and a position that is closed
and re-opened repeatedly by retries accumulates fees it never paid. Downstream hit-rate and cost
accounting built on `execution` sees a fill that did not happen.

**Spec line.** `tasks/schema_scaffold_addendum.md:287`: `status: str (filled | partial |
unfilled_timeout | cancelled)` alongside `fill_price: Optional[float]` and `fee_paid: Optional[float]`
— the fields are meant to agree with the status.

**Suggested fix.** In `_record`, derive the fill fields from the status: `fill_price`/`fill_qty`/`fee_paid`
only when the status is `filled` or `partial`, and null otherwise (keeping the observed trade value in a
separate, clearly-named field such as `position_open_rate` if it is useful). Charge the fee when the
venue reports the close, which the write-back can only know by reading the trade after it closes.

---

### B11 — MEDIUM: an exit resolves its target from the card's coin, not from the venue's open position

**File/lines**
- `agentoquant/execution/order_manager.py:1224-1252` — `_full_exit` uses `request.pair`, built from
  `coin` (`pair_for(coin)`), and calls `forceexit` with `amount=None`.
- `agentoquant/execution/order_manager.py:513-525` — `open_trade_id(pair)` only ever looks up the pair
  it was handed.
- Contrast `agentoquant/execution/paper.py:302-306` — the kill-switch path is the one place that asks
  the venue what is open.

**What I did.** Ran the placeholder's own exit cycle against the live venue, whose only open position is
ETH/USD, and read the venue's state afterwards.

**What I observed.**

```
$ .venv/bin/python /tmp/adv2/loop_checks.py
  placeholder universe: ('BTC', 'ETH', 'SOL')
  exit cycle summary: {"action": "exit", "verdict": "approved", "intents": 1, "orders": 0,
                       "refused": 1}
  venue's own open trades: [('ETH/USD', 3, True)]
```

The live soak shows the same thing twice, in its own cycle log:

```
2026-09-19T15:00:23  2026-09-19T15Z-0001  exit  approved  refused 1
2026-09-19T15:08:48  2026-09-19T15Z-0001  exit  approved  refused 1
```

and `docs/phase0_status.md`'s own quote of the refusal:
`no_open_position_for_pair: forceexit needs a trade id and the venue holds no open trade for 'BTC/USD'`.

**Why it matters.** Refusing rather than inventing a trade id is right, and honest reporting instead of
a `TypeError` is the F4 fix working. But the consequence is that an `exit` card whose coin does not
match the venue's open position is a **no-op that looks like a decision**: the position stays open, the
cycle reports a refusal with a rule nobody watches, and the loop moves on. In Phase 0 that is
scaffolding (the placeholder's coin rotates independently of what the venue holds), and the parent is
right that it needs a verdict one way or the other. My verdict: **the placeholder mismatch is
scaffolding, the resolution logic is a real defect** — because nothing reconciles "the card says exit
BTC" with "the venue holds ETH", and the only path that does reconcile (the kill switch) is the one no
production trigger can reach (B2). A production exit that names a coin with no position should be a
loud, named refusal that also reports *what is open* so an operator can see the disagreement.

**Spec line.** `tasks/todo.md` Task 6's acceptance requires every vocabulary action to have a working
execution path; `.hermes.md`'s rule for this repo is that the loop records its own faults honestly.

**Suggested fix.** On a `no_open_position_for_pair` refusal for `exit`, include the venue's open pairs
and their trade ids in the refusal detail, and add a check at the top of the exit path: if the card's
coin is not among the venue's open positions but the venue holds exactly one position, that is a
disagreement worth surfacing (a warning counter on the cycle) rather than silently refusing.

---

### B12 — LOW: the verdict dedupe only collapses identical payloads

**File/lines** `agentoquant/ledger/store.py:370-398` (`_dedupe_on_natural_key`), `:168-170`
(`STAGE_NATURAL_KEYS`).

**What I did.** Wrote one verdict twice, then a conflicting one, then the first payload again.

**What I observed.**

```
$ .venv/bin/python - <<'EOF'
ids: True <id-A> <id-C> <id-A>
rows: [{'verdict': 'approved', 'rule_fired': None},
       {'verdict': 'rejected', 'rule_fired': 'kill_switch_flat'}]
count: 2
```

**Why it matters.** The loop's two writes are the same object, so production is fixed (Part A, F2).
But the *property* round 1 named — "a stage written twice per cycle makes `COUNT(*) ... GROUP BY
cycle_id` wrong" — still holds whenever two evaluations of one card disagree, and the fix report says
the gate's `_card_id` can fall back to `selected_proposal_id`, which makes a shared card id between
genuinely different decisions a real possibility. Deliberate and argued, so this is a stated residual
rather than a defect; but a reader of the status doc's "F2 fixed" line would not know.

**Spec line.** `tasks/todo.md` Task 2's per-cycle stage ledger, which the status doc claims is
countable.

**Suggested fix.** Record the boundary where a reader will find it — one line in
`docs/phase0_status.md` beside F2 saying the dedupe collapses identical repeats only, and that a
conflicting repeat under one card id is a distinct decision with its own row.

---

### B13 — LOW: the `reduction_cap` shrink is unobservable for `exit`

**File/lines**
- `agentoquant/risk/gate.py:599-619` — `_evaluate_reducing`, the new `reduction_cap`.
- `agentoquant/execution/order_manager.py:1244-1252` — `_full_exit` calls `forceexit` with
  `amount=request.amount`, which the loop leaves `None`.
- `agentoquant/execution/paper.py:271-281` — `orders.plan(...)` passes neither `amount` nor
  `position_amount`.

**What I did.** Traced what the exit plan actually places and compared it with what the gate shrinks.

**What I observed.** The gate can shrink an `exit` from 3.0% to, say, 1.0% (`shrunk reduction_cap`),
while the placed order is `forceexit(tradeid, ordertype, amount=None)` — a full close of the whole
trade, because neither `amount` nor `position_amount` is ever supplied. Only the min-size pre-check
changes.

**Why it matters.** The new rule can only ever act as a *reject* on `exit` (position 0 in the context)
and never as a real bound, because the executor does not consume the size it shrinks. That is not a
safety hole — a shrink to a lower number cannot enlarge anything, and a full close is the conservative
direction — but it means the "0 of 12 pass through" claim rests partly on a rule whose shrink arm is
inert for one of the two actions it guards, and the same is true of `trim` if the cast ever drops
`position_amount`. It also means a *rejected* exit is the one way an exit signal can be swallowed: an
exit on a coin the context reports at 0% is refused outright rather than closed.

**Spec line.** `.hermes.md`'s "the Risk Gate can only shrink or reject" is satisfied; the implied
requirement that a shrink *means* something to the executor is not.

**Suggested fix.** For `exit`, bound the *order* rather than the card: pass
`amount = position_amount * final_size_pct / position_size_pct` (or refuse `exit` on a position the
context cannot see, with a named rule) so the verdict's number reaches the venue.

---

### B14 — LOW: the soak's cycle log is the repo's default path, so dev and test runs append to the acceptance evidence

**File/lines** `agentoquant/execution/paper.py:76-95` — `log_dir()`/`cycle_log_path()`; every
`run_cycle(...)` without an explicit `log_path` writes there.

**What I did.** Read the soak's own log and counted its cycle ids.

**What I observed.**

```
$ .venv/bin/python - <<'EOF'      (logs/paper/cycles.jsonl from the main clone)
total 22
duplicate cycle ids: {'2026-09-19T13Z-0001': 2, 'cycle-flat': 3, 'cycle-dead': 3,
                      '2026-09-19T15Z-0001': 2}
2026-09-19T13:00:00+00:00  cycle-flat  enter_laddered rejected kill_switch_flat orders 1 closes 1
2026-09-19T13:00:00+00:00  cycle-dead  enter_laddered approved None            orders 0 closes 0
2026-09-19T13:38:12+00:00  2026-09-19T13Z-0001 add approved ...      # a burst, four rows, one timestamp
```

**Why it matters.** The file the checkpoint's "a trivial strategy runs the full hourly loop" evidence
is read from has no isolation: `cycle-flat`/`cycle-dead` are acceptance-script and test artifacts, and
the 13:38 burst wrote four cycles under the soak's own cycle ids with one identical `ran_at`. Counting
rows in this file therefore over-reports the soak. The ledger has the same property (the 13:38 rows are
in `decision_card`), though the ledger at least can be told apart by timestamp.

**Spec line.** `docs/phase0_status.md`'s evidence table reads this file as the unattended run's record.
I should also disclose that my own reproductions used a scratch ledger but no `log_path`, so rows from
my runs are in this file too — I did not write to the soak's ledger or signal dir, but I did append to
its cycle log. That is the defect demonstrating itself.

**Suggested fix.** Give the log path an environment override (the pattern already exists for the ledger
and the call log — `AGENTOQUANT_LEDGER_PATH`, `AGENTOQUANT_CALL_LOG`) and set
`AGENTOQUANT_PAPER_LOG` in the systemd unit and in test fixtures; then a soak's log contains only soak
ticks.

---

### B15 — LOW: the quota journal is a second durable store, growing without bound and invisible to the ledger

**File/lines** `agentoquant/data/quota_manager.py:116,193-204,319-380`; default path
`data/quota_journal.jsonl`.

**What I did.** Read the journal's role against the plan, and measured its growth under exhaustion.

**What I observed.**

```
$ .venv/bin/python /tmp/adv2/quota_checks.py
  {"workers": 4, "granted_across_processes": 15, "refused_across_processes": 145,
   "journal_lines": 160, "journal_lines_that_are_calls": 15}
  default journal path: /home/shahrad/work/agentoquant-wt/review-round2/data/quota_journal.jsonl
  the soak's own journal exists: True
$ grep -n "source of truth" plan.md
70:- Champion/challenger for models; weekly embargoed Reflector ...; the ledger as the single source of truth; ...
```

**Why it matters.** The journal is the right host for this data — F15's fix report gives the mechanical
reason (DuckDB's lock makes a ledger-resident budget unshareable by the two processes that must share
it, which B3 shows is true) — so I am not asking for it to move. Two smaller consequences are real:
one exhausted source wrote **145 lines** in a minute of retries with no pruning and no rotation (reads
only look at the last 20 000 lines, so the file grows while the view stays bounded), and the budget is
invisible to the only tool the plan calls the source of truth: `agentoquant ledger query` cannot see
it, so a reviewer asking "what did this cycle spend" has to know about a second file.

**Spec line.** `plan.md:70` — "the ledger as the single source of truth". Also the recorded decision
that quota spending is metered and reported.

**Suggested fix.** Two changes, both small: name the journal's location in the ledger's own report/CLI
(it is already in `ingest`'s report note — add it to `ledger query`'s output or a `quotas` named query)
and add a refusal-throttle so a persistently exhausted source writes one refusal line per window rather
than one per attempt.

---