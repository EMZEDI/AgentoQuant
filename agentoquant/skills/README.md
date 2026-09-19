# skills/

Versioned skill files, git-tracked.

A skill is a concrete, reviewable rule the pipeline applies — a playbook update, an analyst belief
update, a new pre-trade check — written as an instruction with the ledger evidence that motivated it.

Rules:

- Nothing lands here without Shahrad's explicit approval (Task 24's approval flow).
- Every file cites the ledger records that motivated it.
- Every proposed change must pass the pipeline's tests before it merges.
- A skill is versioned: change it by editing it, and the diff is the record of the change.
- Priors are **not** skills. They live in `config/worldview.yaml`, are Shahrad's, and are never
  edited by the system — it may only flag one as possibly miscalibrated.

Empty in Phase 0 by design: the Reflector that proposes skills is Task 24 (Phase 3), and it works from
embargoed outcomes, which do not exist until the system has been deciding for weeks.
