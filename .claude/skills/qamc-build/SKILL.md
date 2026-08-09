---
name: qamc-build
description: Execute the currently authorized QAMC implementation stage or tranche. Use when the operator asks to continue/build/implement QAMC.
---

# QAMC authorized build workflow

## Live repository state

```!
git branch --show-current
git status --short
```

First read:
1. `docs/STATE.md`;
2. `docs/ROADMAP.md`;
3. `docs/decisions/ACTIVE.md`;
4. only the architecture/UI source documents relevant to the currently authorized work.

Do **not** preload historical governance/audit material. Use `docs/history/` only when a specific historical fact is needed.

## Role

Act as the engineering lead/orchestrator.
Own architecture within accepted boundaries, work decomposition, worker choice, integration, tests, debugging, and implementation documentation.

Do not ask the operator for routine implementation choices that repository evidence or engineering judgment can resolve.

## Cost-aware orchestration

Use the least expensive mechanism that can reliably do each job:

1. Keep architecture, integration, difficult reasoning, and final synthesis in the lead.
2. Use built-in Explore / focused Haiku subagents for repo search, file discovery, logs, bounded test investigation, and high-volume reading where only the conclusion matters.
3. Use a stronger focused worker for implementation or review that actually needs it.
4. Use worktree isolation for parallel writers with disjoint ownership.
5. Use full agent-team teammates only when sustained peer-to-peer communication/self-coordination materially improves the result.

Do not maximize worker count. Maximize engineering quality per unit of usage.
Agent teams are optional/experimental; immediately fall back to subagents/worktrees if team machinery is unavailable or creates friction.

## Current tranche behavior

Follow the authorization in `docs/STATE.md`.

For the currently authorized Stage 3–5 Mission Control tranche:
- Stage 3 and Stage 4 are internal gates, not external STOPs;
- self-verify each, run relevant targeted checks, perform independent review, and create a clear commit boundary;
- continue when the internal gate is green;
- Stage 5 / Checkpoint E is the next external STOP.

Do not start Stage 7+, writable operations, or live trading.

## Data and safety

All root `CLAUDE.md` invariants and path-scoped rules remain mandatory.
If the authorized UI/journal work genuinely requires new read-side API/read-model support, implement the minimum clean read-side extension while preserving Stage-2 isolation.

Stop and escalate rather than silently widening scope if success requires:
- deterministic risk/execution changes;
- write-capable Mission Control endpoints;
- a new distributed service;
- a broad safety-sensitive canonical-schema redesign;
- inability to prove a UI value comes from canonical/API-backed state;
- donor/integration work turning into a separate project.

## Verification

Use targeted tests during implementation.
For UI stages, run the application and perform real visual/runtime verification when tools allow.
At internal gates, invoke a fresh `qamc-reviewer`; invoke `qamc-ui-reviewer` for UI-heavy gates.

At the external gate, use `/qamc-checkpoint`.
