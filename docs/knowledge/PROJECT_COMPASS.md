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
- **Stage 1 — Provider, Model & Correlation Plumbing: DONE. Checkpoint B
  ACCEPTED 2026-08-09.** Branch
  `claude/stage-1-qamc-integration-m1n0pw`. Explicit per-agent provider
  override (`resolve_provider()` single source of truth in `src/agents/base.py`,
  nine new optional `LLMConfig.<agent>_provider` fields) added OpenRouter
  through the least-invasive seam (OpenAI-wire-compatible, reuses
  `_call_openai` unmodified); `BaseAgent._execute()`'s hardened
  retry/deadline/failover loop body untouched. `AgentResult` gained
  requested-vs-actual provider/model, `used_fallback`, `prompt_version`,
  `latency_s`; nine new nullable `agent_logs` columns + one `trades.decision_id`
  column via the existing `_ensure_column` mechanism. `decision_id` links a
  PM proposal's `agent_logs` row through RM's review to the resulting
  order/trade rows. 28 new targeted tests; full suite **1464 passed, 0
  failed**. Details: `docs/MILESTONES.md` Stage 1.
- **Stage 2 — Thin Read-Only Mission Control API: IMPLEMENTED 2026-08-09,
  Checkpoint C completion slice IMPLEMENTED 2026-08-09, awaiting Checkpoint
  C operator acceptance.** Branch `claude/stage-2-mission-control-api-4zpx7j`,
  completion slice on `claude/stage-2-checkpoint-c-fb1ip0`. New `src/api/`
  package (FastAPI + uvicorn, optional `pyproject.toml` extra) exposes
  existing canonical state read-only: `/health`, `/account`, `/positions`,
  `/orders` (broker-live), `/trades`, `/runs`, `/runs/{run_id}`,
  `/decisions/{decision_id}`, `/agents`, `/agents/{agent_name}`,
  `/reflections`, `/candidates` (SQLite, via a dedicated `mode=ro`
  connection — never shares `src.storage.db.Database`'s writer
  connection/lock). No trading-critical file was modified; no schema
  change. 57 Stage 2 targeted tests (structural safety, functional
  contract, no-secrets, DB concurrency, process-kill isolation) plus an
  independent review pass (fresh subagent, no authorship bias) confirming
  read-only isolation, secret exposure, trading-process independence, and
  contract completeness. Independent ChatGPT review of the
  implemented-but-unaccepted branch then found two Checkpoint C gaps, both
  closed in the completion slice: (1) `/candidates` now exists — the
  existing `TradingPipeline._build_watchlist_candidates` aggregation is a
  pure function of already-persisted `insights` rows, extracted verbatim
  into a new zero-dependency `src/watchlist_candidates.py` module imported
  by both the pipeline (thin wrapper, identical output) and
  `src/api/db_reads.py` — `TradingPipeline` is still never imported by
  `src/api/`; (2) a fully hard-risk-blocked run's rejection reason is now
  persisted as one additive `agent_logs` row (`agent_name="risk_gate"`
  sentinel, existing table/mechanism, no schema change, no change to hard-risk
  calculations), surfaced via `RunDetailResponse.hard_risk_block_recorded`
  (now computed, not hardcoded `False`) and a new
  `DecisionDetailResponse.hard_risk_block` field. 9 new targeted tests.
  **Full suite 1530 passed, 0 failed.** Details: `docs/MILESTONES.md`
  Stage 2, `docs/architecture/MISSION_CONTROL_API.md`.
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

## Stage 1 outcome (DONE, Checkpoint B accepted)
Provider, Model & Correlation Plumbing implemented on
`claude/stage-1-qamc-integration-m1n0pw` (see `docs/MILESTONES.md` Stage 1 for
the full account):

- explicit provider/model configuration compatible with existing per-agent settings — done (nine `LLMConfig.<agent>_provider` fields, default `None` = unchanged prefix inference);
- OpenRouter path through the least-invasive provider seam — done; Google AI Studio evaluated and deferred (would need a genuinely new call path, not an extension of the existing seam);
- preserve resilience without silent experimental attribution — done (`requested_provider`/`requested_model` always distinct from `actual_provider`/`model`; `used_fallback` explicit);
- persist actual provider/model/tokens/cost/latency/status as required by the frozen contract — done (nine new nullable `agent_logs` columns via `_ensure_column`);
- add only the correlation identifiers minimally necessary to trace run → decision → order/trade → prompt/model version — done (`decision_id`, one new column on `trades` + `agent_logs`);
- preserve the hardened retry/deadline/failover behavior in `BaseAgent._execute()` rather than casually refactoring it — done (loop body unchanged; new code is one dispatch branch reusing `_call_openai`).

