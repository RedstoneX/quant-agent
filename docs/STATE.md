# QAMC Current State

Updated: 2026-08-09

This is the single live operational-state document. Git history preserves history; this file says what is true **now**.

## Accepted implementation

- Stage 0 — Baseline & Integration-Seam Audit: DONE / Checkpoint A accepted.
- Stage 0.5 — Actual-model attribution hotfix: DONE / Checkpoint A5 accepted.
- Stage 1 — Provider, Model & Correlation Plumbing: DONE / Checkpoint B accepted.
- Stage 2 — Thin Read-Only Mission Control API: DONE / Checkpoint C accepted.
- Final accepted Stage-2 verification reported: **1530 passed, 0 failed**.

Stage 2 includes the separate read-only Mission Control API, broker-live/account reads separated from SQLite history reads, independent SQLite `mode=ro` access, accepted account/position/order/trade/run/decision/agent/reflection/candidate resources, and additive `risk_gate` forensic records. Deterministic trading/risk/execution semantics were not changed.

## Current phase

**Outcome discovery / architecture challenge for Mission Control.**

The previously proposed Stage 3–5 product capabilities remain the current candidate scope, but their architecture, sequencing and implementation assumptions are **provisional until Claude Code has independently explored/challenged them and the result is reconciled through GitHub**.

Current work contract: `docs/work/ACTIVE.md`.

### Authorized now

Claude Code may:
- inspect the actual repository, tests, accepted Stage-0–2 implementation, architecture and donor material;
- use `/qamc-discover` and focused subagents to investigate efficiently;
- challenge the current plan against `docs/OUTCOME.md`;
- ask the operator only genuine product/value questions, **one at a time**;
- update and push the discovery contract/proposal on a dedicated branch.

### Not authorized during discovery

- Mission Control product implementation;
- deterministic risk/execution changes;
- write-capable Mission Control operations;
- Stage 7+ implementation;
- live trading.

Claude must STOP after pushing the discovery result. ChatGPT then independently reviews the actual GitHub findings, reconciles material architecture questions, and the operator accepts/rejects the resulting outcome contract.

After an accepted discovery/reconciliation result is merged, implementation begins in a **fresh Claude Code session** from GitHub. The implementation session should not require the discovery transcript.

## Engineering posture

- Operator: defines desired outcomes/product preferences and makes final acceptance decisions.
- Claude Code: engineering/architecture participant during discovery; engineering lead/orchestrator during accepted implementation.
- ChatGPT: architecture challenger/reconciliation layer, independent checkpoint reviewer, and accepted GitHub merge/sign-off layer.
- GitHub: durable shared memory and handoff between Claude sessions and between Claude and ChatGPT.
- Project auto-memory is disabled.
- Secret reads/writes and dangerous permission modes are repo-controlled; safe routine checks are selectively pre-authorized.
- Prefer focused inexpensive subagents for bounded work; use agent teams only when their coordination capability materially helps.
