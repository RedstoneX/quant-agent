# QAMC Project Compass

## Current state
- Repository: `RedstoneX/quant-agent`, controlled fork of `yebof/quant-agent`.
- Bootstrap baseline: upstream commit `6fc3cf14f4e6f9fde5f6c10fbe4a8d51e3d0f4e7` (verify Stage 0).
- Architecture: frozen baseline documented in `docs/DECISIONS.md`.
- Feature implementation: **not started**.
- Authorized next stage: **Stage 0 — Baseline & Integration-Seam Audit only**.
- Live trading: **not authorized**.

## Start here
1. `/AGENTS.md`
2. `/CLAUDE.md` (upstream invariants)
3. `/docs/DECISIONS.md`
4. `/docs/MILESTONES.md`
5. `/docs/ACCEPTANCE_CRITERIA.md`
6. Architecture document(s) relevant to the task.

## Current architectural headline
Keep quant-agent intact. Add minimal provider/telemetry/API seams. Build a QAMC-native React dashboard using OpenTradex and Orallexa as selective component/design donors, with TradingView Lightweight Charts. Build a native journal over canonical data. Pilot AgentLens later as an optional sidecar rather than customizing it upfront.

## Stage 0 output expected
A verified source-code map, baseline tests, donor component inventory, integration-seam proposal and a discrepancy report. Then STOP for human approval.

## Non-goals now
No OpenRouter implementation, no dashboard code, no AgentLens fork, no journal code, no risk changes and no repository restructuring during Stage 0.
