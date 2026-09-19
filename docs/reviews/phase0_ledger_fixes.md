# Phase 0 ledger fixes — F3, F2, F9

Repair work for three findings of the Phase 0 adversary review (`docs/reviews/phase0_adversary.md`),
on branch `fix/outcomes-ledger` in worktree `~/work/agentoquant-wt/fix-outcomes`, branched from
`main` at `4fedfc4`. Scope was deliberately held to `agentoquant/ledger/**`, `agentoquant/scheduler.py`
and new files under `tests/`: `agentoquant/execution/paper.py`, `agentoquant/execution/order_manager.py`
and `agentoquant/risk/**` are owned by three other agents in this wave, so nothing in them was touched.

```
Task             Phase 0 ledger findings — F3 (no outcome writer), F2 (two verdict rows per cycle),
                 F9 (Ontario net-buy cap cannot bind)
Branch:          fix/outcomes-ledger      Worktree: ~/work/agentoquant-wt/fix-outcomes
Base:            main at 4fedfc4
Status:          partial (F3 fixed, F2 fixed, F9 partially fixed - the residual is F1, owned elsewhere)

Files touched:
  agentoquant/ledger/outcomes.py                  (new) the outcome stage's writer
  agentoquant/ledger/store.py                     natural-key dedupe for risk_gate_verdict
  agentoquant/ledger/__init__.py                  export the recorder, document the module
  agentoquant/scheduler.py                        run_outcome_pass() + `--outcomes` on the timer
  tests/test_outcome_recorder.py                  (new) 11 tests
  tests/test_ledger_verdict_dedupe.py             (new) 8 tests
  tests/test_ontario_net_buy_cap.py               (new) 9 tests
  docs/reviews/phase0_ledger_fixes.md             (new) this report

Commands run (all from the worktree, `.venv/bin/python` = Python 3.11.16):
  .venv/bin/python -m pytest -q                    262 passed, 1 skipped in 101.86s
  .venv/bin/ruff check .                           All checks passed!
  .venv/bin/python -m pytest -q tests/test_outcome_recorder.py tests/test_ledger_verdict_dedupe.py
                                                   19 passed
  .venv/bin/python -m pytest -q tests/test_ontario_net_buy_cap.py
                                                   9 passed
```

---

## F3 — the `outcome` stage has no writer: **fixed**

**Reproduced first.** On `main` (`4fedfc4`), in a worktree at `/tmp/fixout_base`:

```
$ grep -rn "write_outcome|due_outcomes" --include=*.py agentoquant/ | grep -v ledger/store.py
agentoquant/ledger/__init__.py:12:  ``cycle_records``, ``cost_by``, ``due_outcomes`` and ``write_outcome``.
```

The only hit is a docstring line. Nothing in the package called `due_outcomes` or `write_outcome`, so
the stage had no writer at all. One real cycle through the real loop, then the ledger inspected three
hours later:

```
=== the cycle that ran (3 hours ago) ===
{'cycle_id': '2026-09-19T11Z-0001', 'action': 'trim', 'verdict': 'approved', 'intents': 1, 'orders': 0}

=== F3: no outcome writer ===
outcome rows in the ledger: 0
  due for 1h at now: []
  due for 4h at now: []
  due for 24h at now: []
  cards in the ledger: 1
```

**Fix.** `agentoquant/ledger/outcomes.py` (new): `record_due_outcomes(store, as_of=..., horizons=...,
price_lookup=..., dry_run=...)`, a self-contained, idempotent pass that

- asks `LedgerStore.due_outcomes(horizon, as_of)` which `(card, horizon)` pairs have elapsed and are
  not yet written, so a second run in the same hour writes nothing and a run after downtime backfills
  exactly the horizons that elapsed while it was down;
- covers executed, rejected and vetoed cards alike (`due_outcomes` already excluded only `hold`), so
  veto value and Adversary accuracy have counterfactual ground truth;
