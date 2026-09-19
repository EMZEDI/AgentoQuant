# Phase 0 — adversarial review

Reviewer: adversary agent (delegated). Repo under review: `/home/shahrad/work/AgentoQuant`
`main` = `bdd47bd`. This report is written on branch `review/phase0-adversary` in worktree
`~/work/agentoquant-wt/review-phase0`.

Method: read every call site that touches Kraken or freqtrade; diff the implementation against
`tasks/schema_scaffold_addendum.md`; drive the Risk Gate with adversarial proposals against a
scratch ledger; exercise the data layer, ledger and execution bridge with real commands. Findings
are only promoted to "finding" when a command's real output is quoted below. Anything I could not
reproduce is labelled **hypothesis** and says so explicitly.

Severity legend: **blocker** (Phase 0 checkpoint cannot be signed off), **high** (a claimed
acceptance criterion is false or a safety invariant is unenforced), **medium** (real defect,
contained), **low** (spec literalism / hygiene).

Evidence base. The live soak's own artifacts were read read-only:
`/home/shahrad/work/AgentoQuant/logs/paper/cycles.jsonl` (9 cycles), a byte copy of
`data/ledger.duckdb` at `/tmp/adv_ledger_snapshot.duckdb`, and `data/signals/`. No write was made
to the soak's ledger or signal directory; all scratch work used `/tmp` ledgers.

---

## F1 — `execution` ledger rows report filled dry-run orders as `unfilled_timeout` with null fill and fee

**Severity: high** (a claimed acceptance criterion is false: "cost per cycle measured"; and the
ledger's `execution` shape is filled with wrong values, not missing ones)

**Files/lines**
- `agentoquant/execution/order_manager.py:1183-1198` — `_record` maps the venue report onto
  `ExecutionPayload`.
- `agentoquant/execution/order_manager.py:444-446` — `_FreqtradeDryRunTransport.submit` returns
  freqtrade's raw response dict unchanged.

**What I did.** Copied the soak ledger to `/tmp/adv_ledger_snapshot.duckdb` and queried the
`execution` table for all nine cycles.

**What I observed.**

```
('2026-09-19T05Z-0001', 'post_only_limit', 'kraken', None, None, None, 'unfilled_timeout', '01M2W3D783AXSF2NNXVZ72K321')
...
('2026-09-19T13Z-0001', 'post_only_limit', 'kraken', None, None, None, 'unfilled_timeout', '01M2WW1V7XE7KAKDQE3ENXQRST')
```

Nine rows, every one `fill_price=None, fill_qty=None, fee_paid=None, status='unfilled_timeout'`.
The venue's own sqlite for the same window shows the ladder filling (three buy fills at 80,996.6
plus an open stop), and the cycle log agrees the cycles were `"fills": 0, "fees_paid": 0.0` while
`"orders": 1`.

**Why it matters.** `tasks/schema_scaffold_addendum.md` §4 defines the `execution` payload as
`fill_price`, `fill_qty`, `fee_paid`, `status` in `(filled | partial | unfilled_timeout | cancelled)`.
`docs/phase0_status.md` line 16 claims "three real dry-run trades each tagged with its cycle id" and
line 71 lists the null fills as a gap; it is worse than a gap. `order_manager.py:1184-1185` derives
`status` from `report.get("fill_qty")`, and freqtrade's REST response for `forceenter` carries no
`fill_qty` key, so **every** REST-submitted order is unconditionally recorded as a timeout even when
it fills. The checkpoint clause "cost per cycle measured" cannot be satisfied while the only
execution cost row is `fee_paid=None`; any hit-rate or slippage accounting built on this table is
inverted.

**Suggested fix.** Read the fill back from the venue rather than from the submit response: after
`forceenter`, poll `trade` (freqtrade REST `trades` / `trade/<id>`) for `open_rate`, `amount`,
`fee_open` and set `fill_price`/`fill_qty`/`fee_paid`/`status` from it; map `open_rate` to
`fill_price` and `amount` to `fill_qty` at minimum, and treat an unknown status as a new explicit
`unknown` rather than silently defaulting to `unfilled_timeout`. Add a reconciliation pass that
writes the outcome at +1h from the venue, not from the submit ack.

---

## F2 — every cycle writes two `risk_gate_verdict` rows

**Severity: high** (ledger integrity: per-cycle stage counts are doubled, and any query that counts
verdicts double-counts)

