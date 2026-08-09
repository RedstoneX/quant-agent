# AgentLens Policy

## Role
AgentLens answers “How did the AI execution unfold?” Mission Control/journal answer “What happened and why does it matter?”

> **Stage 0 finding (D-3): "AgentLens" is not identified.** No governed
> document records a repository, license or commit. At least three unrelated
> public GitHub projects use the name (`Soufianeazz/agentlens` — quality
> scoring / hallucination detection; `agentkitai/agentlens` — tamper-evident
> hash-chained audit log; `tranhoangtu-it/agentlens` — tool-call tracing),
> plus an unrelated Salesforce product. They solve materially different
> problems, so the "low upstream maturity" judgement below cannot be verified
> against any of them. **Stage 6 should stay deferred until the target is
> pinned by SHA.** See `docs/STAGE0_BASELINE_AUDIT.md` §8.

## Current decision
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
