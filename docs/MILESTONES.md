# QAMC Milestones

## Status legend
- `NEXT` — currently authorized bounded stage.
- `BLOCKED` — prerequisite checkpoint not accepted.
- `OPTIONAL` — may be deferred without failing the core project.
- `DONE` — accepted.

## Stage 0 — Baseline & Integration-Seam Audit — **DONE (accepted 2026-08-09)**
**No feature implementation.** Checkpoint A **ACCEPTED by the operator.**

Executed 2026-08-09 in two passes. Full report: **`docs/STAGE0_BASELINE_AUDIT.md`**.
Headline results: baseline `6fc3cf14…` verified (fork is level with upstream
`yebof/quant-agent` main, +1 docs-only commit); baseline suite **1431 passed,
0 failed, 0 skipped**; 10 discrepancies recorded; all five Checkpoint A
criteria satisfied.

Donor inventory complete — all pinned and inspected:
`deonmenezes/opentradex` @ `30b23f5e`, `alex-jb/orallexa-ai-trading-agent`
@ `794a2ec0`, `tranhoangtu-it/agentlens` @ `21ab445a`. D-2 and D-3 **resolved**.

**Operator decisions at sign-off (2026-08-09):**
1. **AgentLens DROP — ACCEPTED.** Stage 6 is removed from the roadmap (see
   "Retired scope" at the end of this file). Trace affordances re-scoped onto
   native `agent_logs` / `run_id` / replay.
2. **Checkpoint A / Stage 0 — ACCEPTED as complete.**
3. **Stage 0.5 — AUTHORIZED** as the next bounded implementation stage.

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
- assess upstream AgentLens as a pilot only; — **done; outcome was DROP, accepted**
- report discrepancies between actual source and frozen docs.

Acceptance checkpoint A:
- existing behavior unchanged; — **met** (documentation-only changes, both passes)
- existing tests pass or every pre-existing failure is documented; — **met** (1431/1431)
- integration-seam report completed; — **met**
- donor inventory completed; — **met** (all donors pinned and inspected)
- no feature code added. — **met**

**All Checkpoint A criteria satisfied and ACCEPTED by the operator 2026-08-09.
Stage 0 is DONE.**

## Stage 0.5 — D-1 Actual-Model Attribution Hotfix — **IMPLEMENTED, AWAITING CHECKPOINT A5 ACCEPTANCE**
Bounded correctness fix, deliberately kept **outside** Stage 1.

Operator direction: historical experimental attribution cannot reliably be
repaired after the fact, so correct attribution must exist before new
experimental trading data is generated.

**In scope:** persist the model that actually answered, at the nine
`insert_agent_log(...)` call sites listed in `docs/STAGE0_BASELINE_AUDIT.md`
§9A (`pipeline_stages.py:280, 328, 483, 699`; `pipeline.py:4704, 6109, 6296,
6654/6668, 7050`). Behaviour-neutral for trading. Add targeted tests proving
that a cross-provider failover records the failover model, not the configured
one.

**Explicitly out of scope** (these stay in Stage 1): provider abstraction or
any change to `base.py:_execute()`; new `agent_logs` columns; provider,
latency, prompt-version, `finish_reason` or `truncated` capture; correlation-ID
work; OpenRouter.

**Known limits this hotfix does not remove**, to be recorded rather than
solved: `tech_analyst` collapses N chunk calls into one row keeping only the
last chunk's model (audit §3.5 F-3), and the relay attribution ceiling (F-4).

Checkpoint A5: trading behavior unchanged; full suite green plus the new
targeted tests; `agent_logs.model` demonstrably records the actual model; no
schema change; no `_execute()` change. **STOP.**

**Implemented 2026-08-09 on branch `claude/stage-0-5-attribution-hotfix-nbjkep`.**
All nine call sites changed from `model=config.llm.<agent>_model` to
`model=<result>.model` (the field `AgentResult.model` already carried,
previously discarded). One pre-existing test
(`tests/test_cash_sweep.py::test_position_review_hides_vehicle_and_parks_at_end`)
mocked its `AgentResult` with a bare `MagicMock()` that had no `.model` set —
harmless before the fix (the code read `config.llm.position_reviewer_model`
instead) but would fail to bind to SQLite once the real field is read; fixed
by adding `model="test-model"` to that mock, no behavior assertions changed.
Five new targeted regression tests added in
`tests/test_agent_log_attribution.py`, one per session type, together
covering all nine call sites; each was confirmed to fail against the
pre-fix code (configured model persisted) and pass against the fix (actual
`AgentResult.model` persisted). Full suite: **1436 passed, 0 failed** (1431
baseline + 5 new). No database schema change. `src/agents/base.py::_execute()`
untouched. Awaiting operator Checkpoint A5 acceptance before Stage 1 unblocks.

