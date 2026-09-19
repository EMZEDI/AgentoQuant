Task 5 (follow-up) — Phase 0 adversary findings F7, F8, F11 and F12 in the Risk Gate
Branch:  fix/risk-gate-adversary      Worktree: /home/shahrad/work/agentoquant-wt/fix-risk-gate
Status:  complete for the gate; one caller-side wiring item is owned by another agent (F11, below)

Source of truth: `docs/reviews/phase0_adversary.md`, sections F7, F8, F11, F12. Every finding was
reproduced on the gate as merged (`main` = `c28129b`, branch base `4fedfc4`) before anything was
changed, then fixed, then re-measured. The reproduction script is `/tmp/oldbehaviour.py`, which
imports only names that exist in both versions so it runs against either tree.

Files touched:
```
agentoquant/risk/gate.py                      F7, F8, F11, F12 (rules, inputs, evaluation paths)
config/risk_limits.yaml                       F12 (the two dead positions keys, now read)
docs/phase0_status.md                         F12 (rule count 22 -> 27; the file the finding names)
tests/test_risk_gate_adversarial.py           F7/F8/F11 cases added to the two rule tables
tests/test_risk_gate_adversary_fixes.py       new: the F7/F8/F11/F12 regression suite (decorator-free)
```
Nothing outside that list was written. `agentoquant/execution/order_manager.py`,
`tests/test_execution_bridge.py` and `agentoquant/execution/paper.py` were read, never written.

## Commands run

```
# reproduction and before/after, on the same script
$ git show main:agentoquant/risk/gate.py > agentoquant/risk/gate.py ; .venv/bin/python /tmp/oldbehaviour.py
  0/8 checks passed
$ git checkout -- agentoquant/risk/gate.py ; .venv/bin/python /tmp/oldbehaviour.py
  8/8 checks passed
```
```
# the suite
$ .venv/bin/python -m pytest -q
  270 passed, 1 skipped in 83.11s
$ .venv/bin/ruff check agentoquant/ config/ tests/ scripts/
  All checks passed!
$ .venv/bin/python scripts/task5_acceptance.py
  18/18 checks passed
```
```
# the loop the soak runs, eight consecutive hours = the whole placeholder pattern, dry-run only
$ .venv/bin/python /tmp/loopcheck.py            # NullTransport, scratch ledger + signal dir
  2026-09-19T08Z-0001 enter_laddered       verdict=approved rule=None size=3.0 intents=3
  2026-09-19T09Z-0001 trail_stop           verdict=approved rule=None size=2.0 intents=1
  2026-09-19T10Z-0001 hold                 verdict=approved rule=None size=0.0 intents=0
  2026-09-19T11Z-0001 trim                 verdict=approved rule=None size=1.5 intents=1
  2026-09-19T12Z-0001 take_profit_ladder   verdict=approved rule=None size=3.0 intents=1
  2026-09-19T13Z-0001 hold                 verdict=approved rule=None size=0.0 intents=0
  2026-09-19T14Z-0001 add                  verdict=approved rule=None size=2.0 intents=2
  2026-09-19T15Z-0001 exit                 verdict=approved rule=None size=3.0 intents=1
  failures: 0
```

## F7 — a severity-5 adversary objection blocked nothing

**Fixed.**

Before (gate as merged): `verdict=approved rule=None final=3.0` with
`card.strongest_objection={'severity': 5, ...}`. After:
`verdict=rejected rule=unresolved_severity5_objection final=0.0`.

