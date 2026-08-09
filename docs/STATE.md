# QAMC Current State

Updated: 2026-08-09

This is the single live operational-state document. Git history and checkpoint records preserve history; this file says what is true **now**.

## Accepted

- Stage 0 — Baseline & Integration-Seam Audit: DONE / Checkpoint A accepted.
- Stage 0.5 — Actual-model attribution hotfix: DONE / Checkpoint A5 accepted.
- Stage 1 — Provider, Model & Correlation Plumbing: DONE / Checkpoint B accepted.
- Stage 2 — Thin Read-Only Mission Control API: DONE / Checkpoint C accepted.
- Final accepted Stage-2 verification reported: **1530 passed, 0 failed**.

Stage 2 includes:
- separate FastAPI/uvicorn read-only Mission Control API;
- broker-live account/positions/orders reads separated from SQLite history reads;
- independent SQLite `mode=ro` history access;
- `/health`, `/account`, `/positions`, `/orders`, `/trades`, `/runs`, `/runs/{run_id}`,
  `/decisions/{decision_id}`, `/agents`, `/agents/{agent_name}`, `/reflections`, `/candidates`;
- additive `risk_gate` forensic records for complete deterministic hard-risk reconstruction;
- no change to deterministic trading/risk/execution semantics.

## Currently authorized

**Mission Control build tranche: Stages 3 → 4 → 5.**

Claude Code may execute these as one coordinated engineering tranche with internal self-verification/commit boundaries:

- Stage 3 — Native Cockpit.
- Stage 4 — AI Decision Interface.
- Stage 5 — Native Journal & Indexed Search.

The Stage 3 and Stage 4 gates are internal engineering gates for this tranche.
The next **external STOP** is after Stage 5 / Checkpoint E.

Claude must not merge the tranche to `main`; it pushes the branch and hands off for independent ChatGPT/operator review.

## Not authorized

- Stage 6 — removed (AgentLens).
- Stage 7 — Learning Center: not part of the current tranche.
- Stage 8 — Writable Operations: not authorized.
- Stage 9 — Paper Soak & Experiment Analytics: not authorized yet.
- Live trading: not authorized.

## Current engineering posture

- Claude Code is the engineering lead/orchestrator.
- Use native Claude Code delegation and progressive context loading.
- Prefer cheap/focused subagents for bounded research/testing; use agent teams only when their coordination capability materially helps.
- Git is durable project memory. Project auto-memory is disabled.
- ChatGPT performs the independent external checkpoint review and accepted GitHub merge/sign-off.
