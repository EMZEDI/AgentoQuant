# Data-layer adversary findings F14 and F15 — quota authority and a persisted budget

Task:        Fix the data-layer findings from `docs/reviews/phase0_adversary.md` (F14, F15)
Branch:      `fix/quota-adversary`      Worktree: `~/work/agentoquant-wt/fix-quota`
Status:      complete (both findings fixed; see "Not done" for what is deliberately left to the phase agent)
Baseline:    `main` = `4fedfc4`, 234 passed / 1 skipped
Merged:      `main` at `c28129b` (Task 4's per-module listener tests, F16/F17, F18) merged in on this
             branch: **396 passed, 1 skipped, ruff clean** on the merged tree, so this branch
             integrates with the work that landed while it was open.

## Findings

| # | Finding | Status | Evidence |
|---|---|---|---|
| F14 | The early-signal layer bypasses the repo-wide quota manager; seven sources' budgets unenforced | **fixed** | Reproduction below (request sent with the budget at 0, `breaches()` empty) and the post-fix run (0 requests, refusal journalled, breach counted in a *second* process) |
| F15 | Quota accounting is per process, so a budget can be spent twice | **fixed** | Reproduction below (B still saw 15/15 after A spent it and acquired anyway) and the post-fix run (B sees 0 and is refused); `tests/test_quota_shared_budget.py` pins it across real processes |

Nothing in this change touches `agentoquant/execution/**`, `agentoquant/risk/**`,
`agentoquant/ledger/**`, or any pre-existing test file.

## What changed, in one paragraph

The quota journal is now the **budget**, not a per-process copy of it. Every read re-checks the
journal's `(mtime_ns, size)` and re-reads it when any process has appended, and `QuotaManager.acquire`
holds an exclusive `flock` on the journal across its read-check-append, so two processes cannot both
take the last unit. A refused call is journalled (`"refused": true`, with the ceiling that refused it,
`"call": false` so a refusal never extends the exhaustion it reports), which makes a skip visible to
the *other* process's report. `HttpFetcher` (the early-signal layer's HTTP path) now takes an optional
`QuotaManager` plus the `config/sources.yaml` name and reserves every attempt before the socket, and
`build_listeners` hands one manager to all six fetching listeners. `agentoquant/data/ingest.py`'s
report says which journal the budget lives in and names any source it had to skip, with the reason.

## Reproduction of the two findings (pre-fix code, `4fedfc4`)

F15 — two instances over one journal, the second having loaded its view first:

```
$ .venv/bin/python /tmp/f15_repro.py
B sees remaining: 15
A remaining: 0 A spent: 15
B remaining AFTER A spent the whole budget: 15
B acquired anyway: the budget was spent twice across two instances
journal lines on disk: 16
```

F14 — a real `BybitListingsListener` against an offline transport, with the source's declared
10 calls/minute already spent:

```
$ .venv/bin/python /tmp/f14_repro.py
repo-wide manager says bybit_announcements remaining: 0
repo-wide manager breaches: {}
listener poll_once wrote: []
HTTP requests the listener actually sent: 1 ['/v5/announcements/index']
journal lines total: 10
journal lines for bybit_announcements: 10          <- all ten are the repro's own exhausting loop
repo-wide manager breaches after the listener fetched: {}
```

The listener fetched with the budget at zero and the manager that reports breaches never saw it.

## The same two commands after the fix

```
$ .venv/bin/python /tmp/f15_repro.py
B sees remaining: 15
A remaining: 0 A spent: 15
B remaining AFTER A spent the whole budget: 0
B refused: quota for 'kraken_rest' would be breached: calls_per_minute is 15/15, next call allowed in 60s
journal lines on disk: 16                          <- 15 calls + 1 refusal line
```

