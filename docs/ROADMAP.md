# QAMC Roadmap

This is the concise active product roadmap. `docs/OUTCOME.md` defines the desired result; `docs/work/ACTIVE.md` contains the current discovery/implementation contract. Historical milestone narratives are preserved under `docs/history/legacy/`.

| Phase / Stage | Outcome | Gate | Status |
|---|---|---|---|
| 0 | Baseline & integration-seam audit | A | DONE |
| 0.5 | Actual-model attribution hotfix | A5 | DONE |
| 1 | Provider/model/correlation plumbing | B | DONE |
| 2 | Thin read-only Mission Control API | C | DONE |
| Discovery R1 | Claude independently challenges the Mission Control plan against the outcome and actual repo | architecture reconciliation | **ACTIVE** |
| 3 | Native cockpit capability | internal candidate gate | PROVISIONAL / HELD |
| 4 | AI decision-understanding capability | internal candidate gate | PROVISIONAL / HELD |
| 5 | Journal & forensic search capability | E candidate external gate | PROVISIONAL / HELD |
| 6 | AgentLens pilot | — | REMOVED |
| 7 | Learning Center | G | OPTIONAL / NOT AUTHORIZED |
| 8 | Writable Operations | H | OPTIONAL / NOT AUTHORIZED |
| 9 | Paper Soak & Experiment Analytics | final experiment gate | NOT AUTHORIZED |

There is no live-trading implementation milestone in the active plan.

## Discovery R1 — current work

Claude Code must explore the actual repository and challenge whether the existing Stage 3–5 architecture and sequencing are the best path to `docs/OUTCOME.md`.

The prior Stage 3–5 plan is evidence and a starting hypothesis, not an instruction to preserve.

Discovery should produce a concise KEEP / CHANGE / REMOVE / ADD proposal and an outcome/acceptance contract in `docs/work/ACTIVE.md`, then STOP for ChatGPT/operator reconciliation before any product implementation.

## Candidate Mission Control capabilities

These outcomes remain desired unless discovery produces a better grouping/sequencing:

### Native cockpit
- polished QAMC-native browser/iPad Mission Control;
- real account/P&L, positions, orders, trades, candidates and health;
- useful financial visualization;
- no production mock trading state;
- frontend failure has zero trading impact.

### AI decision understanding
- specialist role/provider/model/recommendation/confidence/reasoning/cost;
- disagreement/consensus;
- PM proposal → AI Risk → deterministic gate → execution/rejection;
- proposed-versus-executed delta;
- one decision understandable end-to-end without raw logs.

### Journal & forensic search
- useful calendar/list/daily journal from canonical QAMC records;
- rebuildable structured/full-text search/read model;
- useful forensic queries;
- no arbitrary LLM-generated SQL;
- no second authoritative memory system.

## After discovery

If the reconciled GitHub contract is accepted, implementation starts from a fresh Claude Code session. Claude then owns decomposition, orchestration, implementation and self-verification inside the accepted outcome contract.

The implementation gate structure itself may be adjusted by the discovery/reconciliation result; the next external acceptance point must still be explicit in `docs/STATE.md` before coding begins.