- `RULE_UNRESOLVED_SEVERITY5 = "unresolved_severity5_objection"` is in `REJECT_RULES`, read from
  `card.strongest_objection` through `objection_severity(card)` against
  `UNRESOLVED_OBJECTION_SEVERITY = 5` (`plan.md:86`, Task 20's acceptance criterion).
- It blocks **new exposure only**: `trim`, `exit`, `hold`, the stop actions and `cancel_order` are
  unaffected, because refusing a way out of a position is the failure mode worth avoiding.
- An objection that is present but malformed (`{}`, `severity: None`, `severity: "high"`) fails
  closed: unresolved, not absent.
- Tests: `RULE_UNRESOLVED_SEVERITY5` case in the pinned `REJECT_CASES` table, plus four tests in the
  new file (any size, severities 0/1/2/4 still approve, absent objection, malformed fails closed,
  reductions unaffected).

## F8 — the gate was a pass-through for seven of the twelve actions

**Fixed.** The seven were `trim`, `exit`, `set_stop`, `trail_stop`, `take_profit_ladder`,
`cancel_order`, `hold` — everything outside `INCREASING_ACTIONS`. Before, all seven returned
`approved / rule=None / final == original` for a card that violated every rule at once
(`size_pct=99`, `coin=None`, `sleeve=None`, `confidence=0`, `NO_TRADE`, severity-5 objection, against
`venue_status="offline"`, `stop_on_exchange=False`, `HaltState(flat=True)`). After: `0 of 12 still
pass through`.

`evaluate` now dispatches to one path per action class, and each of the seven passes through at
least one rule that can shrink or reject it:

| action | rule(s) that now bound it | case in the suite |
|---|---|---|
| `trim` | `incomplete_card`, `missing_size`, `reduction_cap` | trim 25% against a 12% position → `shrunk 12.0` |
| `exit` | same three | exit 40% against a 3% position → `shrunk 3.0` |
| `hold` | `hold_size_zero` | hold asking 99% → `shrunk 0.0` (it used to publish 99.0) |
| `set_stop` | `incomplete_card`, `venue_status`, `missing_stop` | venue `maintenance` → `rejected venue_status` |
| `trail_stop` | same three | no stop price and none on the exchange → `rejected missing_stop` |
| `take_profit_ladder` | same three | as above |
| `cancel_order` | `incomplete_card`, `venue_status` | no coin → `rejected incomplete_card` |

- New shrink rules: `RULE_REDUCTION_CAP` (a reduction can never exceed the position the context
  reports; a position the context does not name is left alone, and an already-closed position is a
  rejection) and `RULE_HOLD_SIZE_ZERO` (`hold` is approved at 0.0 whatever the card asks for, so a
  size is never published for an action that places nothing).
- The module docstring now names which rules apply to which action, so a reader can see which
  actions the gate actually guards.
- Tests: `test_no_vocabulary_action_is_a_pass_through`, `test_the_former_pass_throughs_are_bounded_by_a_named_rule`,
  `test_a_hold_never_publishes_a_size`, `test_a_reduction_is_bounded_by_the_position_it_reduces`,
  `test_management_needs_a_coin_and_an_online_venue`, plus the pinned suite's new
  `RULE_REDUCTION_CAP` / `RULE_HOLD_SIZE_ZERO` shrink cases and a completeness check that fails if a
  rule has no case.

**Stated deviation.** The review suggested routing the management actions through "at least the
halt, venue-status and incomplete-card rules". I did not put the halt rules on them, and the reason
is a conflict inside the review's own evidence: `tests/test_risk_gate_adversarial.py`'s
`test_reductions_and_hold_are_never_blocked_by_a_halt` pins `set_stop`/`trail_stop` as approved under
`/flat`, and that is the safer call — a halt closes positions, so blocking stop placement during one
is the opposite of what a halt is for. Management actions are guarded by card integrity, venue
status and their own stop instead, which is enough to make none of them a pass-through. If the phase
agent prefers the review's wording, the change is two lines in `_evaluate_management`, but the pinned
test would have to change with it.

## F11 — nothing in the gate could detect stale data

**Fixed in the gate; the caller-side wiring is outstanding and is owned by another agent.**

- `MarketContext` now carries `as_of: datetime | None` and `is_stale: bool` (the ledger's own
  `RawSnapshotPayload.is_stale`, which the gate previously never read).
- New reject rule `RULE_STALE_MARKET_DATA = "stale_market_data"`, checked as the first market-data
  rule (before `not_tradable`, `min_liquidity`, `max_spread`): an old snapshot is not even a market.
- The limit is one cadence plus a full margin — `STALE_MARKET_DATA_CADENCES = 2.0` times
  `settings.cadence_minutes` (60) = 7 200 s — read from config, not invented, so a faster cadence
  tightens it. `PortfolioContext.market_data_max_age_s` overrides it per call.
- `is_stale=True` is stale outright. An unknown `as_of` is **not** stale by default (the gate will
  not invent a timestamp), but a caller that declares its own limit is saying its snapshots carry
  timestamps, so under a declared limit an unknown age fails closed.
- It stops new exposure only: `trim`, `exit`, `set_stop`, `trail_stop` are unaffected by stale data.
- Before/after: `MarketContext.__init__() got an unexpected keyword argument 'as_of'` → a three-hour-old
  snapshot is `rejected stale_market_data`; 59 minutes and 1 h 59 m stay approved.
- **Residual, stated plainly:** `agentoquant/execution/paper.py`'s `placeholder_context` is the only
  caller in Phase 0 and it does not populate `as_of`/`is_stale`, and that file belongs to another
  agent right now (the kill-switch `halts=` wiring is landing there). So the running placeholder
  loop still cannot produce a stale verdict; the gate can. The wiring is one argument in
  `placeholder_context` (`as_of=now`) and, in Phase 2, the ingest snapshot's `ts` and `is_stale` on
  the real brief.

## F12 — dead config keys and a rule count documented as 22 while the code had 23

**Fixed, with one residual that needs a `config_loader.py` change to remove.**

Both `positions` keys the gate never read are now read, so neither is dead:

- `positions.sleeve_caps_pct`: the gate's sleeve cap is now the **tighter** of it and
  `config/sleeves.yaml`'s `cap_pct` (`RiskGate._sleeve_cap_pct`). Editing the file named for limits
  now binds; editing either file can only tighten the cap, never loosen it. Evidence: sleeve B cap
  35 → 10 in the limits file turns a 15% sleeve B entry from `shrunk 11.67` into
  `rejected sleeve_cap` (on `main`: `15.0 rule=None`, unchanged by the edit).
- `positions.min_concurrent`: read as the number of positions the book spreads across, so one
  position may take at most `sleeve_cap / min_concurrent` (`RiskGate._diversification_cap_pct`).
  Evidence: `min_concurrent` 3 → 5 takes the same sleeve B entry from 11.67% to 7.0%
  (`rule=position_cap`); on `main` both values allowed 15.0%. Shrink-only by construction.
- `docs/phase0_status.md` now says 27 rules, and
  `test_the_status_doc_rule_count_matches_the_code` parses that line and fails if it drifts from
  `len(ALL_RULES)`. A second test pins the four `positions` keys, so a new key without a reader
  fails the suite.
- **Residual:** `sleeves.yaml`'s `cap_pct` cannot be deleted (it is also the sleeve target the
  free-cash floor converts, and `config_loader.py`'s `_Strict` models require it and forbid extra
  keys — `config_loader.py` is outside this change's scope). "One source, named once" is therefore
  not achieved; the tighter-of-two rule removes the silent-failure trap the finding was about. A
  follow-up that makes the limits file the single source needs a `config_loader.py` change.

## The never-enlarge invariant

Intact and provably so:

- `RiskGate._verdict` still clamps `final` into `[0, original]` with the assertion behind it, so the
  invariant is structural: every new rule is a cap, a zero, or a rejection, and nothing added here
  can raise a size.
- `assert_never_enlarges` (final ≤ original, final ≥ 0, approved ⇒ unchanged, rejected ⇒ 0, rule ∈
  `ALL_RULES`) is asserted in every test that produces a verdict in both files.
- `test_no_action_and_no_size_can_be_enlarged` sweeps all 12 actions × 9 sizes
  (`None`, `0`, `-50`, `1e-9`, `1`, `25`, `1e9`, `inf`, `nan`) × 5 contexts = 540 verdicts.
- `test_a_ledger_attached_does_not_change_a_verdict` shows the new paths are pure with and without a
  ledger, and the pinned suite's original seven named adversarial cases (oversized,
  over-concentrated, no stop, taker order, illiquid coin, over the turnover cap, over the net-buy
  cap) still reject or shrink.