```
$ .venv/bin/python /tmp/f14_verify.py
repo-wide manager says bybit_announcements remaining: 0
poll_once -> []
HTTP requests actually sent: 0
listener failure recorded: 1 FetchQuotaExceeded: bybit_announcements: quota refused before the request: ...
breaches now: {'bybit_announcements': 1}
journal refusals: ['quota refused: calls_per_minute exhausted (10/10)']
fresh ingest-process manager breaches: {'bybit_announcements': 1}
listeners: ['bybit_listings', 'okx_listings', 'kraken_listings', 'github_releases', 'google_news_rss', 'telegram_previews']
unbudgeted fetchers: []
sources spent from: ['bybit_announcements', 'github_releases', 'google_news_rss', 'kraken_listings_rss', 'kraken_rest', 'okx_announcements', 'telegram_previews']
```

## End-to-end: the two production processes, one budget

The hourly ingest is a fresh `oneshot` process; the listener runner is long-lived. Script
`/tmp/two_process_demo.sh` runs the runner for 14 s while a second process (the ingest's shape)
spends from the same journal, then reads the runner's own log and status:

```
second process stopped after 8 calls: quota for 'bybit_announcements' would be breached: calls_per_minute is 10/10, next call allowed in 57s
second process sees remaining: 0
--- runner status (tail) ---
  "quota_journal": "/tmp/tmp.4BsTbX1mUY/quota.jsonl",
  "quota_breaches": {
    "bybit_announcements": 3
  }
--- runner poll failures caused by the other process's spend ---
"event": "poll_failed", "listener": "bybit_listings", "consecutive_failures": 1, "backoff_seconds": 5.818, "error": "FetchQuotaExceeded: bybit_announcements: quota refused before the request: quota for 'bybit_announcements' would be breached: calls_per_minute is 10/10, next call allowed in 53s"
"event": "poll_failed", "listener": "bybit_listings", "consecutive_failures": 2, "backoff_seconds": 11.154, "error": "FetchQuotaExceeded: bybit_announcements: quota refused before the request: ..."
--- shared journal: calls vs refusals ---
calls by the runner+ingest: 10
refusals recorded:          3
{"ts": "...", "source": "bybit_announcements", "ok": false, "cost_usd": 0.0, "detail": "quota refused: calls_per_minute exhausted (10/10)", "call": false, "refused": true}
```

Ten calls granted against a ten-call ceiling across two processes, three refusals recorded in the
shared journal, `restarts_total: 0`, `events_total: 18`, `failures_total: 2`. The runner skipped the
source, backed off, kept running and kept collecting from the other listeners — the graceful
degradation the design constraint asked for, with the skip reported in its own status.

A single-listener run (the production command, scratch journal) also shows the wiring line:

```
$ python -m agentoquant.data.early_signals.runner --only bybit_listings --no-onchain --duration 3 --interval-scale 0.05 --log-dir <scratch>
  "quota_journal": "/tmp/tmp.SE7BqvIRzg/quota.jsonl",
  "quota_breaches": {}
$ head -1 <scratch>/early_signals.jsonl
{"ts": "...", "event": "quota_wired", "journal": "/tmp/tmp.SE7BqvIRzg/quota.jsonl", "sources": ["bybit_announcements"], "unbudgeted": []}
$ head -1 <scratch>/quota.jsonl
{"ts": "...", "source": "bybit_announcements", "ok": true, "cost_usd": 0.0, "detail": "GET https://api.bybit.com/v5/announcements/index", "call": true, "refused": false}
```

## Files touched

```
agentoquant/data/quota_manager.py                     F15: journal-as-budget, flock, journalled refusals
agentoquant/data/early_signals/__init__.py            F14: HttpFetcher.quota/source/reserve(), FetchQuotaExceeded, Listener.fetchers()
agentoquant/data/early_signals/bybit_listings.py      F14: SOURCE + quota wiring
agentoquant/data/early_signals/okx_listings.py        F14: SOURCE + quota wiring
agentoquant/data/early_signals/kraken_listings.py     F14: SOURCE/RSS_SOURCE + quota wiring on both fetchers
agentoquant/data/early_signals/github_releases.py     F14: SOURCE + quota wiring
agentoquant/data/early_signals/google_news_rss.py     F14: SOURCE + quota wiring
agentoquant/data/early_signals/telegram_previews.py   F14: SOURCE + quota wiring
agentoquant/data/early_signals/runner.py              F14: one QuotaManager for the set, quota_audit/quota_sources, status reporting
agentoquant/data/ingest.py                            Report the journal and the reason a source was skipped
tests/test_quota_shared_budget.py                     NEW: 15 tests, no decorators, no network
docs/reports/f14_f15_quota_fix.md                     This report
```

## Acceptance criteria and verification (Task 3 and Task 4 lines, `tasks/todo.md`)

| Criterion | Result | Evidence |
|---|---|---|
| Task 3: "One hourly snapshot completes inside every source's quota" | **pass, now covering the listener sources too** | The snapshot test with an exhausted `gold_api` budget completes and reports `missing_sources == ["gold_api"]`, `quota_breaches == {"gold_api": 1}`; the report note carries `calls_per_hour exhausted (60/60)` |
| Task 3: "A failed source is recorded as missing, and the cycle continues" | **pass** | Same test: `report.cycle_id` is set, the healthy source's readings are written, nothing raised |
| Task 3 verification: "24-hour dry run with zero quota breaches and a per-source call count and cost report" | **not re-run here** (phase evidence) | The report now covers the early-signal sources, which it previously could not: `QuotaManager.report()` lists every declared source with `calls_1m/1h/1d`, `limits`, `remaining`, `breaches` and `last_refusal_reason` |
| Task 4: "A new Kraken or Bybit listing appears in the ledger within one minute" | **unchanged, still pass** | The live runner run above wrote 9 Bybit listing records to a scratch ledger and 0 restarts |
| Checkpoint: "No API quota breaches" | **now verifiable for both paths** | A refusal is journalled with its reason and counted by `breaches()` in *either* process (`test_a_refusal_is_recorded_and_visible_to_another_process`, and the two-process demo above) |
| Design constraint: budget survives a restart and is shared between the two processes | **pass** | `test_the_budget_survives_a_process_restart`, `test_a_budget_spent_in_one_process_is_visible_to_the_next`, `test_racing_processes_cannot_spend_the_same_budget_twice` (four processes, ten-call ceiling, exactly ten granted) |
| Design constraint: an over-budget source is skipped with a recorded reason, not a crash | **pass** | `test_an_over_budget_listener_sends_no_request`; two-process demo (`restarts_total: 0`) |

## Commands run

```
$ .venv/bin/python -m pytest -q tests/test_quota_shared_budget.py
15 passed in 1.87s

$ git checkout 4fedfc4 -- agentoquant/data/... ; .venv/bin/python -m pytest -q tests/test_quota_shared_budget.py
FAILED test_two_managers_over_one_journal_share_the_budget
FAILED test_a_budget_spent_in_one_process_is_visible_to_the_next
FAILED test_the_budget_survives_a_process_restart
FAILED test_a_refusal_is_recorded_and_visible_to_another_process
FAILED test_a_refusal_does_not_extend_the_exhaustion
FAILED test_an_over_budget_listener_sends_no_request
FAILED test_a_listener_call_is_charged_to_its_sources_yaml_name
FAILED test_the_listener_and_the_hourly_ingest_share_one_journal
FAILED test_every_listener_the_runner_builds_reports_to_one_manager
FAILED test_the_audit_names_an_unbudgeted_fetcher
FAILED test_a_retry_spends_a_second_unit_of_the_budget
FAILED test_an_over_budget_connector_is_skipped_and_the_snapshot_reports_it
12 failed, 3 passed in 1.70s          <- run against the pre-fix code, same test file

$ .venv/bin/python -m pytest -q
249 passed, 1 skipped in 80.86s       <- 234 baseline + 15 new

$ git merge main --no-edit ; .venv/bin/python -m pytest -q
396 passed, 1 skipped in 87.15s       <- after merging main at c28129b (Task 4's per-module tests)

$ .venv/bin/python -m ruff check .
All checks passed!

$ grep -rn "httpx.Client(\|urllib.request" --include=*.py agentoquant/
agentoquant/data/__init__.py:206:            self.client = httpx.Client(...)          <- Transport, quota'd since Task 3
agentoquant/data/early_signals/__init__.py:278:            self.client = httpx.Client(...)          <- HttpFetcher, quota'd by this change
agentoquant/execution/telegram_bot.py:140:     urllib.request.Request(...)              <- out of scope: Telegram send, not a declared source
```

The data layer now has exactly two places a request can leave the process, and both reserve from the
same manager before they do.

## Deviations, with the smallest-deviation reasoning

1. **The budget lives in the JSONL journal (with an `flock`), not in DuckDB.** The suggested fix
   offered both. DuckDB is the wrong host for this particular file, and the reason is mechanical
   rather than aesthetic: DuckDB refuses a second process that holds the same database open, and the
   listener runner holds `data/ledger.duckdb` open for its whole life. Verified on this box
   (duckdb 1.4.5):

   ```
   _duckdb.IOException: IO Error: Could not set lock on file "/tmp/duck_multi_test.duckdb":
   Conflicting lock is held in /home/shahrad/.local/share/uv/python/cpython-3.11.16.../python3.11
   ```

   A ledger-resident budget would therefore be unshareable by exactly the two processes that must
   share it. The journal is already persisted, already the report's source of truth, and one atomic
   line under a POSIX lock gives the same guarantee.
2. **A refused call is journalled with `"call": false`.** It is not a call (so it cannot extend the
   exhaustion it reports) but it must be durable, or the other process cannot report the skip.
3. **`FetchQuotaExceeded` is a new public name** (a `FetchError` subclass) so a listener's existing
   "this source is unavailable this pass" path handles a refusal without a second code path.
4. **New test file, no decorators.** `tests/test_quota_shared_budget.py` uses plain functions and
   plain loops because of the at-sign rule in `.hermes.md`; `tmp_path` and `monkeypatch` arrive as
   ordinary arguments. It adds no fixtures to the shared `conftest.py`.
5. **Three of the fifteen tests pass on the pre-fix code, on purpose**:
   `test_both_processes_default_to_the_same_journal_file` (the default path was already shared),
   `test_a_fetcher_without_a_manager_keeps_working_standalone` (the hook is optional), and
   `test_racing_processes_cannot_spend_the_same_budget_twice`, whose pre-fix result is
   timing-dependent (it passed because the four children happened not to overlap). Post-fix its
   assertion is exact, not timing-dependent.
6. **Two test functions import the runner lazily.** `build_listeners`/`quota_audit`/`quota_sources`
   are new names, so a module-level import would have made the pre-fix run a collection error
   instead of eleven honest test failures.

## Open questions

1. **The listener runner holds the ledger open for its whole life, and DuckDB refuses a second
   writer** (the exception quoted above). In production that means the hourly `agentoquant ingest`
   process cannot open `data/ledger.duckdb` while the runner is up — a ledger/store question, not
   F14/F15, and out of this task's scope (another agent owns that area). It is worth verifying
   against the live soak before the 3-day checkpoint run; the smallest fix on the store side is a
   per-write connection in the runner rather than one held for the process's life.
2. **The runner's coverage gap is closed on `main`, not here.** `tests/test_early_signals.py`'s
   docstring advertises runner coverage it does not have (the file ends at line 650 with a sentinel),
   but the per-module files `tests/test_early_runner.py` and friends landed on `main` while this
   branch was open and this branch merged them cleanly. Worth knowing that
   `test_build_listeners_returns_the_six_pollers_with_scaled_intervals` calls `build_listeners`
   without a quota manager: the default `QuotaManager()` is constructed and never used, which writes
   nothing (the journal is only touched by `acquire`), and the test never polls — verified: no
   `data/quota_journal.jsonl` appears after a full-suite run.

## Not done (deliberately, and who owns it)

1. **No 24-hour soak.** The checkpoint clause "no API quota breaches" is now *verifiable* for the
   early-signal path — every one of the seven sources is reserved, counted and refused-with-a-reason —
   but the soak itself is the phase agent's evidence to collect, not a code change.
2. **F15's alternative shape** (`INSERT ... SELECT WHERE count < ceiling` in the ledger) was not
   taken; see deviation 1 for why.
3. **No change to `agentoquant/execution/**`, `agentoquant/risk/**`, `agentoquant/ledger/**`, or any
   existing test file**, per the task's scope rules.
