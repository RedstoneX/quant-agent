---
name: qamc-checkpoint
description: Close an authorized implementation gate with verification, independent review, clean handoff and correct STOP behavior.
---

# QAMC checkpoint workflow

Read `docs/STATE.md` and `docs/WORK.md`.

Gate type comes from the current `STATE.md` / `WORK.md` contract, **not from the stage number**. Inside an authorized multi-stage tranche, intermediate stage boundaries are internal unless the current contract explicitly marks them external.

## Verification orchestration

Optimize checkpoint wall-clock time without weakening independence. Once the implementation is stable enough to review, run independent verification streams concurrently when they do not depend on one another—for example targeted/full tests, static/build checks, runtime/visual verification, and a fresh `qamc-reviewer`. Do not serialize independent checks merely for procedural neatness, and do not rerun equivalent expensive work without a reason.

The lead owns synthesis and fixes after the parallel checks return. Reviewer independence still matters: the reviewer must not author the implementation it reviews.

## Internal gate

1. Verify the accepted outcome for this slice.
2. Run appropriate targeted tests/build/type checks and UI runtime/visual checks where applicable, parallelizing independent checks.
3. Invoke a fresh `qamc-reviewer` as an independent stream when the diff is ready for review.
4. Fix verified BLOCKER/IMPORTANT findings and rerun only affected checks plus any required gate suite.
5. Create a clear commit boundary.
6. Refresh `docs/PROJECT_COMPASS.md` from authoritative state.
7. Continue when `STATE.md` / `WORK.md` authorize the next slice; do not seek external permission merely because a numbered stage finished.

Do not create a checkpoint/status document merely to narrate evidence already in tests/commits.

## External gate

1. Verify the complete authorized outcome.
2. Run the governed full backend suite plus relevant complete frontend/runtime checks, concurrently with independent review where practical.
3. Re-check paper-only, deterministic-risk, read-only API, no-secret and trading-isolation boundaries.
4. Perform fresh independent review and resolve verified BLOCKER/IMPORTANT findings.
5. Ensure clean auditable commits.
6. Refresh `docs/PROJECT_COMPASS.md` to show external review pending; never self-mark external acceptance.
7. Push and **STOP**. Do not merge or start unauthorized work.

ChatGPT/operator perform external acceptance and accepted merge.