- writes `still_open` from the ledger's own evidence (a non-null `fill_price` and `fill_qty` is the
  signal, the same rule the Ontario net-buy query uses; an `exit` card is never still open, a
  rejected or vetoed card opened nothing);
- fills `pnl_pct` / `pnl_abs` / `realized_up` when the caller supplies a `price_lookup`, and writes
  honest nulls when it does not - computing market prices is Task 7's and Task 12's job, and Phase 0's
  hourly loop has no ingest, so a null PnL with the horizon recorded is the truthful row. Before the
  fix there was no row to be null in.

Entry points: `python -m agentoquant.ledger.outcomes [--as-of ISO] [--horizon H] [--price COIN=VALUE]
[--prices file.json] [--dry-run] [--json] [--ledger path]`, and the timer hook
`agentoquant.scheduler.run_outcome_pass()` / `python -m agentoquant.scheduler --outcomes`.

**Evidence after the fix** (same scratch-ledger shape as the reproduction):

```
=== one cycle through the real loop ===
{'cycle_id': '2026-09-19T11Z-0001', 'action': 'trim', 'verdict': 'approved', 'intents': 1}
outcome rows before the pass: 0
=== python -m agentoquant.ledger.outcomes ===
outcome pass at 2026-09-19T14:16:42.374089+00:00: 1 row(s) due, written; 1h=1, 4h=0, 24h=0
=== second run (idempotent) ===
outcome pass at 2026-09-19T14:16:42.509976+00:00: 0 row(s) due, written; 1h=0, 4h=0, 24h=0
outcome rows: 1
=== +4h pass with a price source (SOL at 153.00) ===
outcome pass at 2026-09-19T15:16:31.563751+00:00: 1 row(s) due, written; 1h=0, 4h=1, 24h=0
   {'horizon': '4h', 'pnl_pct': 2.0, 'pnl_abs': 0.432, 'realized_up': True, 'still_open': True}
```

**Tests, and what they do on the old code.** `tests/test_outcome_recorder.py`, 11 tests: a past cycle
gets its due rows exactly once (three consecutive passes, one row), a horizon 59 minutes old is not
written while the same card at +1h01 is, a 30-hour-old card is backfilled across all three horizons,
executed/rejected/vetoed cards all get a row with the right `still_open`, a null fill records honest
nulls, a price source fills the PnL, `hold` is never due, the `exit` horizon and unknown horizons are
refused, and both entry points are driven. Against the pre-fix tree the whole module cannot even be
imported, which is the finding stated as a test failure:

```
=== base: F3 tests ===
ERROR tests/test_outcome_recorder.py
E   ImportError: cannot import name 'outcomes' from 'agentoquant.ledger'
1 error in 0.22s
```

**Wiring the hourly loop still needs (the explicit TODO).** One line in
`agentoquant/execution/paper.py` (not edited here - the execution agent owns that file):

1. add to the imports: `from agentoquant.ledger.outcomes import record_due_outcomes`
2. inside `run_cycle`, after the orders are submitted and before `append_cycle_log(summary, ...)`:

```python
    record_due_outcomes(ledger, as_of=moment)
```

That is the whole change: the recorder reads the ledger rather than the cycle, so it needs no state
from the loop, no signature change and no new argument. Until it lands, the same pass is available to
the timer (`python -m agentoquant.scheduler --outcomes`) and by hand. It is safe to add the line twice
or to run it on a schedule as well: the second call in the same hour writes nothing.

---

## F2 — every cycle writes two `risk_gate_verdict` rows: **fixed**

**Reproduced first.** One real cycle, on `main`:

```
=== F2: risk_gate_verdict rows per cycle ===
('2026-09-19T11Z-0001', 2)
total verdict rows: 2
```

**Fix.** `agentoquant/ledger/store.py`: `STAGE_NATURAL_KEYS` names `(cycle_id, decision_card_id)` as
`risk_gate_verdict`'s key, and `LedgerStore.write` returns the stored record's id instead of inserting
when a record with that key already holds an **identical** payload. The loop's second write (the same
verdict object the gate returned) is therefore a no-op that reports the gate's `record_id` in the cycle
summary; a cycle now writes one verdict row and `COUNT(*) ... GROUP BY cycle_id` is no longer doubled.

