# Required Screen States

Mockups/implementation should cover at least these states rather than one pretty dashboard screenshot.

## A. Normal / No Trade
Candidates and agent views populated; PM may hold; no order implied.

## B. Proposed Trade
Show agent evidence, disagreement, PM proposed side/size/confidence and pending risk evaluation.

## C. Rejected/Reduced Trade
Show AI Risk response and deterministic rejection/reduction reason prominently; no ambiguity that nothing/less was executed.

## D. Executed Trade
Show actual Alpaca order/fill and chart marker; preserve proposed→executed delta.

## E. Journal Day
Structured thesis/candidates/agents/risk/trades/results/lessons/tomorrow with a
link into that day's `agent_logs` rows by `run_id` (was "raw-trace link";
re-scoped 2026-08-09 — DECISION #35).

## F. Learning Proposal
Meta Reflector evidence, affected agent, before/after prompt diff, historical versions and approve/reject controls (later milestone only).

## G. Degraded Services
Dashboard API disconnected; provider quota/error; scheduler session missed. The UI
must clearly distinguish observability failure from trading-engine failure.
(The "AgentLens down" state is retired — QAMC has no external observability
dependency. DECISION #34.)
