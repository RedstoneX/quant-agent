# QAMC Current State

Updated: 2026-08-09

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
- Cloud/ephemeral development environments are staging only. The QAMC MVP is not operationally accepted until the integrated product is deployed to the intended VPS/server runtime, verified there, independently reviewed, and accepted through operator UAT.
- Dedicated Mission Control visualization/UX polish remains after that deployed-MVP gate.

## Authorized now

**VPS cutover / deployment hardening and deployed-runtime verification** are authorized as the next bounded engineering tranche. See `docs/WORK.md` for the exact contract.

Claude may investigate the repository and choose implementation details, subagents and safe parallelism inside that contract. The tranche ends with a pushed branch and checkpoint report for independent ChatGPT review; Claude does not merge its own work.

## Not authorized now

- deterministic trading/risk behavior changes;
- broker-write Mission Control controls;
- live trading;
- dedicated TradingView/donor-dashboard/visual-polish work;
- later learning/write-control stages;
- any product expansion unrelated to deployment, runtime hardening or the deployed-MVP verification gate.

## Handoff

The immediate path is: establish secure temporary SSH bootstrap access from Claude's cloud environment, deploy/harden the accepted QAMC bundle to the OVH VPS, verify runtime/browser behavior there, push the bounded implementation branch, then **STOP** for fresh independent review and operator UAT.

Operator UAT and MVP acceptance happen after that external review. Dedicated visualization/UX polish is not authorized until the deployed MVP is accepted.
