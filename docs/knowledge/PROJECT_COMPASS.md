# QAMC Project Compass

## Plain-English operator view
![[OPERATOR_SUMMARY]]

## Current state
- Repository: `RedstoneX/quant-agent`, controlled fork of `yebof/quant-agent`.
- Bootstrap baseline: upstream commit `6fc3cf14f4e6f9fde5f6c10fbe4a8d51e3d0f4e7`
  — **verified in Stage 0**. The fork is level with upstream `main` plus
  documentation-only commits; no source divergence.
- **Stage 0 — Baseline & Integration-Seam Audit: DONE. Checkpoint A ACCEPTED
  2026-08-09.** Report: `docs/STAGE0_BASELINE_AUDIT.md`.
- **Stage 0.5 — D-1 Actual-Model Attribution Hotfix: IMPLEMENTED 2026-08-09 on
  branch `claude/stage-0-5-attribution-hotfix-nbjkep`, awaiting operator
  Checkpoint A5 acceptance.** All nine `insert_agent_log(...)` call sites
  (`pipeline_stages.py:280,328,483,699`; `pipeline.py:4704,6109,6296,
  6654/6668,7050`) now persist `AgentResult.model` (the model that actually
  answered, including cross-provider failover) instead of
  `config.llm.<agent>_model` (the configured/requested model). Five targeted
  regression tests added (`tests/test_agent_log_attribution.py`), one per
  session type, covering all nine sites; each fails pre-fix and passes
  post-fix. Full suite: 1436 passed, 0 failed. No schema change; `base.py::
  _execute()` untouched. Details: `docs/MILESTONES.md` Stage 0.5.
- Stage 1 remains **BLOCKED** until Checkpoint A5 (Stage 0.5) is accepted.
- Feature implementation: **not started** beyond the authorized Stage 0.5 scope.
- Live trading: **not authorized**.
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

## Current architectural headline
Keep quant-agent intact. Add minimal provider/telemetry/API seams. Build a
QAMC-native React dashboard using OpenTradex and Orallexa as selective
component/design donors, with TradingView Lightweight Charts. **Forensic
observability is native — `agent_logs` + `run_id` + `scripts/replay_decision.py`
— with no external observability service.**

## Next bounded task — Stage 0.5 — IMPLEMENTED, awaiting Checkpoint A5 acceptance
Persisted the model that **actually answered** at the nine `insert_agent_log(...)`
call sites listed in `docs/STAGE0_BASELINE_AUDIT.md` §9A, plus targeted tests
proving a cross-provider failover records the failover model. Behaviour-neutral
for trading. See `docs/MILESTONES.md` Stage 0.5 for the implementation record.

**Out of scope, do not touch:** `src/agents/base.py::_execute`, the database
schema, provider routing, correlation IDs, latency/prompt-version capture,
OpenRouter — all Stage 1. None were touched.

## Stage 0 outcome (for reference)
- Baseline suite: **1431 passed, 0 failed, 0 skipped** (hermetic; no network,
  no API keys). Container setup requires a venv — system pip cannot build `ta`.
- Decision chain, Alpaca lifecycle, persistence, reflection/Meta and scheduler
  mapped in the audit report.
- Integration seams identified and **not implemented**, cheapest first:
  actual-model attribution (Stage 0.5), additive `agent_logs` columns via the
  existing `_ensure_column` migration, provider strategy *below* `_execute()`,
  `decision_id` on `trades` for decision-level correlation.
- 10 discrepancies recorded (D-1 … D-10).

## Donor status (all pinned and inspected)
| Donor | Repository | Commit | Verdict |
|---|---|---|---|
| OpenTradex | `deonmenezes/opentradex` | `30b23f5e` | keep — layout/visual language only |
| Orallexa | `alex-jb/orallexa-ai-trading-agent` | `794a2ec0` | keep — concepts verified; **adapt, don't vendor** |
| TradingView Lightweight Charts | `tradingview/lightweight-charts` | library dep | keep |
| ~~AgentLens~~ | `tranhoangtu-it/agentlens` | `21ab445a` | **DROPPED — out of plan** (DECISION #34) |

## Discrepancy status
- **D-1** — authorized as Stage 0.5.
- **D-2, D-3** — resolved at Stage 0 sign-off.
- **D-4 … D-10** — open and unassigned; see the `docs/DECISIONS.md` conflict
  register. None blocks Stage 0.5.

## Non-goals now
No OpenRouter implementation, no dashboard code, no journal code, no risk
changes, no schema changes and no repository restructuring. AgentLens is out of
plan — do not instrument, integrate or pilot it (reconsideration condition in
`docs/architecture/AGENTLENS.md`). Stage 0 recommendations beyond Stage 0.5
remain advisory and must not be implemented without explicit authorization.
