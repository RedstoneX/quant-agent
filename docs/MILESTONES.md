# QAMC Milestones

## Status legend
- `NEXT` — currently authorized bounded stage.
- `BLOCKED` — prerequisite checkpoint not accepted.
- `OPTIONAL` — may be deferred without failing the core project.
- `DONE` — accepted.

## Stage 0 — Baseline & Integration-Seam Audit — COMPLETE, AWAITING CHECKPOINT A SIGN-OFF
**No feature implementation.**

Executed 2026-08-09 in two passes. Full report: **`docs/STAGE0_BASELINE_AUDIT.md`**.
Headline results: baseline `6fc3cf14…` verified (fork is level with upstream
`yebof/quant-agent` main, +1 docs-only commit); baseline suite **1431 passed,
0 failed, 0 skipped**; 10 discrepancies recorded; **all five Checkpoint A
criteria now satisfied**.

Donor inventory complete — all pinned and inspected:
`deonmenezes/opentradex` @ `30b23f5e`, `alex-jb/orallexa-ai-trading-agent`
@ `794a2ec0`, `tranhoangtu-it/agentlens` @ `21ab445a`.
D-2 and D-3 are **resolved**. Stage 0 recommends **dropping AgentLens**
(§8C) — advisory, awaiting operator acceptance.

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
- existing behavior unchanged; — **met** (documentation-only changes, both passes)
- existing tests pass or every pre-existing failure is documented; — **met** (1431/1431)
- integration-seam report completed; — **met**
- donor inventory completed; — **met** (all donors pinned and inspected)
- no feature code added. — **met**

**All Checkpoint A criteria are satisfied. STOPPED. Awaiting human sign-off.**

Two operator decisions are carried forward. Neither blocks Checkpoint A:
1. Accept or reject the **AgentLens DROP** recommendation (audit §8C). If
   accepted: strike Stage 6, and re-scope `TraceLink` / "Inspect AI Trace"
   onto `agent_logs`.
2. Authorize **Stage 0.5** below. Sequencing is already decided; the change is not.

## Stage 0.5 — D-1 Actual-Model Attribution Hotfix — NOT YET AUTHORIZED
Bounded correctness fix, deliberately kept **outside** Stage 1.

Operator direction (2026-08-09): historical experimental attribution cannot
reliably be repaired after the fact, so correct attribution must exist before
new experimental trading data is generated.

Scope when authorized: persist the model that actually answered, at the nine
`insert_agent_log(...)` call sites listed in `docs/STAGE0_BASELINE_AUDIT.md`
§9A. Behaviour-neutral for trading. Does **not** include provider abstraction,
new schema columns, latency or prompt-version capture — those stay in Stage 1.

**Not authorized and not implemented as of this checkpoint.**

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

## Stage 6 — AgentLens Pilot — OPTIONAL / BLOCKED — **DROP RECOMMENDED**

> **Stage 0 outcome (2026-08-09).** `tranhoangtu-it/agentlens` inspected at
> `21ab445a`. Recommendation: **DROP FROM THE PLAN** — architectural mismatch
> (deep-trace tooling vs. nine flat single-shot calls), near-total overlap with
> `agent_logs` + `run_id` + `scripts/replay_decision.py`, weaker search than
> QAMC will build in Stage 5, and a dormant single-author upstream. It is
> genuinely non-blocking and operationally light; the objection is fit and cost.
> Advisory only — awaiting operator acceptance. See
> `docs/architecture/AGENTLENS.md` and audit §8B/§8C. The stage text below
> stands unless and until the drop is accepted.

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
