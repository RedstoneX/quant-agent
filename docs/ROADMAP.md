# QAMC Roadmap

This is the concise active roadmap. Historical milestone narratives are preserved under `docs/history/legacy/`.

| Stage | Outcome | Gate | Status |
|---|---|---|---|
| 0 | Baseline & integration-seam audit | A | DONE |
| 0.5 | Actual-model attribution hotfix | A5 | DONE |
| 1 | Provider/model/correlation plumbing | B | DONE |
| 2 | Thin read-only Mission Control API | C | DONE |
| 3 | QAMC Native Cockpit | D (internal in current tranche) | AUTHORIZED |
| 4 | AI Decision Interface | internal tranche gate | AUTHORIZED |
| 5 | Native Journal & Indexed Search | E (external STOP) | AUTHORIZED |
| 6 | AgentLens pilot | — | REMOVED |
| 7 | Learning Center | G | OPTIONAL / NOT AUTHORIZED |
| 8 | Writable Operations | H | OPTIONAL / NOT AUTHORIZED |
| 9 | Paper Soak & Experiment Analytics | final experiment gate | NOT AUTHORIZED |

There is no live-trading implementation milestone in the active plan.

## Stage 3 — Native Cockpit

Outcome:
- QAMC-native React/Vite/Tailwind frontend;
- TradingView Lightweight Charts;
- real API-backed account/P&L, positions, orders, trades, candidates, health;
- responsive browser/iPad experience;
- no production mock trading state;
- frontend failure has zero trading impact.

Gate D is an internal self-verification/commit boundary in the current Stage 3–5 tranche.

## Stage 4 — AI Decision Interface

Outcome:
- specialist agent role/provider/model/recommendation/confidence/reasoning/cost;
- disagreement/consensus;
- PM proposal → AI Risk response → deterministic gate → execution/rejection;
- proposed → executed delta;
- native agent-call drill-down;
- one candidate/decision understandable end-to-end without raw logs.

This is an internal self-verification/commit boundary in the current tranche.

## Stage 5 — Native Journal & Indexed Search

Outcome:
- calendar/list/structured daily journal derived from canonical QAMC records;
- required daily sections from `docs/architecture/JOURNAL_AND_SEARCH.md`;
- rebuildable server-side structured/full-text search/read model;
- useful forensic queries;
- no arbitrary LLM-generated SQL;
- no second authoritative memory system.

**Checkpoint E is the next external STOP.**

## Later

Stage 7 may expose existing Meta Reflector reports/diffs/history/rollback.
Stage 8 may add tightly validated/audited operator writes only after the read-only system is mature.
Stage 9 performs long-running paper stability and experiment analytics.

None of those later stages are part of the current authorization.
