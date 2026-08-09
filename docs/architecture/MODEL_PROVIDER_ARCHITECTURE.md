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

## Stage 1 implementation result (2026-08-09)

Implemented on branch `claude/stage-1-qamc-integration-m1n0pw`, following the
"Recommended seam ordering" above almost exactly. Full account:
`docs/MILESTONES.md` Stage 1.

- **Attribution (item 1)**: done at Stage 0.5, not Stage 1 (separate hotfix).
- **Schema (item 2)**: done. Nine nullable `agent_logs` columns
  (`requested_provider`, `requested_model`, `actual_provider`,
  `prompt_version`, `latency_s`, `status`, `finish_reason`, `truncated`,
  `decision_id`) plus `trades.decision_id`, all via `_ensure_column`.
- **Producer (item 3)**: done. `AgentResult` gained `requested_model`,
  `requested_provider`, `actual_provider`, `used_fallback`, `prompt_version`,
  `latency_s` — all captured at the entry/exit of `_execute()`, never inside
  the retry loop body.
- **Provider abstraction (item 4)**: implemented narrower than the advisory
  "Provider strategy object with `build_client()`/`call()`" suggested. A
  single `resolve_provider(model, explicit_provider)` function in
  `src/agents/base.py` is the source of truth for provider selection
  (explicit override wins; unset falls through to the pre-existing prefix
  chain), reused by `BaseAgent.__init__`, `AppConfig._check_llm_provider_keys`,
  and `pipeline.py`'s `_key_for`. OpenRouter — the one new provider added —
  needed no new `_call_*` method at all (OpenAI-wire-compatible, reuses
  `_call_openai`), so the full strategy-object shape wasn't justified for a
  single new provider whose call path is identical to an existing one. That
  shape remains the right one IF Google AI Studio (a genuinely
  different-shaped call path) is ever added.
- **Correlation IDs (item 5)**: done, exactly as sized here — one new
  `decision_id` column, generated once per successful PM call, not a
  distributed tracing system.

**Required contract**: satisfied. Every field the contract lists (agent,
requested provider/model, actual provider/model, prompt version,
input/output tokens, cost, latency, status, run/decision correlation) is now
captured and persisted, with unknowns staying `NULL` rather than fabricated.

**Experiment integrity**: satisfied. `used_fallback`/`status="fallback"` make
every failover explicit in the persisted record; `requested_provider` is
never overwritten by what the fallback actually used.

**Google free-tier use**: evaluated at the Stage 1 synthesis gate and
deferred, not built. Its SDK/message-shape/usage-field differences make it a
genuinely new call path rather than an extension of the OpenAI-compatible
seam OpenRouter used — implementing "one generic mechanism" (per governance)
meant choosing OpenRouter for Stage 1. `resolve_provider`'s explicit-override
design accommodates a Google path later without further restructuring.

**Known limits still open, unchanged by Stage 1**: F-3 (tech_analyst's
per-chunk collapse to one `agent_logs` row) is less lossy than before
(`used_fallback`/`truncated` now OR across chunks, `latency_s` summed) but the
structural one-row-per-N-calls limitation itself is not fixed — doing so
would mean a schema/call-site change disproportionate to Stage 1's bounded
scope. F-4 (relay attribution ceiling — QAMC cannot independently verify what
model an `OPENAI_BASE_URL` relay actually served) is unchanged; no amount of
QAMC-side plumbing raises this ceiling.
