# Model & Provider Architecture

## Goal
Allow easy per-agent experimentation among Google/Gemini, OpenRouter-hosted models and other supported providers without changing agent code or losing attribution.

## Existing seam
quant-agent already has per-agent model settings and a centralized base-agent provider-routing layer. Stage 0 must verify exact current implementation/failover behavior.

### Stage 0 verification result (2026-08-09)

**Routing exists and is centralized — attribution does not.** Full evidence in
`docs/STAGE0_BASELINE_AUDIT.md` §3.

Confirmed present: a single routing layer in `src/agents/base.py`; nine
per-agent model fields plus per-agent `max_tokens`; config-load validation that
the right provider key exists; a hardened retry/deadline/failover loop
(7 attempts with jitter, 480 s wall-clock deadline, 300 s HTTP timeout, 4xx and
DeepSeek-402 fast-fail, per-provider concurrency semaphores); single-shot
cross-provider failover to `claude-opus-4-7`; per-call token and cost
accounting priced against the model that actually answered.

Confirmed **absent**, against the "Required contract" below:

- **Actual model is never persisted (D-1).** All nine `insert_agent_log(...)`
  sites write `config.llm.<agent>_model` — the *requested* model.
  `AgentResult.model`, which holds the actual one, is read in exactly one
  internal place and never stored. On failover the row says `gpt-5.5` while
  `cost_usd` was priced at Anthropic rates. This is the silent fallback
  DECISION #12 prohibits.
- **No storage exists** for provider, requested-vs-actual, latency,
  prompt version, `finish_reason` or `truncated`. `agent_logs` has no such
  columns; `latency` / `prompt_version` appear nowhere but comments.
- **Provider is not a configuration concept** — it is derived from the model-id
  prefix at client construction.
- **`tech_analyst` logs one row per N HTTP calls**, keeping only the last
  chunk's model, so per-invocation attribution is structurally unavailable for
  that agent.
- **Relay ceiling:** with `OPENAI_BASE_URL` set, the recorded id is what we
  asked the relay for; nothing verifies what it served.

Recommended seam ordering (advisory, not implemented): fix the nine call sites
first (~9 lines, behavior-neutral); add nullable `agent_logs` columns through
the existing idempotent `_ensure_column` migration; add `provider`/`latency_ms`
to `AgentResult`; and place any provider strategy object **below** `_execute()`
— that loop's constants are tuned against dated production incidents and are
not covered by tests that would catch a regression.

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