## Acceptance

```
Gate can only shrink or reject, never enlarge   -> pass: 540-case sweep + the clamp + 18/18 acceptance checks
No vocabulary action is a pass-through          -> pass: 0 of 12 pass through a hostile card (was 7)
A severity-5 objection blocks a new entry       -> pass: rejected unresolved_severity5_objection
A stale snapshot is not treated as fresh        -> pass: rejected stale_market_data (gate side; caller wiring open)
Every config key in risk_limits.yaml is read    -> pass: sleeve_caps_pct and min_concurrent both move a verdict
The documented rule count matches the code      -> pass: docs/phase0_status.md 27 == len(ALL_RULES) 27
```

## Verification

```
.venv/bin/python -m pytest -q                        -> 270 passed, 1 skipped (was 234 + 1 on the branch base)
.venv/bin/ruff check agentoquant/ config/ tests/ scripts/ -> All checks passed!
.venv/bin/python scripts/task5_acceptance.py         -> 18/18 checks passed
/tmp/oldbehaviour.py on main / on this branch        -> 0/8 then 8/8
/tmp/loopcheck.py (8 consecutive hourly cycles)      -> 8 actions, 0 failures (16 verdict rows for 8 cycles: F2's double write, untouched)
.venv/bin/python scripts/at_restore.py check ...     -> markers=0 in every file written here
```

