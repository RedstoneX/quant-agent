---
name: qamc-checkpoint
description: Close a QAMC internal stage gate or external tranche checkpoint with tests, independent review, commit boundary, operator Compass refresh, and correct STOP behavior.
---

# QAMC checkpoint workflow

Read `docs/STATE.md`, `docs/ROADMAP.md`, and `docs/work/ACTIVE.md` to determine whether this is an internal tranche gate or the external STOP.
Do not preload the Project Compass while engineering; refresh it at the handoff step from authoritative live state.

## Internal gate

1. Verify that stage's acceptance outcome against the actual implementation.
2. Run targeted tests/type/build checks appropriate to the changed surface.
3. For UI work, obtain real runtime/visual evidence where available.
4. Invoke a fresh `qamc-reviewer`; for UI-heavy work also invoke `qamc-ui-reviewer`.
5. Fix verified BLOCKER/IMPORTANT findings and rerun affected checks.
6. Create a clear commit boundary identifying the stage.
7. Refresh `docs/knowledge/PROJECT_COMPASS.md` so the operator can immediately see what just completed, what is happening now, and what comes next. Follow `.claude/rules/documentation.md`; keep it plain-English, visual, emoji-landmarked and concise.
8. Do **not** create another governance/checkpoint document merely to narrate work already evidenced by commits/tests.
9. If the current `docs/STATE.md` authorizes continuation across the gate, continue.

## External checkpoint / tranche end

1. Verify all authorized outcomes.
2. Run the governed complete backend suite once plus complete frontend build/type/lint/test checks that exist.
3. Re-check paper-only, deterministic-risk, API read-only, no-secret, and trading-isolation invariants.
4. Perform fresh-context independent review with `qamc-reviewer` and UI review where applicable.
5. Fix verified findings and rerun affected checks.
6. Ensure the branch has clean, auditable stage-boundary commits.
7. Refresh `docs/knowledge/PROJECT_COMPASS.md` to show **implementation complete / external review pending**. Never mark external acceptance yourself.
8. Push the implementation branch.
9. STOP. Do not merge and do not start the next unauthorized stage.

Handoff must report branch, stage-boundary commits, tests/checks, visual verification, architecture choices, independent-review findings/resolutions, and remaining limitations.

ChatGPT/operator perform the external acceptance review and merge. `docs/STATE.md` is updated after accepted merge, not by Claude claiming its own external acceptance. The accepted merge/reconciliation should also leave the operator Compass synchronized with the newly accepted state.