**Checkpoint B ACCEPTED by the operator 2026-08-09** (`docs/CHECKPOINT_B_ACCEPTANCE.md`):
paper-trading/risk behavior unchanged, attribution correct, tests green.
Stage 2 was then authorized as NEXT and is now implemented — see below.

## Stage 2 outcome (implemented, awaiting Checkpoint C acceptance)
Thin Read-Only Mission Control API implemented on
`claude/stage-2-mission-control-api-4zpx7j` (see `docs/MILESTONES.md` Stage 2
and `docs/architecture/MISSION_CONTROL_API.md` for the full account):

- read-only HTTP API exposing existing canonical state, no new trading
  engine/memory store/operational dependency — done (`src/api/`, FastAPI +
  uvicorn, optional install extra);
- broker-live reads (account/positions/orders) kept structurally separate
  from canonical SQLite reads — done (`broker_reads.py` vs. `db_reads.py`,
  never sharing a connection or code path);
- API cannot place/cancel/modify broker orders — done (GET-only enforced
  at router + app-middleware level; every write-capable broker method
  verified absent via AST scan, including the two the independent review
  found initially missing from the denylist, `shift_stops_down`/
  `replace_stop_loss` — fixed);
- API death/absence does not affect trading — done, proven both
  structurally (no trading-critical file imports `src.api`) and
  behaviorally (a real separate OS process is started, confirmed live,
  killed, then an ordinary trading DB write is proven to succeed
  identically);
- no secrets in any response — done (narrow non-secret config accessors
  only, typed Pydantic response models with no secret-shaped field,
  live sentinel-value sweep across every route);
- SQLite reads safe under concurrent trading writes — done (dedicated
  `mode=ro` connection, verified under real concurrent load plus
  `PRAGMA integrity_check`);
- one genuine schema gap found at original sign-off, documented rather
  than silently patched with new persistence — a fully hard-risk-blocked
  run's rejection reason was not recorded anywhere in canonical storage.
  **Closed in the Checkpoint C completion slice below** via one additive
  `agent_logs` row, no schema change.

## Checkpoint C completion slice outcome (implemented 2026-08-09)
Independent ChatGPT review of the implemented-but-unaccepted Stage 2 branch
found two Checkpoint C gaps, both closed on
`claude/stage-2-checkpoint-c-fb1ip0` (see `docs/MILESTONES.md` Stage 2 and
`docs/architecture/MISSION_CONTROL_API.md` "Checkpoint C completion slice"
for the full account):

- candidates/watchlist API contract gap — closed. The governed milestone
  text did require candidates; `/candidates` now exists. The existing
  `TradingPipeline._build_watchlist_candidates` aggregation was a pure
  function of already-persisted `insights.missed_opportunities_json` rows —
  extracted verbatim into a new zero-dependency `src/watchlist_candidates.py`
  module, imported by both the pipeline (now a thin wrapper, identical
  output, unchanged existing tests) and `src/api/db_reads.py`.
  `TradingPipeline` is still never imported by `src/api/`; no second
  candidate-generation engine; no trading decisions recomputed;
- deterministic hard-risk rejection reconstruction gap — closed. New
  `TradingPipeline._persist_hard_risk_block` writes one additive
  `agent_logs` row (`agent_name="risk_gate"` sentinel, distinct from the
  real `"risk_manager"` LLM agent name) at both `RiskStage.run()`
  early-return sites, via the existing `insert_agent_log` mechanism — no
  schema change, no change to hard-risk calculations/limits/eligibility/
  execution/broker behavior. `cost_usd`/`tokens_used` persist as
  known-zero, not unknown, preserving `sum_session_cost`'s convention.
  `RunDetailResponse.hard_risk_block_recorded` is now computed (not
  hardcoded `False`); `DecisionDetailResponse` gained a `hard_risk_block`
  field. Backward compatible with old SQLite DBs (zero `risk_gate` rows on
  a pre-Checkpoint-C DB, unchanged behavior).

9 new targeted tests (4 `tests/test_pipeline_stages.py`, 5
`tests/test_api_contract.py`). **Full suite: 1530 passed, 0 failed** (1521
baseline + 9 new). No trading-critical file modified.

**Checkpoint C: implementation-side self-verification complete, including
the completion slice** (see `docs/MILESTONES.md` Stage 2 for the full
account). **Awaiting operator acceptance before Stage 2 is marked DONE and
Stage 3 is authorized. STOP at Checkpoint C; Stage 3 has not been started.**

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
No dashboard UI, journal implementation, risk-policy redesign, live trading,
repository restructuring, or AgentLens integration. Stage 2 is scoped to the
thin read-only Mission Control API only; it must not expand into Stage 3+
(frontend, journal, learning UI) merely because adjacent work is convenient.