**The boundary, stated.** The dedupe collapses identical repeats only. A *conflicting* payload under
the same key is still written, because `decision_card_id` is not a unique key in practice: the gate's
`_card_id` falls back to `selected_proposal_id` when the card has not been stored yet, and the
adversarial battery legitimately evaluates several cards that share one proposal id in one cycle.
Merging those would drop a decision's verdict, which is worse than the double count being fixed. I
found this by making the first version of the fix raise on a conflict and watching
`tests/test_risk_gate_adversarial.py::test_reductions_and_hold_are_never_blocked_by_a_halt` fail with
`a record for {'cycle_id': ..., 'decision_card_id': 'PROPOSAL-0001'} already exists with a different
payload` - the failure is recorded here because it is the reason the fix is scoped the way it is.

**Evidence after the fix.**

```
verdict rows for that cycle: [{'cycle_id': '2026-09-19T11Z-0001', 'n': 1}]
```

**Tests, and what they do on the old code.** `tests/test_ledger_verdict_dedupe.py`, 8 tests: the real
loop writes one row for one cycle and one per cycle over two cycles, the gate's write followed by the
loop's write is one row returning the same id, two `evaluate` calls for one card write one row,
identical repeats collapse while conflicting payloads do not, two cards in one cycle keep their own
verdicts, other stages are not deduplicated, and a verdict for the next hour is its own row. Against
the pre-fix tree:

```
=== base: F2 tests ===
FAILED tests/test_ledger_verdict_dedupe.py::test_the_hourly_loop_writes_one_verdict_row_per_cycle
FAILED tests/test_ledger_verdict_dedupe.py::test_two_cycles_write_two_verdict_rows_not_four
FAILED tests/test_ledger_verdict_dedupe.py::test_the_gate_write_then_the_loop_write_is_one_row
FAILED tests/test_ledger_verdict_dedupe.py::test_re_evaluating_one_card_writes_one_row
FAILED tests/test_ledger_verdict_dedupe.py::test_identical_repeats_are_collapsed_but_conflicting_ones_are_not
5 failed, 3 passed in 22.21s
```

---

## F9 — the Ontario net-buy cap can never bind: **partially fixed**

**What is fixed.** The cap's own code path is now proven correct and testable given a real fill, and
both halves of the story are pinned by tests:

- with a real fill (`fill_price` and `fill_qty` non-null) the 12-month total is real and the cap
  **binds**: at or over the limit the gate returns `rejected` with `rule_fired = ontario_net_buy_cap`
  and `final_size_pct = 0.0`; with headroom left it returns `shrunk` with the headroom in percent of
  book value (29,940 of a 30,000 cap and a 900 CAD book leaves 60 CAD = 6.667 percent, and that is
  the returned size);
- the CAD conversion the context carries is applied (a 22,500 USD fill is 22,500 CAD at parity and
  30,825 CAD at 1.37, so the same card is approved at parity and rejected at 1.37);
- exempt coins are reported for audit and never counted; a 400-day-old fill is outside the rolling
  12-month window; `trim` subtracts from the net total (the window is a *net* buy window);
- the **real hourly loop** applies the cap: seeded with one filled buy of the same coin over the cap,
  an hour whose placeholder action is an increasing action on that coin comes back
  `verdict=rejected`, `rule_fired=ontario_net_buy_cap`, `size_pct=0.0`, `orders=0`;
- the control is the same cycle against a ledger whose execution row has null fills: `approved`,
  `rule_fired=None`.

**What is not fixed, and why.** The finding's root cause is F1 - the null-fill write-back in
`agentoquant/execution/order_manager.py`, which is explicitly owned by another agent and was not
touched. So on real data today the cap is still inert, and this report does not claim otherwise:

