# AgentLens — RETIRED (dropped from the QAMC plan, 2026-08-09)

> **Status: CLOSED.** AgentLens is **not part of QAMC.** Stage 6 was removed
> from the roadmap at Stage 0 sign-off — dropped, not deferred. This document is
> kept as the decision record, not as an active architecture contract. Do not
> instrument, integrate, fork or pilot AgentLens on the strength of anything
> written here.
>
> Superseded decisions: `DECISIONS.md` #23, #24, #25.
> Evidence: `docs/STAGE0_BASELINE_AUDIT.md` §8B / §8C.
> Retired-scope entry: `docs/MILESTONES.md` → "Retired scope".

## What was evaluated

`tranhoangtu-it/agentlens`, MIT, inspected at
**`21ab445a91bf2bc2f8b7eb0a2a8fb70468a9047f`** (2026-03-30 — the default-branch
tip at inspection). Self-described as "Chrome DevTools for AI Agents": Python /
TypeScript / .NET / Go SDKs, a FastAPI server, a React dashboard, a CLI and a
VS Code extension.

## What was good about it

Recorded honestly, because the drop is not a quality judgement:

- **Verifiably non-blocking.** Every emit path in `sdk/agentlens/transport.py`
  uses a `daemon=True` thread, a 5 s httpx timeout and a blanket
  `except Exception → logger.debug`. A dead sidecar cannot raise into or stall
  the caller — it would have satisfied `SAFETY_BOUNDARIES.md` #5.
- **Operationally light.** One Docker container, SQLite/WAL by default,
  Postgres optional. No Redis, no Kafka, no queue — consistent with DECISION #31.
- **Clean extension points.** `SpanExporter` / `SpanProcessor` protocols, an
  OTel exporter and an OTel ingest route.

Two qualifications on "non-blocking": daemon threads lose in-flight traces at
process exit (quant-agent sessions are short one-shots, so this matters), and
`flush_batch()` clears its queue *before* POSTing, so a failed send silently
drops traces. Non-blocking is achieved by discarding data.

## Why it was dropped

1. **Architectural mismatch.** It exists to answer "why did the agent choose
   tool A over tool B, and where in a deep call tree did the reasoning break?"
   quant-agent runs **nine flat single-shot prompt→JSON calls** per session.
   The span tree it is built to visualize barely exists here.
2. **Near-total overlap with existing capability.** `agent_logs` already stores
   the full prompt (`input_message`), the full response (`full_response`),
   model, token split and cost, joined by `run_id` to the trades produced.
   `scripts/replay_decision.py` *re-executes* a stored input through the
   current prompt+model and structurally diffs the resulting decisions —
   strictly stronger than AgentLens's trace-compare, which only diffs two
   historical recordings.
3. **Weaker search than QAMC can build natively.** `storage.list_traces()`
   offers exactly one filter: a SQL `LIKE` on `agent_name`. No full-text search
   over prompts or responses, no FTS table. Stage 5 builds SQLite FTS5 over
   richer data QAMC already owns.
4. **No project/workspace dimension.** Isolation is per `user_id` only — the
   dimension the old "deferred fork candidates" list named is simply absent.
5. **All remaining work would be QAMC-side.** None of its six auto-integrations
   (langchain / crewai / autogen / llamaindex / google_adk / mcp) applies —
   quant-agent calls the OpenAI and Anthropic SDKs directly — so instrumenting
   means hand-writing spans into `base.py:_execute()`, the one loop the seam
   analysis says to leave alone. There is **no redaction anywhere in the SDK**;
   QAMC would write and own it.
6. **Dormant, single-author upstream:** 69 commits, 1 contributor, no activity
   for ~4.5 months at inspection. Piloting it would mean owning it — exactly
   what the old DECISION #24 set out to avoid.
7. **New secret surface** for its marquee "AI autopsy" feature: a BYO LLM API
   key stored server-side (`server/llm_provider.py`, `server/crypto.py`), on a
   service that by design holds verbatim prompts and responses.

This is the `AGENTS.md` engineering-effort cap and the `ACCEPTANCE_CRITERIA.md`
stop rule working as intended.

## Where the scope went

Forensic observability is now served entirely by native quant-agent records.

| Retired AgentLens capability | Native replacement |
|---|---|
| Trace capture of agent activity | `agent_logs.input_message` + `full_response`, written per call today |
| Decision/trade ↔ trace linking | `run_id`, already shared by `agent_logs` and `trades` |
| Trace search | Stage 5 indexed search over canonical records (SQLite FTS5) |
| Replay / compare | `src/replay.py` + `scripts/replay_decision.py` |
| Per-call model / tokens / cost | `agent_logs` columns — corrected by Stage 0.5, extended in Stage 1 |
| "Inspect AI Trace" journal section | Inspect the `agent_logs` rows for that `run_id` (`JOURNAL_AND_SEARCH.md`) |
| `TraceLink` UI component | `AgentLogLink` — deep-links a `run_id` to its agent-log rows (`UI_COMPONENT_MAP.md`) |
| AgentLens outage testing | Moot — no external observability dependency exists |

## Reconsideration condition

**Preserved deliberately.** Revisit AgentLens **only if QAMC's architecture
evolves toward deeper or tool-calling agent traces** — multi-step tool-using
agents, or a genuine need for a span timeline over the morning
`ThreadPoolExecutor` fan-out (macro / news / tech / earnings, which is a small
real tree). In that case:

- the pinned commit above is the starting point for a fresh evaluation;
- its non-blocking transport design was sound and would likely still hold;
- re-adding the stage costs nothing that was destroyed by this decision.

Absent that architectural change, this is closed. Do not reopen it on the
strength of a nicer dashboard or a new release.
