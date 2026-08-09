# Model & Provider Architecture — Accepted Contract

Status: **Stages 0.5 / 1 accepted — 2026-08-09**.  
Historical evidence: `docs/STAGE0_BASELINE_AUDIT.md`, `docs/CHECKPOINT_B_ACCEPTANCE.md`, and Git history.

## Goal

Allow per-agent model/provider experimentation without changing trading logic or silently corrupting experimental attribution.

## Accepted architecture

- Per-agent model configuration remains backend-owned.
- Per-agent provider override is supported without forcing frontend routing logic.
- Provider resolution remains centralized below the agent layer rather than duplicated across agents.
- Stage 1 added OpenRouter through the least-invasive existing provider seam.
- The hardened `BaseAgent._execute()` retry/deadline/failover behavior remains the reliability boundary; provider experimentation should not casually rewrite that loop.

## Attribution contract

For relevant LLM calls, QAMC must keep requested intent distinct from what actually answered.

Persist/analyze at least:
- agent;
- requested provider/model;
- actual provider/model;
- whether fallback occurred;
- prompt version;
- input/output token use where available;
- cost;
- latency;
- call status / available completion metadata;
- run/decision correlation.

Current persistence uses additive fields on existing `agent_logs` plus `decision_id` correlation into trades. This remains canonical experiment telemetry; the UI must not invent a parallel attribution store.

## Experiment-integrity rule

Fallback may improve resilience, but a fallback result must never be counted as though the requested model answered. Provider/model comparison must be based on **actual** recorded execution evidence.

Frontend controls, if later authorized, may edit validated backend configuration; they do not become the provider router.

## Known limitations / deferred work

- The technical analyst can aggregate multiple HTTP chunks into one persisted logical call, so per-chunk invocation attribution is not fully represented.
- When an external relay/proxy is used, QAMC can record the model/provider it requested and the response path it observed, but cannot independently prove an opaque relay served the claimed underlying model.
- A direct Google AI Studio/Gemini integration was evaluated and deferred because it would require a genuinely new provider call path; it is not required for current work.

These limitations are experiment-analysis concerns, not permission to widen the current Mission Control discovery scope.
