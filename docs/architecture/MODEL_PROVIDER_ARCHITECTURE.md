# Model & Provider Architecture — Accepted Contract

Status: **Stages 0.5 / 1 accepted — 2026-08-09**.

## Contract

QAMC supports per-agent model/provider experimentation without changing trading logic or silently corrupting experimental attribution.

- Model/provider configuration is backend-owned.
- Provider resolution remains centralized below the agent layer.
- OpenRouter support uses the existing provider seam.
- `BaseAgent._execute()` retry/deadline/failover behavior is hardened; do not casually rewrite it.

For relevant LLM calls, persist/analyze enough evidence to distinguish requested intent from what actually answered:
- agent;
- requested provider/model;
- actual provider/model / fallback;
- prompt version;
- token use where available;
- cost;
- latency/status/completion metadata where available;
- run/decision correlation.

Current canonical telemetry is additive `agent_logs` data plus `decision_id` correlation into trades. UI/analytics must not invent a parallel attribution store.

## Experiment integrity

A fallback result must never be counted as though the requested model answered. Provider/model comparison uses actual recorded execution evidence.

Future UI controls, if authorized, may edit validated backend configuration; they do not become the provider router.

## Known limitations

- Technical-analyst multi-chunk work can be persisted as one logical call, so per-chunk attribution is incomplete.
- An opaque external relay cannot be independently proven to have served the model it claims.
- Direct Google AI Studio/Gemini integration was evaluated and deferred; it is not required by current work.

Historical evidence: `docs/history/STAGE0_BASELINE_AUDIT.md`, `docs/history/CHECKPOINT_B_ACCEPTANCE.md`, and Git history.
