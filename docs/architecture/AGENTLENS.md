# AgentLens Policy

## Role
AgentLens answers “How did the AI execution unfold?” Mission Control/journal answer “What happened and why does it matter?”

> ## Stage 0 completion — D-3 RESOLVED, and the recommendation is **DROP**
>
> **Identity (operator-supplied 2026-08-09):** `tranhoangtu-it/agentlens`,
> MIT, inspected at **`21ab445a91bf2bc2f8b7eb0a2a8fb70468a9047f`** (2026-03-30,
> the current default-branch tip). Full evidence:
> `docs/STAGE0_BASELINE_AUDIT.md` §8B / §8C.
>
> **Recommendation: DROP FROM THE PLAN.** Advisory — awaiting operator
> acceptance. Not integrated, not forked, nothing implemented.
>
> **What is genuinely good about it.** Operationally light (one Docker
> container, SQLite/WAL by default, no Redis/Kafka/queue — consistent with
> DECISION #31) and **verifiably non-blocking**: every emit path uses a
> `daemon=True` thread, a 5 s httpx timeout and a blanket
> `except Exception → logger.debug`. A dead sidecar cannot raise into or stall
> the caller, satisfying `SAFETY_BOUNDARIES.md` #5. Clean `SpanExporter` /
> `SpanProcessor` extension points and an OTel ingest route.
>
> **Why it should still be dropped:**
> 1. **Architectural mismatch.** It exists to explain deep nested agent traces
>    ("why tool A over tool B"). quant-agent runs **nine flat single-shot
>    prompt→JSON calls** per session. The span tree it visualizes barely exists.
> 2. **Near-total overlap.** `agent_logs` already stores the full prompt
>    (`input_message`), full response (`full_response`), model, token split and
>    cost, joined by `run_id` to the trades produced — and
>    `scripts/replay_decision.py` *re-executes* a stored input through the
>    current prompt+model and diffs the decisions. AgentLens's trace-compare
>    only diffs two historical recordings; replay is strictly stronger for the
>    prompt-change question QAMC actually asks.
> 3. **Its search is weaker than what QAMC can build natively.**
>    `storage.list_traces()` offers one SQL `LIKE` on `agent_name`. No
>    full-text search over prompts or responses, no FTS table. Stage 5 will
>    build SQLite FTS5 over richer data anyway.
> 4. **No project/workspace dimension exists** — isolation is per `user_id`
>    only. The deferred-fork candidate listed below is confirmed absent.
> 5. **Every remaining gap is QAMC-side work.** None of its six auto-
>    integrations (langchain / crewai / autogen / llamaindex / google_adk / mcp)
>    applies — quant-agent calls the OpenAI and Anthropic SDKs directly — so
>    instrumentation means hand-writing spans into `base.py:_execute()`, the one
>    loop the Stage 0 seam analysis says to leave alone. There is **no
>    redaction/scrubbing anywhere in the SDK**; QAMC would write it.
> 6. **Upstream is dormant and single-author**: 69 commits, 1 contributor, no
>    activity for ~4.5 months. Piloting it means owning it — exactly what
>    DECISION #24 set out to avoid.
> 7. **New secret surface** for the marquee "AI autopsy" feature: a BYO LLM API
>    key stored server-side (`server/llm_provider.py`, `server/crypto.py`), on a
>    service that by design holds verbatim prompts and responses.
>
> Two honest qualifications on the non-blocking claim: daemon threads lose
> in-flight traces at process exit (quant-agent sessions are short one-shots),
> and `flush_batch()` clears the queue *before* POSTing, so a failed send
> **silently drops** traces. Non-blocking is achieved by discarding data.
>
> **Counter-argument, recorded for balance.** If QAMC later adopts tool-calling
> or multi-step agents, or wants a span timeline over the morning
> `ThreadPoolExecutor` fan-out (macro/news/tech/earnings — a small real tree),
> AgentLens becomes a reasonable fit and its non-blocking design would hold up.
> Dropping Stage 6 forecloses nothing.
>
> **If the DROP is accepted:** strike Stage 6, and re-scope the `TraceLink`
> component (`docs/ui/UI_COMPONENT_MAP.md`) and the "Inspect AI Trace" daily
> section (`docs/architecture/JOURNAL_AND_SEARCH.md`) to link to `agent_logs`
> rows by `run_id` instead of to an external trace service.

## Prior decision (superseded pending operator acceptance of the DROP above)
**Pilot upstream AgentLens first. Do not fork it at project start.**

Stage 6 pilot requirements:
- best-effort/non-blocking tracing;
- QAMC-side secret/PII redaction before transmission;
- decision/trade ↔ trace ID linking;
- explicit outage testing;
- useful trace/replay/compare experience demonstrated.

Trading must continue normally if AgentLens is unavailable.

## Deferred fork candidates
Only if the pilot demonstrates value:
- first-class project/workspace dimension;
- substantially better indexed/full-text trace search;
- generic SDK/schema/UI enhancements;
- any maintainability fixes warranted by low upstream maturity.

These are not prerequisites for QAMC core success.
