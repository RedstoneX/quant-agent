# AgentLens Policy

## Role
AgentLens answers “How did the AI execution unfold?” Mission Control/journal answer “What happened and why does it matter?”

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
