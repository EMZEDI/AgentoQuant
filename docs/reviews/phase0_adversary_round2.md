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

---