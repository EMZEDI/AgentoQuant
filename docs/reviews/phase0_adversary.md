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

_Report in progress — findings are appended and committed incrementally._
