# QAMC Current State

Updated: 2026-08-10

This file says what is accepted and authorized **now**. Git history preserves prior state and discovery evidence.

## Accepted

- Stages 0, 0.5, 1, 2, 3, 4 and 5 are accepted.
- Stage 2 delivered the isolated read-only Mission Control API and deterministic `risk_gate` forensic persistence without changing trading/risk semantics.
- Stage 3 delivered the read-only browser/iPad Trading Cockpit at `/ui`; accepted verification: **1531 passed, 0 failed** plus desktop/iPad runtime review.
- Discovery R1 and ChatGPT reconciliation are accepted.
- Stage 4 delivered per-candidate specialist evidence + decision-chain drill-down while preserving each specialist's real data scope and existing `decision_id` semantics.
- Stage 5 delivered read-only journal and parameterized forensic search. Stage 4–5 passed external ChatGPT/operator review with **1558 passed, 0 failed** plus committed browser/runtime evidence.
- PR #24 is merged into `main` as merge commit `105cc91a14faebd8a981061b3098eb181b306dda`.
- The permanent frontend-verification requirement under `.claude/rules/frontend-verification.md` remains accepted.
- VPS deployment/hardening tranche completed and independently reviewed.
- Development environment established separately under `/home/dev` with Claude Code installed for on-demand development use.

## Current operating model

- `ubuntu` = OVH administration / recovery account.
- `qamc` = QAMC runtime account only.
- `dev` = development and agent workspace only.

Development and runtime environments remain intentionally separate.

## Authorized now

The deployed MVP checkpoint is complete. Future engineering must continue from the accepted repository state and preserve:

- Alpaca Paper-only operation.
- Deterministic trading/risk protections.
- Mission Control read-only, non-critical boundary.
- Secrets outside Git/client surfaces.

## Not authorized now

- deterministic trading/risk behavior changes;
- broker-write Mission Control controls;
- live trading;
- dedicated dashboard visualization/visual-polish work until separately authorized;
- later learning/write-control stages without authorization;
- unnecessary infrastructure expansion.

## Handoff

Claude Code should operate from the `/home/dev` development environment when engineering work is required. Runtime changes belong to `/home/qamc` and require explicit authorization.

Operator UAT and future acceptance gates remain separate from implementation work.
