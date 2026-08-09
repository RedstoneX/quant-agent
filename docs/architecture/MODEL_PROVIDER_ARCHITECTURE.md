# Model & Provider Architecture

## Goal
Allow easy per-agent experimentation among Google/Gemini, OpenRouter-hosted models and other supported providers without changing agent code or losing attribution.

## Existing seam
quant-agent already has per-agent model settings and a centralized base-agent provider-routing layer. Stage 0 must verify exact current implementation/failover behavior.

## Required contract
Each invocation records at minimum:
- agent;
- requested provider/model;
- actual provider/model used;
- prompt version;
- input/output token counts;
- estimated/actual cost where available;
- latency;
- success/failure/fallback status;
- run/decision correlation identifier.

## Routing principle
Frontend model selectors modify validated backend configuration only. They do not perform routing.

## Experiment integrity
Existing cross-provider failover may be retained for resilience only if the actual fallback is explicit in records. No session may silently count a fallback model as the requested model.

## Google free-tier use
A Google AI Studio/Gemini path may be used for inexpensive/free routine agents subject to current quota. Quota exhaustion/failure must be visible, not silently hidden.
