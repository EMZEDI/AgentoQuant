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

_Report in progress — remaining sections (Risk Gate adversarial battery, data layer, spec
literalism, tests that cannot fail, checkpoint answer) are appended and committed incrementally._
