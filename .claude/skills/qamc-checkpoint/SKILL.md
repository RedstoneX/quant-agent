---
name: qamc-checkpoint
description: Close an authorized implementation gate with verification, independent review, clean handoff and correct STOP behavior.
---

# QAMC checkpoint workflow

Read `docs/STATE.md` and `docs/WORK.md`.

## Internal gate

1. Verify the accepted outcome for this slice.
2. Run appropriate targeted tests/build/type checks and UI runtime/visual checks where applicable.
3. Invoke a fresh `qamc-reviewer`.
4. Fix verified BLOCKER/IMPORTANT findings and rerun affected checks.
5. Create a clear commit boundary.
6. Refresh `docs/PROJECT_COMPASS.md` from authoritative state.
7. Continue only if `STATE.md` / `WORK.md` authorize it.

Do not create a checkpoint/status document merely to narrate evidence already in tests/commits.

## External gate

1. Verify the complete authorized outcome.
2. Run the governed full backend suite plus relevant complete frontend checks.
3. Re-check paper-only, deterministic-risk, read-only API, no-secret and trading-isolation boundaries.
4. Perform fresh independent review and resolve verified BLOCKER/IMPORTANT findings.
5. Ensure clean auditable commits.
6. Refresh `docs/PROJECT_COMPASS.md` to show external review pending; never self-mark external acceptance.
7. Push and **STOP**. Do not merge or start unauthorized work.

ChatGPT/operator perform external acceptance and accepted merge.
