# QAMC Current Work

Status: **STAGE 4–5 EXTERNALLY ACCEPTED — INTEGRATION HOUSEKEEPING ONLY, THEN STOP**

## Goal

Stage 4 (per-candidate specialist evidence + decision chain) and Stage 5
(journal + forensic search) were built as one coordinated engineering
tranche, internally reviewed, and pushed as PR #24. **ChatGPT/operator
external review passed and the tranche is accepted.**

The only currently authorized work is **integration housekeeping** on
`claude/stage-3-implementation-75e6dp` so PR #24 stays mergeable and its
acceptance evidence is durable:

- reconcile the branch with any newer commits on `main` (preserving both
  the accepted Stage 4–5 implementation and whatever newer
  lifecycle/source-of-truth documentation exists on `main`);
- resolve conflicts without overwriting newer authoritative documentation;
- record the permanent frontend-verification requirement introduced
  during this housekeeping pass (`.claude/rules/frontend-verification.md`)
  and its Stage 4–5 acceptance evidence (`docs/verification/stage-4-5/`);
- rerun the full test suite and confirm the cockpit UI still runs
  correctly after reconciliation;
- refresh `STATE.md`/`WORK.md`/`PROJECT_COMPASS.md` only as needed to
  reflect the accepted state and the current (not-yet-authorized) next
  gate;
- push, then **STOP**.

**Do not begin VPS deployment, deployed-MVP verification/UAT, dedicated
visualization/UX polish, or any new product work in this pass.** Do not
merge PR #24 — a human merges after this housekeeping is pushed.

## What "accepted" means for this repository

- The Stage 4–5 implementation (backend + cockpit UI) is accepted as-is;
  do not redesign, re-polish, or otherwise rework it during housekeeping.
- If reconciliation surfaces a genuine conflict between the accepted
  Stage 4–5 implementation and a newer decision on `main`, resolve it in
  favor of the newer `main` decision for documentation/process questions,
  and preserve the accepted implementation for anything Stage 4–5 already
  delivered and tests still cover. If a conflict can't be resolved that
  way, stop and record it here rather than guessing.

## Frontend verification requirement (new, permanent)

Added as `.claude/rules/frontend-verification.md`, path-scoped to
`src/api/static/**/*` and `docs/verification/**/*`: every future cockpit
UI acceptance pass must be browser/runtime verified by Claude (real
browser, seeded representative data, actually inspected) before external
review, with a small representative screenshot set committed to
`docs/verification/<stage-or-checkpoint>/` alongside a manifest recording
commit SHA, viewport/scenario, and verification date/time. Routine/
transient browser-test captures stay out of Git. This rule applies to
every future frontend change, not only this checkpoint.

## Next gate (not yet authorized)

If this housekeeping push is merged, the next intended tranche is **VPS
cutover/deployment hardening**, followed by Claude-run runtime/browser QA
on the VPS, a fresh independent review, and operator UAT. Only after that
deployed-MVP gate is accepted should dedicated Mission Control
visualization/UX polish be authorized. None of that is authorized by this
document — a subsequent `WORK.md` update must explicitly open it.

## Hard boundaries

- Alpaca **Paper only**.
- No deterministic trading/risk semantic changes.
- No broker-write Mission Control operations.
- No secrets or fake production trading state in client/UI surfaces.
- No VPS/deployment work, no dedicated UI-polish work, in this pass.
- Claude does not merge PRs, force-push, or push directly to `main`.

## Escalate early only when necessary

Stop before pushing only if there is a genuine unresolved operator
product/value trade-off, a material architecture/safety/scope conflict,
or evidence that invalidates the accepted Stage 4–5 outcome. Routine
reconciliation/documentation work belongs to Claude.
