# QAMC Project Compass

## Current state
- Repository: `RedstoneX/quant-agent`, controlled fork of `yebof/quant-agent`.
- Bootstrap baseline: upstream commit `6fc3cf14f4e6f9fde5f6c10fbe4a8d51e3d0f4e7`
  — **verified in Stage 0**. The fork is level with upstream `main` plus one
  docs-only commit; no source divergence.
- Architecture: frozen baseline documented in `docs/DECISIONS.md`.
- Feature implementation: **not started**.
- Stage 0 (Baseline & Integration-Seam Audit): **COMPLETE 2026-08-09 (two
  passes). All Checkpoint A criteria satisfied; stopped, awaiting human
  sign-off.** Report: `docs/STAGE0_BASELINE_AUDIT.md`.
- Authorized next stage: **none until Checkpoint A is signed off.** Stage 0.5
  (D-1 hotfix) is scheduled but **not authorized**. Stage 1 remains BLOCKED.
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

## Donor status (all pinned and inspected)
| Donor | Repository | Commit | Verdict |
|---|---|---|---|
| OpenTradex | `deonmenezes/opentradex` | `30b23f5e` | keep — layout/visual language only |
| Orallexa | `alex-jb/orallexa-ai-trading-agent` | `794a2ec0` | keep — concepts verified; **adapt, don't vendor** |
| TradingView Lightweight Charts | `tradingview/lightweight-charts` | library dep | keep |
| AgentLens | `tranhoangtu-it/agentlens` | `21ab445a` | **DROP recommended** (advisory) |

## Open operator decisions
1. **Accept or reject the AgentLens DROP** (audit §8C). If accepted: strike
   Stage 6, re-scope `TraceLink` / "Inspect AI Trace" onto `agent_logs`.
2. **Authorize Stage 0.5** — the D-1 actual-model attribution hotfix. Sequencing
   is decided (bounded, pre-Stage-1, separate from provider work); the change
   itself is not authorized and has not been made.
3. Sign off Checkpoint A.

## Still-open discrepancies (none blocking Checkpoint A)
- **D-1** (scheduled as Stage 0.5), **D-4** … **D-10** — see
  `docs/DECISIONS.md` conflict register. **D-2 and D-3 are resolved.**

## Non-goals now
No OpenRouter implementation, no dashboard code, no AgentLens fork, no journal
code, no risk changes and no repository restructuring. Stage 0 recommendations
are advisory and must not be implemented without explicit authorization.
