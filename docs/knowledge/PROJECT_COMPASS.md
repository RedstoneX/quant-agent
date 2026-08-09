# QAMC Project Compass

## Current state
- Repository: `RedstoneX/quant-agent`, controlled fork of `yebof/quant-agent`.
- Bootstrap baseline: upstream commit `6fc3cf14f4e6f9fde5f6c10fbe4a8d51e3d0f4e7`
  — **verified in Stage 0**. The fork is level with upstream `main` plus one
  docs-only commit; no source divergence.
- Architecture: frozen baseline documented in `docs/DECISIONS.md`.
- Feature implementation: **not started**.
- Stage 0 (Baseline & Integration-Seam Audit): **executed 2026-08-09, stopped at
  Checkpoint A, awaiting human review**. Report: `docs/STAGE0_BASELINE_AUDIT.md`.
- Authorized next stage: **none until Checkpoint A is accepted.** Stage 1 remains
  BLOCKED.
- Live trading: **not authorized**.

## Start here
1. `/AGENTS.md`
2. `/CLAUDE.md` (upstream invariants)
3. `/docs/DECISIONS.md`
4. `/docs/MILESTONES.md`
5. `/docs/ACCEPTANCE_CRITERIA.md`
6. `/docs/STAGE0_BASELINE_AUDIT.md` (verified source map, seams, discrepancies)
7. Architecture document(s) relevant to the task.

## Current architectural headline
Keep quant-agent intact. Add minimal provider/telemetry/API seams. Build a
QAMC-native React dashboard using OpenTradex and Orallexa as selective
component/design donors, with TradingView Lightweight Charts. Pilot AgentLens
later as an optional sidecar rather than customizing it upfront.

## Stage 0 outcome
- Baseline suite: **1431 passed, 0 failed, 0 skipped** (hermetic; no network,
  no API keys). Container setup requires a venv — system pip cannot build `ta`.
- Decision chain, Alpaca lifecycle, persistence, reflection/Meta and scheduler
  are mapped in the audit report.
- Integration seams identified and **not implemented**, cheapest first:
  actual-model attribution (~9 lines), additive `agent_logs` columns via the
  existing `_ensure_column` migration, provider strategy *below* `_execute()`,
  `decision_id` on `trades` for decision-level correlation.
- 10 discrepancies recorded (D-1 … D-10).

## Blocking items before Stage 1
- **D-1** — every agent-log row records the *configured* model, never the actual
  one, while cost is priced from the actual one. Contradicts DECISION #12.
- **D-2 / D-3** — Orallexa and AgentLens are named as donors but identified by no
  repository, license or commit in any governed document.

## Non-goals now
No OpenRouter implementation, no dashboard code, no AgentLens fork, no journal
code, no risk changes and no repository restructuring. Stage 0 recommendations
are advisory and must not be implemented without explicit authorization.
