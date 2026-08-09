# QAMC Milestones

## Status legend
- `NEXT` — currently authorized bounded stage.
- `BLOCKED` — prerequisite checkpoint not accepted.
- `OPTIONAL` — may be deferred without failing the core project.
- `DONE` — accepted.

## Stage 0 — Baseline & Integration-Seam Audit — AWAITING CHECKPOINT A REVIEW
**No feature implementation.**

Executed 2026-08-09. Full report: **`docs/STAGE0_BASELINE_AUDIT.md`**.
Headline results: baseline `6fc3cf14…` verified (fork is level with upstream
`yebof/quant-agent` main, +1 docs-only commit); baseline suite **1431 passed,
0 failed, 0 skipped**; 10 discrepancies recorded (D-1 actual-model attribution
is the material one); donor inventory complete for OpenTradex only — Orallexa
and AgentLens are unidentified in governed documentation.

Tasks:
- record exact upstream/fork commit and upstream remote expectations;
- run full existing test suite and record result;
- map provider/model routing and failover behavior;
- map PM, AI Risk Manager, deterministic risk and execution paths;
- map Alpaca order/protection lifecycle;
- map canonical persistence, agent logs, trades, model/token/cost data, reflections and Meta Reflector;
- map scheduler/systemd deployment;
- identify least-invasive seams for correlation IDs, provider routing and read-only API;
- inspect approved donor code at current commits: OpenTradex, Orallexa, TradingView Lightweight Charts; document exact reusable components versus backend-coupled code;
- assess upstream AgentLens as a pilot only;
- report discrepancies between actual source and frozen docs.

Acceptance checkpoint A:
- existing behavior unchanged; — **met** (documentation-only changes)
- existing tests pass or every pre-existing failure is documented; — **met** (1431/1431)
- integration-seam report completed; — **met**
- donor inventory completed; — **partially met**; blocked for Orallexa and
  AgentLens, which no governed document identifies (see D-2 / D-3)
- no feature code added. — **met**

**STOPPED after Checkpoint A. Awaiting human review.**

Operator decisions needed to close Checkpoint A and unblock Stage 1:
1. Name the Orallexa and AgentLens repositories (or drop them).
2. Confirm whether the D-1 attribution fix runs as a pre-Stage-1 hotfix or
   inside Stage 1.

## Stage 1 — Provider, Model & Correlation Plumbing — BLOCKED
- explicit provider/model configuration compatible with existing per-agent settings;
- OpenRouter and/or Google AI Studio path with minimal provider abstraction;
- preserve resilience without contaminating experiment attribution;
- persist actual provider/model/tokens/cost/latency/status;
- stable run/decision/agent/order/trade/prompt-version correlation IDs where minimally necessary.

Checkpoint B: paper run behavior/risk unchanged; attribution correct; tests pass. STOP.

## Stage 2 — Thin Read-Only Mission Control API — BLOCKED
Expose only what is needed for UI: account, positions, orders, trades, candidates, agents, PM/risk/deterministic decisions, model/cost, journal source data, learning reports, scheduler/health.

Checkpoint C: complete trading-day reconstruction possible; killing API does not affect trading; schema frozen. STOP.

## Stage 3 — QAMC Native Cockpit — BLOCKED
- React/Vite/Tailwind native frontend;
- selective OpenTradex presentation components/patterns;
- TradingView Lightweight Charts;
- real account/P&L/positions/orders/trades/candidates/health;
- no production mock trading data;
- responsive iPad experience.

Checkpoint D: usable cockpit over real QAMC API; frontend failure has zero trading impact. STOP for UI review.

## Stage 4 — AI Decision Interface — BLOCKED
- agent cards, provider/model, recommendation/confidence, tokens/cost;
- disagreement visualization;
- PM proposal → AI Risk response → deterministic gate → executed/rejected chain;
- adapt Orallexa concepts/components only where cheaper than native implementation.

Checkpoint: one candidate can be followed end-to-end without reading raw logs.

## Stage 5 — Native Journal & Indexed Search — BLOCKED
Core first:
- calendar/list/daily views;
- required daily sections;
- indexed server-side structured/full-text search over canonical-derived read model.

Enhancements after core:
- visible NL→structured-filter translation;
- Suggested Investigations templates.

Checkpoint E: journal reconstructable from canonical data; index deletable/rebuildable; useful forensic queries work. STOP.

## Stage 6 — AgentLens Pilot — OPTIONAL / BLOCKED
- integrate upstream AgentLens with minimal/non-blocking instrumentation;
- redact sensitive fields QAMC-side before transmission;
- link decision/trade records to trace IDs;
- deliberately test AgentLens outage.

Do **not** add project/workspace schema or major AgentLens search rewrite at this stage.

Checkpoint F: AgentLens proves useful; trading unaffected when it is down; no representative secrets stored. Decide whether a fork is justified. STOP.

## Stage 7 — Learning Center — OPTIONAL / BLOCKED
Expose existing Meta Reflector: reports, evidence, prompt diffs, approve/reject, history/rollback, before/after performance where statistically defensible. Auto-Evolve remains off.

Checkpoint G: approved/rejected/rollback paths auditable and cannot alter hard deterministic safety.

## Stage 8 — Writable Operations — OPTIONAL / BLOCKED
Only after read-only system is mature: safe model changes, scheduler controls, pause/resume/kill semantics based on actual backend capabilities, and any approved operator-configurable risk settings.

Checkpoint H: all writes server-validated/audited; protected safety ceilings inaccessible; UI cannot issue broker orders directly. STOP.

## Stage 9 — Paper Soak & Experiment Analytics — BLOCKED
Long-running paper stability and measurements: agent/model contribution, cost per decision/trade, prompt-version performance, risk intervention, PM-vs-gate deltas and 30/60/90-day views.

No live-trading milestone exists in this plan.
