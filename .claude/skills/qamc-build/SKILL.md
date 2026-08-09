---
name: qamc-build
description: Implement the currently accepted QAMC work contract. Use only when STATE and WORK explicitly authorize implementation.
---

# QAMC implementation workflow

## Authorization

Read:
1. `docs/STATE.md`;
2. `docs/WORK.md`;
3. `docs/OUTCOME.md`;
4. only accepted architecture/source material relevant to the work.

If `STATE.md` or `WORK.md` does not explicitly authorize implementation, **STOP and use `/qamc-discover` instead**.

Use Git history only for targeted evidence when current contracts/code are insufficient.

## Role

Own routine implementation architecture, decomposition, worker choice, integration, tests and debugging inside the accepted outcome contract.

Investigate repository facts; decide routine engineering choices; ask the operator only genuine product/value questions; stop and record material contract/safety conflicts in `docs/WORK.md` for external reconciliation.

Use the least expensive reliable mechanism: focused inexpensive workers for bounded reading/tests, stronger isolated workers only when needed, worktrees for parallel writers, full agent teams only when peer coordination materially helps.

## Safety

Root `CLAUDE.md` and path-scoped rules remain mandatory. Do not silently widen scope into trading/risk changes, broker-write Mission Control operations, distributed infrastructure, broad canonical-schema redesign, fake UI data, or optional integrations that become separate projects.

## Verification / handoff

Use targeted checks while implementing and real runtime/visual verification for UI work where available. Use fresh independent review at meaningful gates.

At an external gate, use `/qamc-checkpoint`, push the branch and **STOP** for ChatGPT/operator review. Never merge the implementation PR yourself.