```
=== F9: Ontario net-buy cap ===
execution rows from the live-shaped loop: 0
execution rows with a non-null fill: 0
net buys, null-fill ledger: {}
net buys, one real fill (150 USD x 100): {'SOL': 15000.0}
```

**Will the cap bind once F1 lands? Yes - I checked the whole path, not just the SQL.** The loop
constructs `RiskGate(limits, ledger=ledger)` with the ledger attached (`paper.py:226`) and passes no
`ontario_net_buys_cad_12m`, so the gate's ledger-derived branch in `_ontario_cumulative_cad` is the
live one; `placeholder_context` sets no such mapping either. The two loop-level tests above exercise
exactly that path with the fills present, which is the strongest evidence available without editing
the file that owns F1. Two qualifications the F1 agent should carry:

1. the gate fails **open** on an unknown coin when the context *does* carry a non-empty
   `ontario_net_buys_cad_12m` mapping that lacks that coin (`gate.py:542-555` returns 0.0). Nothing in
   Phase 0 supplies such a mapping, so it is latent, but it is the same "unknown read as zero" shape
   the adversary objected to. It is in `risk/gate.py`, which this work did not touch.
2. F10 (the approved size is not the placed size, and `usd_cad_rate` defaults to 1.0) still means the
   CAD figure the cap is measured against is a USD figure in the loop. That is a separate finding.

**Tests.** `tests/test_ontario_net_buy_cap.py`, 9 tests (the six bullets above plus the direct totals
check). Against the pre-fix tree one of them fails - the loop-level one, on F2's double count, since
the rest exercise `risk/gate.py`, which is unchanged by this work:

```
=== base: F9 tests ===
E   At index 0 diff: {'cycle_id': '2026-09-19T14Z-0001', 'n': 2} != {'cycle_id': '2026-09-19T14Z-0001', 'n': 1}
FAILED tests/test_ontario_net_buy_cap.py::test_the_hourly_loop_rejects_a_buy_over_the_cap_when_the_ledger_has_fills
1 failed, 8 passed in 2.08s
```

---

## Deviations

1. **No new CLI command.** `COMMAND_TARGETS` / `COMMAND_SPECS` in `agentoquant/cli.py` are the frozen
   twelve-command surface and `tests/test_cli_mcp_parity.py` pins the list, so the recorder's entry
   points are a module entry point (`python -m agentoquant.ledger.outcomes`) and a scheduler hook
   (`--outcomes`) instead. Smallest deviation that keeps the parity contract intact.
2. **F2's dedupe collapses identical repeats only**, not conflicting ones - reasoning above. This is
   narrower than the review's "make the second a no-op", and the narrowness is deliberate.
3. **Test fixtures backdate `ts` with a direct UPDATE.** `LedgerStore.write` stamps `ts` at write
   time, so a test that needs a *past* cycle cannot build one through the public API; the three test
   files do it with one documented helper each (`backdate`). No production code reads a caller-supplied
   timestamp, so this is a fixture concern, not a gap in the store.
4. **`ruff format --check` is not clean, and was not before this work**: 37 files would be reformatted
   at the base commit and 38 now, and the repo has no format gate (no CI workflow, no pre-commit
   config). The lint gate the repo does have, `ruff check .`, is clean. Reformatting `store.py` would
   have buried the two-line fix in unrelated churn, so it was left alone.

## Open questions

1. **F1's owner**: the fill write-back. F9's compliance claim depends entirely on it, and the cap's
   headroom arithmetic is already proven against real fills.
2. **Who writes the `exit` horizon?** The addendum makes it event-driven and the store's own docstring
   says the executor writes it when the position closes. Nothing writes it today; the recorder
   deliberately refuses `--horizon exit` rather than pretending to cover it. That is a Task 6/12
   question, not a ledger one.

## Not done

- The one-line call into `execution/paper.py` (deferred, ownership - the exact line is in F3 above).
- F1, and therefore F9's effect on real data.
- Nothing was changed in `agentoquant/risk/**`, `agentoquant/execution/**` or `agentoquant/data/**`.