## Stage 1 — Provider, Model & Correlation Plumbing — BLOCKED
**Blocked until Checkpoint A5 (Stage 0.5) is accepted.**
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

*(Stage 6 — AgentLens Pilot — **removed from the roadmap 2026-08-09**. See
"Retired scope" at the end of this file. Stage numbering is left unchanged so
existing references to Stages 7–9 stay valid.)*

## Stage 7 — Learning Center — OPTIONAL / BLOCKED
Expose existing Meta Reflector: reports, evidence, prompt diffs, approve/reject, history/rollback, before/after performance where statistically defensible. Auto-Evolve remains off.

Checkpoint G: approved/rejected/rollback paths auditable and cannot alter hard deterministic safety.

## Stage 8 — Writable Operations — OPTIONAL / BLOCKED
Only after read-only system is mature: safe model changes, scheduler controls, pause/resume/kill semantics based on actual backend capabilities, and any approved operator-configurable risk settings.

Checkpoint H: all writes server-validated/audited; protected safety ceilings inaccessible; UI cannot issue broker orders directly. STOP.

## Stage 9 — Paper Soak & Experiment Analytics — BLOCKED
Long-running paper stability and measurements: agent/model contribution, cost per decision/trade, prompt-version performance, risk intervention, PM-vs-gate deltas and 30/60/90-day views.

No live-trading milestone exists in this plan.

---

## Retired scope

### Stage 6 — AgentLens Pilot — **REMOVED 2026-08-09**

Removed from the roadmap by operator decision at Stage 0 sign-off. Not
deferred, not blocked — **out of plan**. Nothing in Stages 1–9 depends on it.

Evaluated at `tranhoangtu-it/agentlens` @ `21ab445a91bf2bc2f8b7eb0a2a8fb70468a9047f`
(MIT). Full evidence: `docs/STAGE0_BASELINE_AUDIT.md` §8B/§8C and
`docs/architecture/AGENTLENS.md`.

Reason, in short: it is a well-built, genuinely non-blocking, operationally
light tool that solves a problem QAMC does not have. It exists to explain deep
nested agent traces; quant-agent runs nine flat single-shot prompt→JSON calls
per session, each already persisted with its complete prompt and response.
`agent_logs` + `run_id` + `scripts/replay_decision.py` cover the forensic need —
and replay is stronger than trace-compare because it re-executes rather than
diffing two recordings. Its search is one SQL `LIKE` on `agent_name`, weaker
than the FTS index Stage 5 builds anyway. Everything left over would be
QAMC-side work: manual spans inside `base.py:_execute()` (the loop the seam
analysis says to leave alone) and a redaction layer the SDK does not have.
Upstream is 69 commits by one author with no activity for ~4.5 months.

**Where its scope went.** The observability outcomes Stage 6 was meant to buy
are now served natively:

| Retired Stage 6 item | Native replacement |
|---|---|
| Trace capture of agent activity | `agent_logs.input_message` + `full_response` (already written per call) |
| Decision/trade ↔ trace linking | `run_id`, already shared by `agent_logs` and `trades` |
| Trace search | Stage 5 indexed search over canonical records (SQLite FTS5) |
| Replay / compare | `src/replay.py` + `scripts/replay_decision.py` |
| Per-call model/token/cost | `agent_logs` columns; corrected by Stage 0.5, extended in Stage 1 |
| Outage-resilience testing | moot — nothing external to fail |

**Reconsideration condition (deliberately preserved).** Revisit only if QAMC's
architecture evolves toward **deeper or tool-calling agent traces** — for
example multi-step tool-using agents, or a genuine need to visualize a span
timeline over the morning `ThreadPoolExecutor` fan-out
(macro/news/tech/earnings). In that case the pinned commit above is the
starting point, its non-blocking transport design holds up, and re-adding the
stage costs nothing that was destroyed here. Absent that architectural change,
this is closed.
