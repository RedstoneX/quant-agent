# Workstreams & Parallelism

Parallelism begins only after Stage 0 establishes interfaces.

## Lead responsibility
One lead Claude session owns architecture compliance, integration, checkpoints and source-of-truth updates.

## Safe parallel work after prerequisites
- Stage 0: parallel **read-only** source inspection (core/provider; risk/execution; persistence/learning; donors), reconciled by lead.
- Stage 1: provider routing and correlation/telemetry can separate after interface agreement.
- Stage 2/3: API endpoints, native frontend shell and chart integration can parallelize after API contracts freeze.
- Stage 4: agent visualization and model/cost visualization can parallelize.
- Stage 5: after journal schema freeze, journal frontend, indexed-search backend and Suggested Investigations can parallelize.
- Stage 6: AgentLens pilot is naturally isolated and can use a separate worktree/session.
- Stages 7/8: reduce parallel writes; prompt mutation/risk/operational controls require tighter serialized review.

## Branch/worktree rule
Independent subagents must use bounded branches/worktrees when they modify code. No two agents should edit the same integration seam concurrently.
