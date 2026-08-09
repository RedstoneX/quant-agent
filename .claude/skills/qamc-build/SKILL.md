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

Optimize for **efficient completion**: reduce wall-clock time without wasting model usage or creating coordination overhead. Neither token-minimization nor maximum-concurrency is the goal; choose the smallest amount of parallelism that materially shortens the critical path.

At the start of substantial work, identify the dependency graph and critical path. Keep the lead productive on the critical path while dispatching genuinely independent work concurrently.

The operator pre-authorizes autonomous delegation inside the accepted work contract. Do not pause merely to ask whether to parallelize. Claude may choose and combine:
- fast built-in/focused subagents for repository search, bounded analysis and high-volume reading;
- `qamc-test-runner` for cheap independent test/check work;
- stronger isolated general-purpose workers for self-contained implementation/debugging;
- worktrees or explicit file ownership for parallel writers;
- an agent team when sustained cross-layer work clearly benefits from peer coordination and independent contexts.

### Practical concurrency budget

- Small or tightly coupled task: lead alone or **1 helper**.
- Normal substantial QAMC work: usually **1–2 helpers alongside the lead**.
- Clearly separable cross-layer work: usually **2–3 helpers alongside the lead**, with separate ownership/worktrees where needed.
- Agent teams are available but **not the default**. Start small and use them only when multiple substantial independent workstreams justify the extra context/usage cost. Add another worker only when it is likely to shorten the critical path more than its coordination/context cost.

These are heuristics, not hard caps. Claude may use fewer or more when the task structure clearly justifies it, without asking the operator for permission.

Prefer cheaper workers for bounded search/test/triage and stronger workers where reasoning or implementation complexity warrants it. Reuse or resume an existing worker when that avoids repeated repository/context loading. Do not wait serially for a background worker when other authorized critical-path work can continue.

Avoid concurrent writers on the same files. Prefer separate modules/worktrees/ownership boundaries, then integrate centrally. Permanent custom agents are for recurring roles that earn their maintenance cost; use temporary task-specialized workers for one-off needs rather than growing the repository control plane.

## Safety

Root `CLAUDE.md` and path-scoped rules remain mandatory. Do not silently widen scope into trading/risk changes, broker-write Mission Control operations, distributed infrastructure, broad canonical-schema redesign, fake UI data, or optional integrations that become separate projects.

Parallel workers and teammates inherit the same QAMC safety boundaries. Parallelism never authorizes scope that the lead itself does not have.

## Verification / handoff

Use targeted checks while implementing and real runtime/visual verification for UI work where available. Use fresh independent review at meaningful gates.

A numbered stage or implementation slice is **not automatically an external gate**. When `STATE.md` / `WORK.md` authorize a multi-stage tranche, close intermediate slices with `/qamc-checkpoint` as internal gates and continue without asking for permission. Do not STOP solely because a stage number changed.

At an explicitly external gate, use `/qamc-checkpoint`, push the branch and **STOP** for ChatGPT/operator review. Never merge the implementation PR yourself.