## Deviations

1. Management actions are guarded by card integrity, venue status and their own stop rather than by
   the halt rules (see F8 above): the pinned suite and the safety direction both say a halt must not
   block stop management.
2. `min_concurrent`'s reading (equal share of the sleeve's cap per position) is an interpretation.
   It is shrink-only, it is config-driven, and it tightens sleeve B's per-position cap from 15% to
   11.67%; a book-level reading (`100 / min_concurrent`) would leave the key unable to bind at all,
   which is the dead-key problem the finding is about.
3. An unknown snapshot age is not called stale unless the caller declares a staleness limit. Failing
   closed on every unknown age would reject every entry in the Phase 0 placeholder loop and every
   case in the pinned suite, i.e. it would trade one defect for a broken loop.
4. The four new rules take the count from 23 to 27; `docs/phase0_status.md` and the completeness
   test were updated with them.
5. `docs/phase0_status.md` is outside the stated file list, but F12's own suggested fix names it.

## Open questions

1. **Merge conflict, must be resolved deliberately:** `main` has since fixed the same doc line (F17)
   and it now reads `Deterministic Risk Gate (23 rules) ... 27 adversarial tests`. On this branch the
   line reads `(27 rules) ... 36 adversarial tests, 27 cases (one per rule)`. The merged line must
   keep `27 rules`; `test_the_status_doc_rule_count_matches_the_code` fails loudly if it does not.
2. The halt-vs-management question in F8 (deviation 1). Recommendation: keep this branch's choice.
3. The `sleeves.yaml` / `risk_limits.yaml` cap duplication (F12 residual). Recommendation: a small
   follow-up that makes `risk_limits.yaml` the single source, with the `config_loader.py` change.

## Not done

- F11's caller wiring: `paper.py`'s `placeholder_context` does not yet pass `as_of`/`is_stale` (file
  owned by another agent), so the Phase 0 loop cannot yet produce a `stale_market_data` verdict.
- The `sleeves.yaml` duplicate removal (needs `config_loader.py`, outside this change's scope).
- F2's double `risk_gate_verdict` write is still visible in the loop check (`verdict rows: 16` for 8
  cycles); it is a different finding and a different agent's file.
