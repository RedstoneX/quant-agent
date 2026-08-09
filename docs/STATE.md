# QAMC Current State

Updated: 2026-08-09

This file says what is accepted and authorized **now**. Git history preserves prior state and discovery evidence.

## Accepted

- Stages 0, 0.5, 1, 2, 3, 4 and 5 are accepted.
- Stage 2 delivered the isolated read-only Mission Control API and deterministic `risk_gate` forensic persistence without changing trading/risk semantics.
- Stage 3 delivered the read-only browser/iPad Trading Cockpit at `/ui`; accepted verification: **1531 passed, 0 failed** plus desktop/iPad runtime review.
- Discovery R1 and ChatGPT reconciliation are accepted.
- Stage 4 delivered per-candidate specialist evidence + decision-chain drill-down (additive `specialist_evidence` table, `/runs/{run_id}/candidates` + `/runs/{run_id}/candidates/{symbol}`), preserving each specialist's real data scope rather than inventing a uniform per-symbol schema. Research evidence remains correlated by `run_id`/natural scope; `decision_id` remains PM → AI Risk → trade correlation, unchanged.
- Stage 5 delivered read-only journal (`/journal/dates`, `/journal/{date}`) and forensic search (`/search?q=`, parameterized-SQL-only). Both stages, backend + cockpit UI, passed **external ChatGPT/operator review** (PR #24) — accepted verification: **1558 passed, 0 failed**, plus browser/runtime verification evidence at `docs/verification/stage-4-5/`.
- A permanent frontend-verification requirement (`.claude/rules/frontend-verification.md`) now governs every future cockpit UI acceptance pass: browser/runtime verified by Claude before external review, with representative screenshot evidence + manifest committed under `docs/verification/<stage>/`.
- Default long-running runtime remains small Linux VPS/server + private access; avoid distributed infrastructure without demonstrated need.
- Cloud/ephemeral development environments are staging only. The QAMC MVP is not considered operationally accepted until the integrated product is deployed to the intended VPS/server runtime, verified there, independently reviewed, and accepted through operator UAT.
- Dedicated Mission Control visualization/UX polish belongs **after** that solid deployed-MVP gate. Polish may improve the operating surface but must not substitute for functional completeness, safety, observability, deployability or forensic integrity.

## Authorized now

**Integration housekeeping only** on the accepted Stage 4–5 branch (reconciliation with `main`, governance/evidence documentation, final verification, pushing so PR #24 stays mergeable). See `docs/WORK.md` for the exact scope.

No new implementation tranche is authorized yet. VPS cutover/deployment hardening, deployed-MVP verification/UAT, and dedicated visualization/UX polish all require a subsequent accepted `WORK.md` contract.

## Not authorized now

- deterministic trading/risk behavior changes;
- broker-write Mission Control controls;
- live trading;
- VPS deployment/cutover work;
- dedicated TradingView/donor-dashboard/visual-polish work as a separate phase;
- later learning/write-control stages beyond the accepted read-only Mission Control tranche;
- any new product work beyond the integration housekeeping `docs/WORK.md` currently scopes.

## Handoff

Stage 4–5 is accepted; PR #24 is the record. Claude does not merge it — a human merges after the current housekeeping push lands.

The next intended tranche is VPS deployment/hardening followed by Claude-run runtime/browser QA on the VPS, fresh independent review, and operator UAT. Only after that deployed-MVP gate is accepted should a dedicated dashboard visualization/UX-polish phase be authorized. That tranche requires its own explicit `docs/WORK.md` authorization before implementation begins.