**Files/lines**
- `agentoquant/risk/gate.py:647-650` — `RiskGate._verdict` writes the record itself when a ledger is
  attached.
- `agentoquant/execution/paper.py:227-234` — the loop writes the same verdict again.

**What I did.** Grouped the snapshot's `risk_gate_verdict` table by `cycle_id`.

**What I observed.**

```
('2026-09-19T05Z-0001', 2, 'risk_gate', None, 'approved')
('2026-09-19T06Z-0001', 2, 'risk_gate', None, 'approved')
...
('2026-09-19T13Z-0001', 2, 'risk_gate', None, 'approved')
```

18 rows for 9 cycles. `docs/phase0_status.md` line 12 reports "4 cycles with 14/12/13/14 stages" —
those counts were taken from a ledger where the double write happened too, so at least one of them
is inflated by 1 per cycle.

**Why it matters.** `tasks/todo.md`'s Task 2 acceptance is a per-cycle stage ledger; a stage written
twice per cycle makes `SELECT COUNT(*) ... GROUP BY cycle_id` wrong and makes the "every cycle and
every token in the ledger" checkpoint clause unverifiable by counting. It also means the gate is not
idempotent as a side effect: calling `evaluate` twice (e.g. a re-evaluation after a config reload)
writes two verdicts for one decision.

**Suggested fix.** Pick one writer. Make `RiskGate.evaluate` pure (no ledger write) and keep the
write in the loop, or keep the write in the gate and delete `paper.py:227-234`. If both are wanted,
key the write on `(cycle_id, decision_card_id)` and make the second a no-op.

---

## F3 — the `outcome` stage has no writer at all: no +1h / +4h / +24h record is ever produced

**Severity: high** (a required ledger stage and a checkpoint clause are unimplemented, not merely
empty)

**Files/lines**
- `agentoquant/enums.py:92` — `Stage.OUTCOME` is declared.
- No other file in `agentoquant/` references it.

**What I did.** Searched the production package for any writer of the outcome stage, then counted
the table in the live ledger.

**What I observed.**

```
$ grep -rn "Stage.OUTCOME" --include=*.py agentoquant/
agentoquant/enums.py:92:    OUTCOME = "outcome"
```
```
outcome                  0
```

Zero rows, and no code path that could produce one. The same search shows `Stage.HUMAN_ACTION` is
written only by `risk/kill_switch.py:407`, and that file is not reached by the hourly loop.

**Why it matters.** `tasks/schema_scaffold_addendum.md` §4 defines `outcome` with
`horizon: (1h | 4h | 24h | exit)`, `pnl_pct`, `pnl_abs`, `realized_up`, `still_open`, and the ledger
is described as the fourteen-stage decision ledger with one table per `Stage`. The checkpoint clause
"every cycle and every token in the ledger" is false for the outcome stage: a cycle can run to
completion and never produce a single outcome row. Without outcomes, Task 24's reflector and Task 22's
daily review have no ground truth, and the "cost per cycle measured" clause cannot be extended to
realized P&L.

**Suggested fix.** Add the outcome writer Phase 0 is missing: a scheduler-driven pass that, for each
`execution` row, reads the venue's position/P&L at +1h, +4h, +24h and at exit, and writes one
`outcome` record per horizon with `still_open` set honestly. If Phase 0 deliberately defers it, say
so in `tasks/todo.md` and in the checkpoint wording rather than leaving a declared stage unwritten.

---

## F4 — three of the twelve vocabulary actions throw `TypeError` at the venue; `exit` and `cancel_order` are dead

