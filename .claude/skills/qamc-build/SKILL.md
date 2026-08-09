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

## Orchestration priority

Optimize primarily for **elapsed completion time**, subject to correctness, safety and avoiding counterproductive coordination overhead. Token/model cost is secondary to wall-clock efficiency for authorized QAMC engineering work.

At the start of substantial work, identify the dependency graph and critical path. Keep the lead productive on the critical path while dispatching independent work concurrently.

The operator pre-authorizes autonomous delegation inside the accepted work contract. Do not pause merely to ask whether to parallelize. Claude may choose and combine:
- fast built-in/focused subagents for repository search, bounded analysis and high-volume reading;
- `qamc-test-runner` for independent test/check work;
- stronger isolated general-purpose workers for self-contained implementation/debugging;
- worktrees or explicit file ownership for parallel writers;
- an agent team when sustained cross-layer work benefits from peer coordination and independent contexts.

Choose worker count dynamically from actual task independence; do **not** spawn workers just to hit a number. For team-style work, roughly **3–5 teammates** is the normal starting range when there are enough independent workstreams; use fewer for tightly coupled work and scale only when additional concurrency materially shortens the critical path. Ordinary background subagents may be used more broadly for genuinely independent tasks within Claude Code's runtime limits.

Avoid concurrent writers on the same files. Prefer separate modules/worktrees/ownership boundaries, then integrate centrally. Reuse or resume an existing worker when that avoids repeated repository/context loading. Do not wait serially for a background worker when other authorized critical-path work can continue.

Permanent custom agents are for recurring roles that earn their maintenance cost; use temporary task-specialized workers for one-off needs rather than growing the repository control plane.

## Safety

Root `CLAUDE.md` and path-scoped rules remain mandatory. Do not silently widen scope into trading/risk changes, broker-write Mission Control operations, distributed infrastructure, broad canonical-schema redesign, fake UI data, or optional integrations that become separate projects.

Parallel workers and teammates inherit the same QAMC safety boundaries. Parallelism never authorizes scope that the lead itself does not have.

## Verification / handoff

Use targeted checks while implementing and real runtime/visual verification for UI work where available. Use fresh independent review at meaningful gates.

A numbered stage or implementation slice is **not automatically an external gate**. When `STATE.md` / `WORK.md` authorize a multi-stage tranche, close intermediate slices with `/qamc-checkpoint` as internal gates and continue without asking for permission. Do not STOP solely because a stage number changed.

At an explicitly external gate, use `/qamc-checkpoint`, push the branch and **STOP** for ChatGPT/operator review. Never merge the implementation PR yourself.
