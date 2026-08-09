# QAMC Project Compass

## Plain-English operator view
![[OPERATOR_SUMMARY]]

## Current state
- Repository: `RedstoneX/quant-agent`, controlled fork of `yebof/quant-agent`.
- Bootstrap baseline: upstream commit `6fc3cf14f4e6f9fde5f6c10fbe4a8d51e3d0f4e7`
  — **verified in Stage 0**. The fork is level with upstream `main` plus
  QAMC changes under governed checkpoints.
- **Stage 0 — Baseline & Integration-Seam Audit: DONE. Checkpoint A ACCEPTED
  2026-08-09.** Report: `docs/STAGE0_BASELINE_AUDIT.md`.
- **Stage 0.5 — D-1 Actual-Model Attribution Hotfix: DONE. Checkpoint A5
  ACCEPTED 2026-08-09.** PR #6 merged. All nine `insert_agent_log(...)` call
  sites now persist `AgentResult.model` (the model that actually answered,
  including cross-provider failover) instead of the configured/requested model.
  Five targeted regression tests cover all nine sites. Full suite: **1436
  passed, 0 failed**. No schema change; `src/agents/base.py::_execute()`
  untouched. Details: `docs/MILESTONES.md` Stage 0.5.
- **Stage 1 — Provider, Model & Correlation Plumbing: NEXT / AUTHORIZED.**
- Live trading: **not authorized**. Alpaca Paper remains the broker boundary.
- AI development economy/session policy: `docs/knowledge/AI_OPERATING_SYSTEM.md`.

## Start here
1. `/AGENTS.md`
2. `/CLAUDE.md` (upstream invariants)
3. `/docs/knowledge/AI_OPERATING_SYSTEM.md` (model/session/context economy)
4. `/docs/DECISIONS.md`
5. `/docs/MILESTONES.md`
6. `/docs/ACCEPTANCE_CRITERIA.md`
7. `/docs/STAGE0_BASELINE_AUDIT.md` (verified source map, seams, discrepancies)
8. Architecture document(s) relevant to the task.

## AI execution policy headline
Use **Sonnet by default for implementation**, reserve **Opus** for architecture/audit, difficult debugging, high-risk decisions and major independent reviews, and use Haiku only for genuinely mechanical/simple work. Start a **fresh Claude Code session for each bounded milestone/slice**, rehydrate from GitHub rather than old transcripts, use targeted tests while developing, and run the full suite at governed checkpoints. If context/usage balloons, STOP and split the work rather than carrying a long session forward. Full policy: `docs/knowledge/AI_OPERATING_SYSTEM.md`.

For stages with genuinely independent work packages, the primary Claude Code
session may act as the **orchestrator** and delegate bounded work to subagents in
parallel. The orchestrator owns interface decisions, integration, conflict
resolution, final tests and the checkpoint report. Do not let multiple workers
edit the same files concurrently merely to increase parallelism.

## Current architectural headline
Keep quant-agent intact. Add minimal provider/telemetry/API seams. Build a
QAMC-native React dashboard using OpenTradex and Orallexa as selective
component/design donors, with TradingView Lightweight Charts. **Forensic
observability is native — `agent_logs` + `run_id` + `scripts/replay_decision.py`
— with no external observability service.**

## Next bounded task — Stage 1 (authorized)
Implement the governed **Provider, Model & Correlation Plumbing** stage:

- explicit provider/model configuration compatible with existing per-agent settings;
- OpenRouter and/or Google AI Studio path through the least-invasive provider seam;
- preserve resilience without silent experimental attribution;
- persist actual provider/model/tokens/cost/latency/status as required by the frozen contract;
- add only the correlation identifiers minimally necessary to trace run → decision → order/trade → prompt/model version;
- preserve the hardened retry/deadline/failover behavior in `BaseAgent._execute()` rather than casually refactoring it.

Checkpoint B requires paper-trading/risk behavior unchanged, attribution correct,
and tests green. **STOP at Checkpoint B; do not begin Stage 2.**

## Stage 0 / 0.5 outcome (for reference)
- Baseline suite at Stage 0: **1431 passed, 0 failed, 0 skipped** (hermetic; no
  network, no API keys). Container setup requires a venv — system pip cannot
  build `ta`.
- Stage 0.5 suite: **1436 passed, 0 failed** after five new attribution tests.
- Decision chain, Alpaca lifecycle, persistence, reflection/Meta and scheduler
  mapped in the audit report.
- Integration seams identified, cheapest first: actual-model attribution
  (**completed Stage 0.5**), additive `agent_logs` columns via existing
  `_ensure_column`, provider strategy below the hardened execution loop, and a
  minimal decision-level correlation addition where needed.

## Donor status (all pinned and inspected)
| Donor | Repository | Commit | Verdict |
|---|---|---|---|
| OpenTradex | `deonmenezes/opentradex` | `30b23f5e` | keep — layout/visual language only |
| Orallexa | `alex-jb/orallexa-ai-trading-agent` | `794a2ec0` | keep — concepts verified; **adapt, don't vendor** |
| TradingView Lightweight Charts | `tradingview/lightweight-charts` | library dep | keep |
| ~~AgentLens~~ | `tranhoangtu-it/agentlens` | `21ab445a` | **DROPPED — out of plan** (DECISION #34) |

## Discrepancy status
- **D-1 — RESOLVED by Stage 0.5; Checkpoint A5 accepted.**
- **D-2, D-3 — resolved at Stage 0 sign-off.**
- **D-4 … D-10** remain in the conflict register unless explicitly resolved by
  a governed stage. Stage 1 may address only those that naturally fall inside
  its authorized provider/model/correlation scope.

## Non-goals now
No dashboard/API/journal implementation, no risk-policy redesign, no live
trading, no repository restructuring, and no AgentLens integration. Stage 1
must not expand into Stage 2+ merely because adjacent work is convenient.