**Severity: high** (the execution bridge's acceptance criterion covers the twelve-action vocabulary;
two actions cannot execute at all and a third's replacement leg cannot either)

**Files/lines**
- `agentoquant/execution/order_manager.py:417-432` — `_rest_kwargs`, which filters kwargs to the
  handler's accepted names and then unconditionally injects `pair` at lines 430-431.
- `agentoquant/execution/order_manager.py:826-849` — `_full_exit` builds
  `{"pair", "amount", "ordertype", "price", "exit_tag"}` for `forceexit`.
- `agentoquant/execution/order_manager.py:1087-1100` — `_cancel_or_amend` builds
  `{"pair", "order_id"}` for `cancel_open_order`.

**What I did.** Built the real intents with the real `OrderManager`, ran them through the real
`_FreqtradeDryRunTransport._rest_kwargs` against a stub whose method signatures are byte-identical to
the installed `freqtrade_client.FtRestClient`, then bound the produced kwargs to the real signature.

**What I observed.**

```
$ .venv/bin/python -c "import inspect; from freqtrade_client import FtRestClient; print(inspect.signature(FtRestClient.forceexit)); print(inspect.signature(FtRestClient.cancel_open_order))"
forceexit (self, tradeid, ordertype=None, amount=None)
cancel_open_order (self, trade_id)
```
```
===  exit path= full_exit intents= 1
   call: forceexit supports: True
   kwargs sent: {'ordertype': 'limit', 'pair': 'BTC/USD'}
   bind: TypeError: missing a required argument: 'tradeid'
===  cancel_order path= cancel_or_amend_resting intents= 2
   call: cancel_open_order supports: True
   kwargs sent: {'pair': 'BTC/USD'}
   bind: TypeError: missing a required argument: 'trade_id'
   call: forceenter supports: True
   kwargs sent: {'price': 100.0, 'enter_tag': 'reprice:2026-09-19T14Z-0001', 'pair': 'BTC/USD'}
   bind: TypeError: missing a required argument: 'side'
```

Three separate defects in one code path: (a) `forceexit` needs a `tradeid` that the manager never
supplies and is handed an unsupported `pair` instead; (b) `cancel_open_order` needs a `trade_id`, and
the intent carries `order_id`; (c) the replacement `forceenter` after a cancel omits `side`, which
`forceenter` requires.

**Why it matters.** This is the root cause of the open gap in `docs/phase0_status.md` line 70
("`trim`, `add`, `exit`, `take_profit_ladder` reported `orders=0`") for the `exit` leg, and it is
worse than `orders=0`: the `TypeError` propagates out of `orders.execute` into the handler at
`agentoquant/execution/paper.py:264-268`, which catches it and marks the **whole cycle** degraded
(`venue_available=False`). A cycle whose only fault was an unsupported cancel would be reported as a
venue outage. `tasks/schema_scaffold_addendum.md` §3 fixes the twelve-action vocabulary and the plan
requires all of it to work; two members cannot.

**Suggested fix.** Resolve the venue's identifiers before building the intent: look the open trade
up by pair (`freqtrade REST 'status'` / `trade`) and put its `trade_id` into `freqtrade_kwargs` as
`tradeid` for `forceexit` and `trade_id` for `cancel_open_order`; stop injecting `pair` into handlers
that do not declare it (`order_manager.py:430-431`); add `side` to the replacement `forceenter`. Add
a test that binds every intent's kwargs to the real `FtRestClient` signature — a signature-binding
test catches all three without a live venue.

---

## F5 — the plan's price levels never reach the venue for any hook-only path, and `take_profit_ladder` can never fire

**Severity: high** (declared but dead: the ladder prices, the stop price and the take-profit target)

**Files/lines**
- `agentoquant/execution/signal_store.py:116-128` — `execution_intent` hardcodes
  `"stop_price": None` and `"target_price": None`.
- `agentoquant/execution/order_manager.py:1140-1155` — an intent the REST client cannot place is
  reported `submitted=False` and its kwargs are dropped; nothing carries them to the strategy.
- `freqtrade_user_data/strategies/AgentBridgeStrategy.py:435-439` — the take-profit exit requires
  `execution.get("target_price")`.
- `agentoquant/execution/order_manager.py:696-727` — `_laddered_entry` computes a per-slice
  `custom_entry_price` for slices 2 and 3.

**What I did.** Read the document the live soak actually published, and the acks the strategy wrote.

**What I observed.**

```
action enter_laddered coin BTC published_at 2026-09-19T13:00:23.983028+00:00
{
 "approved": true,
 "ladder_slices": 3,
 "order_type": "post_only_limit",
 "original_size_pct": 3.0,
 "post_only": true,
 "rule_fired": null,
 "side": "buy",
 "size_pct": 3.0,
 "stop_price": null,
 "target_price": null,
 "time_stop_minutes": null
}
```

and from the cycle log for the same cycle:

```
"intents": 3, "orders": 1, "strategy_paths": 2
```

**Why it matters.** Two consequences, both against stated acceptance criteria.

1. The order manager computes a three-rung ladder (offsets 0.10/0.25/0.40 percent below the
   reference price, `order_manager.py:95` and `696-727`) and a stop price
   (`agentoquant/execution/paper.py:159`), but only the first rung is REST-placeable. Rungs 2 and 3
   are hook-only, so `order_manager.py:1140-1155` drops their `freqtrade_kwargs` on the floor. The
   strategy re-derives the slices from the JSON document with `_stake_for(current_rate, ...)`
   (`AgentBridgeStrategy.py:346`) and freqtrade prices them from its own order book — which is why
   the soak's fills landed at 80,996.6 while the plan asked for 59,940 / 59,850 / 59,760. The
   "post-only ladder below the reference price" is real for exactly one of three slices.
2. `take_profit_ladder` can never produce an exit. `signal_store.py:124` publishes
   `target_price: None`, and `AgentBridgeStrategy.py:436-438` returns an exit only when
   `target` is truthy and `current_rate >= target`. `None` is not truthy, so the branch is
   unreachable by construction. That is the precise root cause of the open gap in
   `docs/phase0_status.md` line 70, and it is not "unproven" — it is impossible.

**Suggested fix.** Carry the plan to the strategy. Either (a) put the price levels into the published
document (`stop_price`, `target_price`, and a per-slice price list) so the hook path reads the same
numbers the manager computed, or (b) drive every slice through the REST force-entry path. Then make
`custom_exit`'s take-profit branch fail loudly when `target_price` is null instead of silently
returning `None`, and add a test that asserts the published document for a `take_profit_ladder`
card carries a non-null `target_price`.

---

## F6 — the only execution evidence in the soak is nine identical `enter_laddered` cycles, all approved

**Severity: medium** (contained: the rotation fix landed after the soak, but the acceptance evidence
quoted in `docs/phase0_status.md` does not support the vocabulary claim)

**Files/lines**
- `agentoquant/execution/paper.py:334-343` — `sequence_for`, the rotation fix.
- `docs/phase0_status.md:16` — "24 consecutive cycles, 0 failures, three real dry-run trades".

**What I did.** Counted the soak's actions, verdicts and orders out of its own cycle log.

**What I observed.**

```
$ wc -l logs/paper/cycles.jsonl
9 logs/paper/cycles.jsonl
action x cycles: {'enter_laddered': 9}
action x orders: {'enter_laddered': 9}
verdicts: {'approved': 9}
```
```
2026-09-19T05Z-0001 enter_laddered 0 2026-09-19T05:49:42.009825+00:00
2026-09-19T06Z-0001 enter_laddered 0 2026-09-19T06:00:23.682005+00:00
2026-09-19T07Z-0001 enter_laddered 0 2026-09-19T07:00:23.651089+00:00
2026-09-19T08Z-0001 enter_laddered 0 2026-09-19T08:00:23.669307+00:00
```

Nine cycles, one action, `"sequence": 0` in every row. The fix commit `c8cc21e` ("make the
placeholder pattern rotate across hourly ticks") is an ancestor of `main`, but `paper.py`'s mtime is
`2026-09-19T13:21` — after the last soak cycle at 13:00 — so the soak predates it and the log is not
evidence about the fix. I verified the fix's arithmetic independently:

```
2026-09-19T05:00:00+00:00 seq= 497165 seq%8= 5 action= hold
2026-09-19T06:00:00+00:00 seq= 497166 seq%8= 6 action= add
2026-09-19T07:00:00+00:00 seq= 497167 seq%8= 7 action= exit
2026-09-19T08:00:00+00:00 seq= 497168 seq%8= 0 action= enter_laddered
2026-09-19T09:00:00+00:00 seq= 497169 seq%8= 1 action= trail_stop
...
```

**Why it matters.** The checkpoint clause "a trivial strategy runs the full hourly loop" is claimed
on the strength of this soak, and the soak exercised one eighth of the placeholder pattern. The
`fills: 0`, `fees_paid: 0.0` and `status: unfilled_timeout` values in all nine rows are F1's defect
surfacing in the summary. Separately, `verdicts: {'approved': 9}` with `rule_fired: null` means the
soak exercised the gate's **approve** path only — the reject and shrink paths have no live evidence
at all, only unit tests (see F12-F16).

**Suggested fix.** Re-run the soak after the rotation fix and report the action histogram, not just
the cycle count, as the acceptance evidence. Require at least one cycle per vocabulary action before
claiming the loop covers it.

---

_Report in progress — remaining sections (data layer, spec literalism, tests that cannot fail,
checkpoint answer) are appended and committed incrementally._

---

## The Risk Gate battery

Run against a scratch ledger (`AGENTOQUANT_LEDGER_PATH=/tmp/adv_gate_battery.duckdb`), never the
soak's. Driver: `/tmp/adv_gate_battery.py`. Headline result — 27 proposals, one per rule plus the
attacks that are supposed to have no rule:

```
case                                                       verdict    rule                                  orig   final
baseline clean entry (SOL, 3%)                             approved   None                                 3.000   3.000
oversized 40% vs 15% position cap                          rejected   daily_turnover_cap                  40.000   0.000
over-concentrated sleeve: A already 95%                    shrunk     sleeve_cap                          10.000   5.000
over-concentrated: existing SOL position 14% + 3% add      shrunk     position_cap                         3.000   1.000
missing stop                                               rejected   missing_stop                         3.000   0.000
taker order for an entry                                   rejected   post_only_required                   3.000   0.000
taker order for stop_loss (allowed)                        approved   None                                 3.000   3.000
illiquid coin (10k 24h volume)                             rejected   min_liquidity                        3.000   0.000
wide spread 1.0%                                           rejected   max_spread                           3.000   0.000
unknown coin: no market state                              rejected   min_liquidity                        3.000   0.000
not tradable pair                                          rejected   not_tradable                         3.000   0.000
over daily turnover cap (24 used + 3)                      rejected   daily_turnover_cap                   3.000   0.000
daily loss halt (3.0 used)                                 rejected   daily_loss_halt                      3.000   0.000
weekly drawdown halt (8.0 used)                            rejected   weekly_drawdown_halt                 3.000   0.000
kill switch flat                                           rejected   kill_switch_flat                     3.000   0.000
venue status maintenance                                   rejected   venue_status                         3.000   0.000
cooldown after 2 consecutive losses (recent)               rejected   cooldown_after_consecutive_losses    3.000   0.000
Ontario: SOL already at 30,000 CAD                         rejected   ontario_net_buy_cap                  3.000   0.000
Ontario: SOL at 29,000 CAD, asking 3% of 900 = 27 CAD      approved   None                                 3.000   3.000
Ontario: BTC is exempt                                     approved   None                                 3.000   3.000
free-cash floor breach (A free cash 5% vs 10% floor)       rejected   free_cash_floor                      3.000   0.000
SEVERITY-5 ADVERSARY OBJECTION OUTSTANDING                 approved   None                                 3.000   3.000
confidence band NO_TRADE                                   rejected   confidence_band_no_trade             3.000   0.000
confidence 40 below sleeve threshold 60                    rejected   entry_confidence_below_sleeve_threshold   3.000   0.000
sleeve C disabled                                          rejected   sleeve_disabled                      3.000   0.000
max concurrent positions reached                           rejected   max_concurrent_positions             3.000   0.000
no size                                                    rejected   missing_size                         0.000   0.000
```

and the enlargement attempts:

```
  size=NaN            verdict=rejected  rule=missing_size                 orig=0.0 final=0.0 never_enlarges=True
  size=negative -50   verdict=rejected  rule=missing_size                 orig=0.0 final=0.0 never_enlarges=True
  size=huge 1e9       verdict=rejected  rule=daily_turnover_cap           orig=1000000000.0 final=0.0 never_enlarges=True
  size=inf            verdict=rejected  rule=daily_turnover_cap           orig=inf final=0.0 never_enlarges=True
  size=zero           verdict=rejected  rule=missing_size                 orig=0.0 final=0.0 never_enlarges=True
```

**Verdict on the enlargement invariant.** I could not enlarge a proposal. NaN, negative, infinite and
1e9 sizes all floor at zero or are rejected, and `gate.py:635-636` clamps `final` into
`[0, original]` with an assertion behind it. The Task 5 acceptance criterion "Gate can only shrink or
reject a proposal, never enlarge it" holds for every input I tried. The seven cases Task 5 names
(oversized, over-concentrated, no stop, taker order, illiquid coin, over the turnover cap, over the
net-buy cap) all reject or shrink. The Ontario rule is sound when it is given a cumulative total —
`cumulative 29,999 CAD -> shrunk rule=ontario_net_buy_cap final=0.1111%`. That much is real.

What the battery did find is in F7 to F12.

---

## F7 — a severity-5 adversary objection blocks nothing; the gate approves it

**Severity: high** (a named system requirement — "no unresolved severity-5 objection remains" — has
no implementation anywhere in Phase 0, and the checkpoint asks specifically whether the gate blocks
its adversarial set)

**Files/lines**
- `agentoquant/risk/gate.py:103-156` — the complete rule list; no rule reads
  `card.strongest_objection`.
- `agentoquant/risk/gate.py:166-178` — `INCREASING_ACTIONS`, the only set that reaches the rules.
- `plan.md:86` — "act only if ... and no unresolved severity-5 objection remains".
- `tasks/todo.md:393` — Task 20 acceptance: "No trade when EV after fees is not positive by margin
  or a severity-5 objection is unresolved".
- `agentoquant/ledger/schema.py:355` — `strongest_objection` is carried on the card and never read
  by the gate.

**What I did.** Attached a severity-5 objection to a card the gate would otherwise approve, then
evaluated it. Separately grepped the whole package for any reader of `strongest_objection`.

**What I observed.**

```
=== severity-5 objection, card carries it ===
verdict: approved rule_fired: None final_size_pct: 3.0 card.strongest_objection: {'severity': 5, 'text': 'the thesis is contradicted by the exchange announcement'}
```
```
$ grep -rn "strongest_objection" --include=*.py agentoquant/ | grep -v schema.py
agentoquant/execution/telegram_bot.py:181:    objection = card.strongest_objection or {}
agentoquant/execution/freqtrade_strategy.py:237:        strongest_objection=None
```
The only production reader renders it into the Telegram message. Nothing acts on it.
`grep -rn "severity" agentoquant/risk/ agentoquant/execution/` returns nothing in `risk/` at all.

**Why it matters.** Being precise about ownership: `plan.md:86` places this rule in the **Judge**
(Task 20), not in Task 5's rule list, and Task 5's acceptance criteria do not name it. So this is not
a Task 5 defect. It is a Phase 0 defect: Phase 0 has no Judge, so the requirement has no
implementation, and my brief's adversarial set includes "a severity-5 objection outstanding" as a
proposal the gate should stop. It does not. The checkpoint clause "the Risk Gate blocks every
adversarial proposal in its test set" is therefore only true of the test set the gate's own author
chose.

**Suggested fix.** Add `RULE_UNRESOLVED_SEVERITY5 = "unresolved_severity5_objection"` to the gate's
reject rules, reading `card.strongest_objection["severity"] >= 5`. It is one comparison, it is
deterministic, and it makes the requirement true regardless of which stage is nominally responsible.
If the decision is that the Judge owns it, record that in `tasks/todo.md` against Task 5 so the
checkpoint question has an answer.

---

## F8 — the gate is a pass-through for seven of the twelve vocabulary actions

**Severity: medium** (the safety rule "No agent may bypass it" is true of five actions, not twelve)

**Files/lines**
- `agentoquant/risk/gate.py:400-403` — `if action not in INCREASING_ACTIONS: return
  self._verdict(..., VERDICT_APPROVED, None, original, original, False)`.

**What I did.** Evaluated every non-increasing action with a card that violates every rule at once:
`size_pct=99`, `coin=None`, `sleeve=None`, `confidence=0`, `band=NO_TRADE`, a severity-5 objection —
against a context with `venue_status="offline"`, `stop_on_exchange=False` and `HaltState(flat=True)`.

**What I observed.**

```
  trim                 verdict=approved  rule=None orig=99.0 final=99.0
  exit                 verdict=approved  rule=None orig=99.0 final=99.0
  set_stop             verdict=approved  rule=None orig=99.0 final=99.0
  trail_stop           verdict=approved  rule=None orig=99.0 final=99.0
  take_profit_ladder   verdict=approved  rule=None orig=99.0 final=99.0
  cancel_order         verdict=approved  rule=None orig=99.0 final=99.0
  hold                 verdict=approved  rule=None orig=99.0 final=99.0
```

**Why it matters.** `gate.py:73-78` documents this as deliberate and the reasoning is defensible for
`trim`/`exit` (a halt should not freeze a position). But the pass-through also covers `set_stop`,
`trail_stop`, `take_profit_ladder` and `cancel_order`, and it returns `final_size_pct = 99.0` for a
`hold`. `agentoquant/execution/paper.py` publishes whatever the verdict says, so a `trim` card with a
nonsensical size is published as `approved` with that size and the bridge acts on it
(`signal_store.execution_intent`). The `.hermes.md` rule "The Risk Gate is deterministic code and can
only shrink or reject a proposal ... No agent may bypass it" reads as covering the vocabulary; it
covers five twelfths of it.

**Suggested fix.** Keep the reduction pass-through for `trim`/`exit` (that is the right call), but
evaluate the size-free management actions through at least the halt, venue-status and
incomplete-card rules, and return `final_size_pct = 0.0` for `hold` rather than echoing the card's
size. If the pass-through is intended, say so in the rule list so a reader can see which actions the
gate actually guards.

---

## F9 — the Ontario net-buy cap can never bind, because F1 leaves every fill null

**Severity: high** (a regulatory limit the plan calls out explicitly is decorative in practice)

**Files/lines**
- `agentoquant/risk/gate.py:329-335` — `ontario_net_buys_cad_12m`'s SQL filters
  `"fill_price" IS NOT NULL AND "fill_qty" IS NOT NULL`.
- `agentoquant/execution/order_manager.py:1186-1198` — the writer that leaves both null (F1).

**What I did.** Ran the gate's own Ontario function against the soak's real ledger.

**What I observed.**

```
ontario net buys from the real soak ledger: {}
execution rows with non-null fill_price: 0
execution rows total: 9
```

**Why it matters.** `config/risk_limits.yaml:48-52` states the cap is "enforced by the gate while
Shahrad is a Canadian resident". It is not enforced: with no non-null fill, the 12-month net-buy
total is permanently 0, so `_ontario_rejection` (`gate.py:531-540`) and `_ontario_headroom_pct`
(`gate.py:587-602`) can never fire on real data. The cap only works against a ledger whose
`execution` rows carry fills — which is exactly the state F1 prevents. The same query is what makes
the F1 defect a compliance issue rather than a reporting issue.

**Suggested fix.** Fix F1's fill write-back, then add a test that runs the full loop against a fake
venue that reports a fill, and asserts `ontario_net_buys_cad_12m` is non-zero afterwards. Until the
fills land, have the gate fail closed on a coin whose cumulative is unknown rather than treating
unknown as zero.

---

## F10 — the size the gate approves is not the size the venue places

**Severity: medium** (the caps are enforced against a denominator the executor does not use)

**Files/lines**
- `agentoquant/execution/paper.py:221-226` — `book_value_cad = load_settings().capital.starting_capital`
  (900.0 CAD, `config/settings.yaml:15`), and `usd_cad_rate` is never set, so it defaults to 1.0
  (`gate.py:260`).
- `freqtrade_user_data/strategies/AgentBridgeStrategy.py:260-292, 361-375` — `custom_stake_amount` and
  `_stake_for` size from `self.wallets.get_total_stake_amount()`, never from the gate's book value.
- `freqtrade_user_data/config.json:12` — `"tradable_balance_ratio": 0.95`.

**What I did.** Compared what the gate approved for the soak's cycles against what the venue actually
staked, from the strategy's own acks.

**What I observed.**

```
$ python3 -c "import json; d=json.load(open('data/signals/acks/2026-09-19T13Z-0001.json')); print(d['result'])"
{'action': 'ladder_slice', 'of': 3, 'pair': 'BTC/USD', 'slice': 3, 'stake': 8.550010091565}
```

Three slices of 8.550010091565 USD = 25.65 USD placed. The gate approved 3.0 percent of a 900.0 book
value = 27.00. The 5 percent shortfall is `tradable_balance_ratio` being applied at the venue and
nowhere in the gate.

**Why it matters.** The gate's `final_size_pct` travels to the venue as a bare percent and is applied
to whatever the wallet holds. Two consequences: (a) the approved notional and the placed notional
already disagree by 5 percent in the soak; (b) the disagreement is unbounded in the other direction —
if the wallet ever exceeds `settings.capital.starting_capital` (profit, or a manual top-up), the
strategy places more than the gate approved and nothing in the venue enforces the 15 percent position
cap or the sleeve cap, because those are only evaluated against the config constant. `paper.py:221`
also reads a CAD figure and the whole chain treats it as USD (`order_manager.py:514-520` divides by
`usd_cad_rate`, which is 1.0).

**Suggested fix.** Make the gate's book value the live balance it claims to use — read it once per
cycle from the venue (read-only `Balance`) and pass it in both to `PortfolioContext.book_value_cad`
and to the strategy's document as an absolute stake, not a percent. Alternatively have the venue
refuse a stake that exceeds the approved notional and publish `approved_stake_usd` alongside
`size_pct`.

---

## F11 — nothing in the gate can detect stale data

**Severity: medium** (a listed attack has no defence: "stale data" is not representable in the gate's
input)

**Files/lines**
- `agentoquant/risk/gate.py:236-271` — `PortfolioContext`. No field carries an age, a timestamp or a
  freshness flag for `markets`.
- `agentoquant/risk/gate.py:223-234` — `MarketContext` has `coin`, `volume_24h_usd`, `spread_pct`,
  `tradable` and nothing else.

**What I did.** Searched the gate's rule names and inputs for any freshness concept, and checked
whether `RawSnapshotPayload.is_stale` (the ledger's own staleness flag) reaches the gate.

**What I observed.**

```
reject: 20 shrink: 3 total: 23
rule names containing stale/objection/age: []
```
```
$ grep -rn "stale\|staleness" --include=*.py agentoquant/risk/ agentoquant/execution/
agentoquant/execution/freqtrade_strategy.py:285:    """The current signal, or ``None`` when nothing is published or it is stale."""
agentoquant/execution/signal_store.py:175:    treated as stale, which fails closed."""
agentoquant/execution/signal_store.py:249:        pipeline cannot leave a stale card live for the bridge.
```

The three hits are all about the *signal document's* age, not the market data the gate reasons about.
`is_stale` exists on `RawSnapshotPayload` (`ledger/schema.py:170`) and is never read by the gate.

**Why it matters.** The gate's liquidity, spread and venue rules are the only things standing between
a stale snapshot and an order, and a stale snapshot with good numbers passes all of them. The plan's
"fail closed on missing data" posture is implemented for *absent* data (`gate.py:494-497` fails
closed when a coin has no market state) but not for *old* data. The `raw_snapshot` stage already
records `is_stale` and `quota_remaining`; the gate does not consume either.

**Suggested fix.** Add `as_of` and `max_age_s` to `MarketContext` (or a single
`context.market_data_age_s`), and a reject rule `stale_market_data` that fires when the age exceeds
one cadence plus a margin. Feed it from the same snapshot the brief uses.

---

## F12 — spec literalism and dead config in the gate's surroundings

**Severity: low**

**Files/lines**
- `config/risk_limits.yaml:14-17` — `positions.sleeve_caps_pct: {A: 100, B: 35, C: 20}`.
- `agentoquant/config_loader.py:355-363` — validated at load time.
- `agentoquant/risk/gate.py:380` — the gate reads caps from `load_sleeves()` instead.
- `config/risk_limits.yaml:10` — `positions.min_concurrent: 3`, validated, never read by the gate.
- `docs/phase0_status.md:15` — "Deterministic Risk Gate (22 rules)".

**What I did.** Grepped for every reader of the two config keys and counted the rule list.

**What I observed.**

```
$ grep -rn "sleeve_caps_pct\|min_concurrent" --include=*.py agentoquant/
agentoquant/config_loader.py:352:    min_concurrent: int = Field(ge=1)
agentoquant/config_loader.py:355:    sleeve_caps_pct: dict[Sleeve, float]
agentoquant/config_loader.py:359:        if self.min_concurrent > self.max_concurrent:
agentoquant/config_loader.py:361:        missing = [s.value for s in Sleeve if s not in self.sleeve_caps_pct]
```
```
reject: 20 shrink: 3 total: 23
```
```
15:| 5 | Deterministic Risk Gate (22 rules), kill switch, funding floor | ...
```

**Why it matters.** `config/risk_limits.yaml` is presented as "the Risk Gate's inputs", and the gate
never reads two of its five `positions` keys. A reviewer who changes `sleeve_caps_pct` in the limits
file — the file named for limits — changes nothing, because the effective caps live in
`config/sleeves.yaml`. That is a silent-failure config trap, not a cosmetic one. Separately, the
phase report's "22 rules" is wrong: the module exposes 23.

**Suggested fix.** Delete `sleeve_caps_pct` and `min_concurrent` from `risk_limits.yaml` (or make the
gate read them and remove the duplicate from `sleeves.yaml` — one source, named once). Correct
`docs/phase0_status.md` to 23 rules, or state which rule is excluded from the count.
