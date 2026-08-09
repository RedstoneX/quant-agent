# Model & Provider Architecture — Accepted Contract

Status: **Stages 0.5 / 1 accepted — 2026-08-09**.

QAMC supports per-agent model/provider experimentation without changing trading logic or silently corrupting experimental attribution.

- Model/provider configuration is backend-owned.
- Provider resolution remains centralized below the agent layer.
- OpenRouter support uses the existing provider seam.
- `BaseAgent._execute()` retry/deadline/failover behavior is hardened; do not casually rewrite it.

For relevant LLM calls, retain enough evidence to distinguish requested intent from what actually answered: agent, requested provider/model, actual provider/model/fallback, prompt version, token use where available, cost, latency/status/completion metadata where available, and run/decision correlation.

Current canonical telemetry is additive `agent_logs` data plus `decision_id` correlation into trades. UI/analytics must not invent a parallel attribution store.

A fallback result must never be counted as though the requested model answered. Future UI controls, if authorized, may edit validated backend configuration; they do not become the provider router.

Known limitations:
- technical-analyst multi-chunk work can be persisted as one logical call, so per-chunk attribution is incomplete;
- an opaque external relay cannot be independently proven to have served the model it claims;
- direct Google AI Studio/Gemini integration was evaluated and deferred; it is not required by current work.

Detailed acceptance evidence remains in Git history. The last pre-ultra-lean working-tree snapshot is commit `02e20e6ac1c5c7e65b7f512f76c568328c990e3c`.
