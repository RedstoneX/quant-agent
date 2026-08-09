---
name: qamc-build
description: Implement the currently accepted QAMC work contract. Use only when docs/STATE.md and docs/work/ACTIVE.md explicitly authorize implementation.
---

# QAMC accepted implementation workflow

## Authorization gate

Before coding, read:
1. `docs/STATE.md`;
2. `docs/work/ACTIVE.md`;
3. `docs/OUTCOME.md`;
4. `docs/decisions/ACTIVE.md`;
5. only the **current/accepted architecture and source material** relevant to the accepted contract.

If `docs/STATE.md` says discovery/reconciliation is active, or `docs/work/ACTIVE.md` is not explicitly accepted for implementation, **STOP. Do not implement. Use `/qamc-discover` instead.**

Do not preload `docs/reference/`, `docs/history/`, historical audits, or prior proposals. Open a specific reference only when the accepted contract or an implementation question actually needs its evidence.

## Role

Act as the engineering lead/orchestrator for the **accepted outcome contract**.
Own routine implementation architecture inside accepted boundaries, decomposition, worker choice, integration, tests, debugging, and implementation documentation.

Do not reopen settled product decisions casually, but do not treat a discovery proposal as a file-by-file coding recipe. The implementation session owns the engineering details.

## Question routing during implementation

1. Repository fact → investigate.
2. Routine engineering choice → decide.
3. Genuine operator product/value decision not covered by the accepted contract → ask **one question at a time**.
4. Material architecture/safety/governance conflict with the accepted contract → stop and record the issue in `docs/work/ACTIVE.md` for external reconciliation.

Do not ask the operator to choose libraries, modules, worker topology, tests, data structures, or other normal engineering details unless the answer actually changes a product outcome they own.

## Cost-aware orchestration

Use the least expensive mechanism that can reliably do each job:
- keep architecture, integration, difficult reasoning and final synthesis in the lead;
- use Explore/focused inexpensive subagents for repository search, file discovery, logs, bounded tests and high-volume reading;
- use stronger focused workers for implementation/review that genuinely needs them;
- use worktree isolation for parallel writers with disjoint ownership;
- use full agent-team teammates only when sustained peer-to-peer communication materially improves the result.

Do not maximize worker count. Maximize engineering quality per unit of usage.

## Safety

All root `CLAUDE.md` invariants and path-scoped rules remain mandatory.
Stop/escalate rather than silently widening scope if success requires:
- deterministic risk/execution changes not explicitly accepted;
- write-capable Mission Control operations not explicitly accepted;
- a new distributed service without demonstrated need;
- a broad safety-sensitive canonical-schema redesign;
- inability to prove UI state comes from honest canonical/API-backed sources;
- optional donor/integration work becoming a separate project.

## Verification

Use targeted tests while implementing.
For UI work, run real runtime/visual verification when tools allow.
Use fresh independent reviewers at meaningful internal gates rather than relying on the author's own assessment.

At the external gate, use `/qamc-checkpoint`, push the branch and STOP for ChatGPT/operator review. Never merge the implementation PR yourself.
