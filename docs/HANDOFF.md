# Hand-off: how work is assigned, verified, merged and reported

For the phase agents and task agents working this repo. Read `.hermes.md` first (Hermes auto-loads it), then `plan.md`, `tasks/todo.md` and `tasks/schema_scaffold_addendum.md`.

## Roles

- **Phase agent** (one per phase, branched from the planning session): owns a phase end to end. It reads the phase's tasks and dependency lines, decides which tasks can run in parallel, spawns one task agent per task, reviews every diff before merging, runs the phase checkpoint, and reports to Shahrad. It does not implement tasks itself unless a task is trivial.
- **Task agent** (one per task): implements exactly one task on its own branch/worktree, verifies it against the task's acceptance criteria and verification steps, and reports in the format below. It stops and asks rather than guessing when the spec is ambiguous.

## Wave plan — Phase 0

Phase 0 has hard dependencies, so full fan-out creates merge conflicts for no gain. Three waves:

| Wave | Tasks | Parallelism | Why |
|---|---|---|---|
| 1 | Task 1 — package skeleton, config, CLI, MCP stub | serial | everything imports its enums, schemas and config loader |
| 2 | Task 2 — ledger schema, DuckDB store, cost meter | serial | Tasks 3, 4 and 5 all write records into it |
| 3 | Tasks 3, 4, 5 — connectors / early-signal listeners / Risk Gate | **3 in parallel**, one worktree each | independent files; only Task 3 and 4 touch the data layer, Task 5 is pure logic |
| 4 | Task 6 — freqtrade bridge, paper harness, order manager | serial | needs 3 (data) and 5 (gate) |

For later phases derive waves the same way: read each task's `Dependencies:` line in `tasks/todo.md` and group tasks with no shared dependencies, then merge each wave to `main` before starting the next.

## Worktree and branch commands

```bash
cd ~/work/AgentoQuant && git fetch origin && git checkout main && git pull --ff-only

# wave 3 example — one worktree per parallel task
git worktree add ~/work/agentoquant-wt/task-03 -b task/03-data-connectors main
git worktree add ~/work/agentoquant-wt/task-04 -b task/04-early-signals  main
git worktree add ~/work/agentoquant-wt/task-05 -b task/05-risk-gate      main
```

Rules: `main` stays green; one agent per working tree; a task merges only after review and green tests; never rewrite pushed history; never force-push `main`.

Hermes worktree mode (`hermes --worktree`) is fine for agents editing code — it gives each session its own worktree automatically.

## The task prompt template

Give the task agent one task and nothing more. Attach the spec files as context every time.

```
You are implementing exactly ONE task of the AgentoQuant plan. Do not implement any other task.

Repo:      ~/work/AgentoQuant (your branch: task/NN-slug)
Read first: .hermes.md, plan.md, tasks/todo.md, tasks/schema_scaffold_addendum.md, docs/HANDOFF.md

Your task:  Task NN — <title from tasks/todo.md>
Acceptance criteria (verbatim):
  <paste the task's checkbox list>
Verification (verbatim):
  <paste the task's verification list>
Already merged and not to be re-implemented: Task <X>, Task <Y>

Rules: follow .hermes.md exactly. Paper/dry-run only. Never call a Kraken write endpoint.
Deliver: working code + tests, each acceptance criterion checked with real command output,
and a report in the format defined in docs/HANDOFF.md.
```

## Definition of done

A task is done when, and only when:

1. Every acceptance criterion in `tasks/todo.md` is satisfied, demonstrated with real command output (not a claim).
2. Every verification step has been run and its output pasted in the report.
3. The named tests in the addendum exist and pass (`tests/test_risk_gate_adversarial.py` in Task 5, `tests/test_leakage.py` in Task 10, `tests/test_schema_validation.py` in Task 16, `tests/test_ledger_roundtrip.py` in Task 2), plus any test the task's own criteria imply.
4. Nothing from another task was implemented early, and no dependency was added outside the addendum's pinned list without saying so.
5. The report below is written.

## Report format

```
Task NN — <title>
Branch:  task/NN-slug      Worktree: <path>
Status:  complete | partial | blocked

Files touched:   <paths, one per line>
Commands run:    <command → what it actually printed, trimmed to the relevant lines>
Acceptance:      <criterion> → pass/fail + the evidence line
Verification:    <step> → pass/fail + the evidence line
Deviations:      <anything that did not follow the spec verbatim, and the smallest-deviation reasoning>
Open questions:  <only things that genuinely block, with your recommendation>
Not done:        <what remains, if status is partial>
```

## Phase checkpoints

Run the checkpoint at the end of each phase and report it to Shahrad before starting the next phase. The checkpoint criteria are the `Checkpoint:` blocks in `tasks/todo.md`; collect evidence for each line, do not assert them.

- **Foundation**: 3-day paper run, every cycle and token in the ledger, cost per cycle measured, no quota breaches, Risk Gate rejects every adversarial proposal in its test set.
- **Forecasting lab**: calibrated probabilities and conformal intervals on purged held-out data, Verifier catches labelled pump events, at least one sleeve positive net-of-fees EV at the realistic tier surviving the DSR/PBO gate.
- **Decisions**: two weeks of paper trading through the full cascade, calibration reviewed, cost per cycle in budget, funding flow exercised once, sample cards reviewed.
- **Feasibility gate**: four weeks of paper results with measured daily EV net of fees and LLM cost versus the $100–200 target, calibration holding, one promoted challenger and one approved skill flowed end to end, value per model family reported. Go/no-go with Shahrad.

## Decisions recorded — apply these, do not re-litigate

1. **Sonnet-class roles run on OpenRouter `anthropic/claude-sonnet-5`**, using the project's own key (`AGENTOQUANT_OPENROUTER_KEY`). Every model call in the pipeline therefore goes through one key and one ledger cost path. Consequence for Task 16: the addendum's `Harness` enum records the role's **model tier, not the transport** — `CLAUDE_NATIVE` means the role runs on the Claude model family (currently served over OpenRouter), `HERMES_OPENROUTER` means a cheap OpenRouter model. One adapter serves every role, with the model per role in `config/models.yaml`. Opus is banned and a config naming one must fail validation at startup. Budget: ~$0.04 per Judge call, ~$9/month for the whole Sonnet role set, inside the key's monthly limit.
2. **The Hermes gateway owns the Telegram inbound path.** It already polls the bot token with `getUpdates`, and Telegram allows exactly one polling consumer, so `execution/telegram_bot.py` must **never** poll. Outbound: send cards and reports through the bot API or `hermes send`. Inbound: Hermes receives `/veto`, `/pause`, `/resume`, `/status`, `/why`, `/flat`, `/fund approve|decline` and relays each to the project CLI (`agentoquant veto --card-id X`, …) via a Hermes webhook subscription or a small plugin command. The project's 10-minute veto window waits on its own signal store; Hermes is only the input channel. This relay must exist before Task 21's veto timer is built, and the relay must never be able to place an order — it only writes a human action into the ledger.

## Quick reference

```bash
# environment
cd ~/work/AgentoQuant && uv venv --python 3.11 && source .venv/bin/activate && uv pip install -e ".[dev]"

# tests
pytest -q

# read-only Kraken checks (never a write endpoint)
~/.local/bin/kraken ticker BTCUSD -o json
~/.local/bin/kraken balance -o json

# credentials (never commit, never print)
~/.config/agentoquant/credentials.env     # mode 600, outside the repo

# Telegram delivery test (no polling, safe)
hermes send -t telegram "message text"

# Hermes cron: script-only jobs run a script with no LLM (see the openrouter-budget-daily job)
hermes cron list
```
