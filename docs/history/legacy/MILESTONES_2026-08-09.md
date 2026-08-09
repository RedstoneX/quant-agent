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

## Stage 0.5 — D-1 Actual-Model Attribution Hotfix — **DONE (Checkpoint A5 accepted 2026-08-09)**
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

**Implemented 2026-08-09 on branch `claude/stage-0-5-attribution-hotfix-nbjkep` and merged by PR #6.**
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
untouched.

**Checkpoint A5 ACCEPTED by the operator 2026-08-09. Stage 0.5 is DONE. Stage 1 is authorized as NEXT.**

## Stage 1 — Provider, Model & Correlation Plumbing — **DONE (Checkpoint B accepted 2026-08-09)**
- explicit provider/model configuration compatible with existing per-agent settings;
- OpenRouter and/or Google AI Studio path with minimal provider abstraction;
- preserve resilience without contaminating experiment attribution;
- persist actual provider/model/tokens/cost/latency/status;
- stable run/decision/agent/order/trade/prompt-version correlation IDs where minimally necessary.

Checkpoint B: paper run behavior/risk unchanged; attribution correct; tests pass. STOP.

**Implemented 2026-08-09 on branch `claude/stage-1-qamc-integration-m1n0pw`.**
Orchestrated with four read-only Wave-0 investigation subagents (provider/config
seam, telemetry/persistence seam, correlation seam, test/safety review), then
implemented directly by the orchestrator rather than parallel implementation
workers — `base.py`, `config.py`, `pipeline.py`/`pipeline_stages.py` and
`db.py` are exactly the shared files the orchestration brief said not to let
two subagents edit concurrently, and the provider/telemetry/correlation seams
all intersect in those same files (e.g. `AgentResult` carries both the
provider-resolution outcome and the telemetry fields), so sequential
single-owner edits were the safer path. A dedicated Test subagent was not
needed either: the targeted tests were written directly against the now-fixed
interfaces immediately after each seam landed.

**A. Provider/model configuration.** `src/agents/base.py` gains
`resolve_provider(model, explicit_provider)` — a single source of truth
(explicit override wins; `None`, the default on every existing agent, falls
through to the unchanged prefix-inference chain `_provider_for()`) — reused by
`BaseAgent.__init__` (client construction), `AppConfig._check_llm_provider_keys`
(key validation), and `pipeline.py`'s `_key_for` (agent API-key lookup), closing
the "three independent bucketing implementations could disagree" risk the
Wave-0 provider subagent flagged. `LLMConfig` gains nine optional
`<agent>_provider` fields (mirrors the existing `<agent>_max_tokens` pattern),
validated against `{anthropic, openai, deepseek, openrouter}` at config load.
**OpenRouter** is the one new provider added (Google AI Studio was evaluated
and deliberately deferred — see below): it is OpenAI-wire-compatible, so it
reuses `_call_openai()` unmodified via a new client-construction branch
(`base_url=https://openrouter.ai/api/v1`, mirrors the existing DeepSeek
branch) and a dedicated concurrency semaphore
(`QUANT_AGENT_MAX_CONCURRENT_OPENROUTER`, default 3 — a distinct
account/rate-limit domain from the OpenAI relay). OpenRouter is required to be
**explicit-only**, never prefix-inferred: its "vendor/model" ids (e.g.
`anthropic/claude-3.5-sonnet`) would otherwise collide with native provider
prefixes. The cross-provider failover gate
(`(self._use_openai or self._use_deepseek or self._use_openrouter) and
self._fallback_api_key`) now also covers OpenRouter primaries.
`BaseAgent._execute()`'s retry/backoff/deadline/failover **loop body is
unchanged** — the only new code inside it is one `elif self._use_openrouter:`
dispatch line reusing the existing `_call_openai` method, per the Stage 0 audit's
explicit recommendation not to touch that loop. `ApiKeysConfig.openrouter`
added; "at least one provider key" check extended. **Google AI Studio was
evaluated at the synthesis gate and deferred**: it would require a genuinely
new call path (its own SDK, `contents`/`parts` message shape, different usage
field names, its own empty-content/finish-reason mapping) rather than an
extension of the existing seam — implementing "one generic mechanism" per the
prompt's instruction meant picking OpenRouter, which required none of that.
The `resolve_provider` design accommodates Google later without further
`base.py` restructuring.

**B. Experimental attribution / telemetry.** `AgentResult` gains six fields,
all captured at the entry/exit of `_execute()` only — never inside the retry
loop body: `requested_model`, `requested_provider` (captured before the loop,
from `self.model`/`self._provider`), `actual_provider` (derived from
`actual_model` after the loop — `_provider_for(_FALLBACK_MODEL)` correctly
resolves to `"anthropic"` with no special-casing needed), `used_fallback`
(`primary_error is not None`), `prompt_version`
(`sha256(system_prompt)[:12]`), `latency_s` (wall time across the whole call
including retries). A new `agent_log_kwargs(result)` helper in `base.py` maps
these onto the nine `insert_agent_log(...)` call sites uniformly
(`status="fallback"` iff `used_fallback`, else `"success"`; the one exception
path that already synthesizes an `AgentResult` on a hard failure —
`evening_analyst`'s except-block in `pipeline.py` — overrides `status="failed"`
and now also records what model/provider WAS requested even though no call
ever completed). `tech_analyst`'s documented N-chunks-collapse-to-1-row
limitation (Stage 0 audit F-3, explicitly out of Stage 1's required scope) got
a proportionate improvement since the values were already in hand per chunk:
the merged row now carries `used_fallback`/`truncated` ORed across chunks and
`latency_s` summed across chunks, rather than silently defaulting to
"no fallback happened" — the collapse-to-one-row structural limitation itself
remains, as documented.

**C. Persistence.** Nine new nullable `agent_logs` columns
(`requested_provider`, `requested_model`, `actual_provider`, `prompt_version`,
`latency_s`, `status`, `finish_reason`, `truncated`, `decision_id`) and one on
`trades` (`decision_id`), all added the sanctioned way — `_ensure_column` in
`src/storage/db.py:_migrate()`, the same idempotent additive mechanism used
for the prior 18 columns. `finish_reason`/`truncated` were already computed on
`AgentResult` but never persisted (Stage 0 audit F-2) — persisting them cost
nothing extra since the values already existed. No new table, no migration
framework, no external telemetry store. A dedicated test
(`test_migration_adds_new_columns_on_legacy_db`) builds a literal
pre-Stage-1-shaped SQLite file by hand and proves `initialize()` migrates it
safely with pre-existing rows reading back `NULL` in the new columns.

**D. Correlation.** One new identifier, `decision_id`
(`f"{run_id}-dec-{uuid4().hex[:6]}"`, generated independently of `run_id`
rather than reusing it verbatim, so it stays correct even if a future change
ever calls `PortfolioManager.decide()` more than once per run), generated once
in `DecisionStage.run()` right after a successful PM call, stored on
`RunContext.decision_id`, and threaded to: the `portfolio_manager` and
`risk_manager` `agent_logs` rows (`DecisionStage`/`RiskStage`), and every
`trades` row a run produces — HOLD, SELL/PARTIAL_SELL, and BUY
(`ExecutionStage`). `run_id` already correlated `agent_logs`↔`trades` at the
run level (Stage 0 audit — nothing needed there); the genuine gap was
decision-level, and one column closes it. `src/replay.py` /
`scripts/replay_decision.py` needed no change — replay operates purely on
`agent_logs` rows keyed by `agent_name`/`run_id`, never touches `trades`.
A dedicated end-to-end test
(`test_morning_session_decision_id_correlates_pm_rm_and_trade`) runs a full
`TradingPipeline.run_morning()` against a real (`tmp_path`) SQLite DB and
proves the PM row, the RM row, and the resulting BUY's trade row all share one
`decision_id`, while research-phase agents (macro/tech/news/earnings, outside
the PM/RM decision chain) correctly carry `NULL`.

**E. Safety verification.** `RiskRuleEngine.__init__` still takes only
`RiskConfig` (pinned by a new signature-inspection test,
`test_invariant_risk_rule_engine_never_reads_llm_or_provider_config`) — no
code path exists from the new provider/LLM config into deterministic risk
math. A new invariant test
(`test_invariant_hard_risk_gate_unaffected_by_garbage_llm_config`) reruns the
existing hard-risk-breach scenario with `pipeline.config.llm` set to a bare
string (not even a `MagicMock`) and confirms the gate still blocks correctly —
if any code path dereferenced `config.llm`/`config.provider`, this would raise
`AttributeError` instead of gating. Two new tests
(`test_broker_paper_flag_unaffected_by_new_provider_config`,
`test_broker_paper_flag_false_still_passes_through_unmodified`) prove
`AlpacaBroker(paper=...)` reflects only `config.alpaca.paper`, unaffected by
any agent's provider being set to `openrouter`/`deepseek`, and that `False`
(live) passes through unmodified too — the "new config knob near
`AlpacaConfig` becomes a live-trading foot-gun" risk the Wave-0 test/safety
subagent flagged.

**Tests.** 28 new targeted tests across `tests/test_base_agent.py` (provider
routing/failover/attribution), `tests/test_config.py` (explicit-provider
key validation, backward-compat, invalid-provider rejection),
`tests/test_db.py` (schema roundtrip, legacy-DB migration, idempotent
re-migration), `tests/test_agent_log_attribution.py` (end-to-end
decision_id correlation), `tests/test_invariants.py` (hard-risk immunity to
provider config), and `tests/test_pipeline.py` (paper/live isolation). One
pre-existing test (`tests/test_cash_sweep.py::test_position_review_hides_vehicle_and_parks_at_end`)
needed the same fix Stage 0.5 applied once already: its mocked `AgentResult`-like
`MagicMock` had no explicit values for the new fields `agent_log_kwargs()`
reads, so SQLite rejected the auto-vivified `MagicMock` objects at bind time —
fixed by adding explicit field values to the mock, no behavior assertions
changed. **Full suite: 1464 passed, 0 failed** (1436 baseline + 28 new tests).
`src/agents/base.py::_execute()`'s retry/backoff/deadline loop body,
`RiskRuleEngine`, `_filter_hard_risk_decisions`, `PortfolioConstructor`, and
Alpaca paper/live selection are all unchanged in trading/risk semantics.

**Known limitations preserved, not solved (out of Stage 1's bounded scope):**
tech_analyst's chunk-collapse structural limitation (Stage 0 audit F-3) still
means true per-HTTP-call attribution is unavailable for that agent, just less
lossy than before; the relay attribution ceiling (F-4) is unchanged — QAMC
still cannot independently verify what model an `OPENAI_BASE_URL` relay
actually served; Google AI Studio support was evaluated and deferred, not
built. **D-8** (`.env.example` omits several documented env vars) is
partially addressed — the two Stage-1-introduced vars (`OPENROUTER_API_KEY`,
`QUANT_AGENT_MAX_CONCURRENT_OPENROUTER`) plus `DEEPSEEK_API_KEY` are now
documented there; the pre-existing gap for `OPENAI_BASE_URL`,
`OPENAI_CA_BUNDLE`, `TELEGRAM_*`, and `QUANT_AGENT_RETRY_DEADLINE_S` remains
open, unchanged by Stage 1.

**Checkpoint B: implementation-side verification complete, all 15 criteria
self-verified green (paper-safety, backward compat, new provider path,
per-agent selection, requested-vs-actual distinguishability, explicit
fallback attribution, telemetry persistence, prompt versioning, correlation
sufficiency, safe old-DB migration, deterministic-risk non-regression,
targeted + full suite green, Stage 2 not started).**

**Checkpoint B ACCEPTED by the operator 2026-08-09** (see
`docs/CHECKPOINT_B_ACCEPTANCE.md`). **Stage 1 is DONE. Stage 2 is authorized
as NEXT.**

## Stage 2 — Thin Read-Only Mission Control API — **IMPLEMENTED 2026-08-09, awaiting Checkpoint C operator acceptance**
Expose only what is needed for UI: account, positions, orders, trades, candidates, agents, PM/risk/deterministic decisions, model/cost, journal source data, learning reports, scheduler/health.

Checkpoint C: complete trading-day reconstruction possible; killing API does not affect trading; schema frozen. STOP.

**Implemented 2026-08-09 on branch `claude/stage-2-mission-control-api-4zpx7j`.**
Orchestrated with four read-only Wave-0 investigation subagents (canonical
data inventory, API framework/seam, trading-day reconstruction, safety/test
matrix), then two parallel implementation workers on genuinely disjoint
files (broker-live reads vs. SQLite-history reads — the greenfield `src/api/`
package let file ownership split cleanly, unlike Stage 1 where the seams
intersected in shared files), a parallel static-safety-test worker, and a
final independent review subagent (no authorship bias) that challenged
read-only isolation, secret exposure, trading-process independence, and
contract completeness before sign-off. Full account and endpoint contract:
`docs/architecture/MISSION_CONTROL_API.md`.

**A. Framework.** FastAPI + uvicorn, added as an *optional* `pyproject.toml`
`api` extra (`pip install -e '.[api]'`) — a trading-only install pulls in
neither. Chosen over stdlib `http.server` (no schema/validation) and Flask
(no native async/schema) because `pydantic>=2.7.0` is already a direct
dependency and reused directly for response models, and Stage 3's future
React/Vite frontend gets FastAPI's generated OpenAPI schema as a stable,
typed contract at zero extra authoring cost. The trading process is a
synchronous `BlockingScheduler` with no asyncio anywhere, so running an
async framework in a *separate* process creates no runtime interaction risk.

**B. Isolation architecture.** Two structurally separate read paths, per
the brief's explicit instruction to keep broker-live reads apart from
canonical historical SQLite reads: `src/api/broker_reads.py` (the only file
that constructs `AlpacaBroker`, reusing its two pre-existing read-only
methods `get_account`/`get_positions` plus one new read-only
`client.get_orders(...)` wrapper for order listing — implemented inside
`src/api/` rather than added to `src/execution/broker.py`, so zero
trading-critical files were touched) and `src/api/db_reads.py` (never
imports `src.storage.db.Database`; opens its own independent
`sqlite3.connect(f"file:{path}?mode=ro", uri=True)` connection per call — a
structural, OS-enforced guarantee that a write attempt fails at the SQLite
layer, not just by convention; safe under concurrent access because the
trading process already runs WAL mode for exactly this reason).
`src/api/server.py` adds two belt-and-suspenders, app-level guarantees on
top of both routers only ever registering `@router.get(...)`: a
`GetOnlyMiddleware` rejecting any non-GET/HEAD/OPTIONS request with 405
before any handler runs, and a global exception handler turning any
unhandled exception into a plain `{"detail": "internal error"}` 500 rather
than a leaking traceback. `src/api/deps.py` never hands a full
`AppConfig`/`ApiKeysConfig` to a route handler — only narrow accessor
functions — and every response is a typed Pydantic model
(`src/api/schemas.py`), never a raw `dict(row)` passthrough, so the
response *shape* structurally has nowhere to carry a secret.

**C. Endpoint contract.** `/health`, `/account`, `/positions`,
`/orders`, `/trades`, `/runs`, `/runs/{run_id}`, `/decisions/{decision_id}`,
`/agents`, `/agents/{agent_name}`, `/reflections`, `/candidates` — every
route maps to a pre-existing table/broker method, plus the one
narrowly-scoped new read helper noted above. Full table:
`docs/architecture/MISSION_CONTROL_API.md`.

**D. Known limitation at original Stage 2 sign-off — since resolved, see
Checkpoint C completion slice below.** Verified during Wave-0 inspection:
when the deterministic hard-risk gate blocks *every* candidate in a run
before it reaches `risk_manager`, no row in any table recorded which rule
fired — only an in-memory dict that reached a log line and a Telegram
push. `RunDetailResponse.hard_risk_block_recorded` was hardcoded `False`
rather than guessed. A partial hard-risk block (some candidates blocked,
RM still reached for the remainder) was, and remains, fully
reconstructable.

**Checkpoint C completion slice (2026-08-09, branch
`claude/stage-2-checkpoint-c-fb1ip0`).** Independent ChatGPT review of the
implemented-but-unaccepted Stage 2 branch identified two Checkpoint C
gaps; both are closed here, bounded and additive, without reopening Stage
2's architecture. Full account: `docs/architecture/MISSION_CONTROL_API.md`
"Checkpoint C completion slice".

1. **Candidates/watchlist API contract gap.** The governed milestone text
   directly above ("... trades, candidates, ...") did require candidates —
   the original implementation's claim that it wasn't in the Stage 2 data
   contract was incorrect and is corrected here. Repository inspection
   found the existing candidate read
   (`TradingPipeline._build_watchlist_candidates`, symbols the evening
   analyst has repeatedly flagged `add`/`watch` for universe expansion) is
   a pure function of already-persisted `insights.missed_opportunities_json`
   rows — no broker, no execution state. The aggregation was extracted
   verbatim into a new zero-dependency module,
   `src/watchlist_candidates.py`, imported by both
   `TradingPipeline._build_watchlist_candidates` (now a thin wrapper,
   identical output) and the new `src/api/db_reads.py:get_watchlist_candidates()`.
   `GET /candidates?lookback_days=` serves it. `TradingPipeline` is still
   never imported by `src/api/` — a pure helper moved to a neutral
   location both sides import, not a coupling of the API to the trading
   engine, and not a second candidate-generation engine.
2. **Deterministic hard-risk rejection reconstruction gap.** New
   `TradingPipeline._persist_hard_risk_block` writes one additive
   `agent_logs` row (existing table, existing `insert_agent_log`
   mechanism, no schema change) at both `RiskStage.run()` early-return
   sites, using the sentinel `agent_name="risk_gate"` — deliberately
   distinct from the real `"risk_manager"` LLM agent name so it's never
   mistaken for, or replayed as, an actual RM call. `cost_usd`/`tokens_used`
   are known-zero (not `None`), preserving `sum_session_cost`'s
   any-null-means-unknown convention for the rest of the run.
   `RunDetailResponse.hard_risk_block_recorded` is now computed from the
   fetched `agent_logs` (no longer hardcoded `False`); `DecisionDetailResponse`
   gained a `hard_risk_block` field. No hard-risk calculation, limit,
   eligibility, execution semantics, or broker behavior changed; fully
   backward compatible with old SQLite DBs (zero `risk_gate` rows on a
   pre-Checkpoint-C DB — both endpoints behave exactly as before).

9 new targeted tests (4 in `tests/test_pipeline_stages.py`, 5 in
`tests/test_api_contract.py`). **Full suite: 1530 passed, 0 failed** (1521
baseline + 9 new). No trading-critical file modified; deterministic
risk/execution semantics unchanged.

**E. Independent review findings.** A fresh subagent given the finished
implementation (no authorship bias) checked four angles — read-only
isolation, secret exposure, trading-process independence, contract
completeness — and returned CONFIRMED SAFE on all four, with one real
test-coverage gap: `tests/test_api_safety.py`'s write-capable-broker-attribute
denylist was missing `shift_stops_down`/`replace_stop_loss` (both
write-capable, neither referenced anywhere in `src/api/` — confirmed before
and after the fix, so this closed a defense-in-depth gap, not a live
vulnerability). Fixed directly; no other finding required a code change.

**Tests.** 57 new targeted tests across five files:
`tests/test_api_safety.py` (30 static/AST-level structural invariants —
GET-only routes, no write-capable broker attribute referenced anywhere,
`AlpacaBroker` constructed in exactly one place, no cross-import in either
direction between `src/api/` and the trading-critical stack, `db_reads.py`'s
SQL provably `SELECT`/`PRAGMA`-only), `tests/test_api_contract.py`
(functional endpoint tests against a real seeded SQLite DB plus stubbed
broker reads, covering every route, 404s, and the
any-null-cost-means-unknown-total convention), `tests/test_api_no_secrets.py`
(schema-level field-name blocklist + live sentinel-value sweep across every
GET route), `tests/test_api_db_concurrency.py` (concurrent trading writes
against real API reads on the same WAL-mode file — zero writer lock errors,
passing `PRAGMA integrity_check`; a second test confirms the `mode=ro`
connection is physically unable to write), `tests/test_api_isolation.py`
(starts the API as a real separate OS process via `python -m src.api`,
confirms it answers `/health`, SIGKILLs it, then confirms an ordinary
trading DB write succeeds identically before and after). **Full suite: 1521
passed, 0 failed** (1464 baseline + 57 new). No trading-critical file
(`main.py`, `src/pipeline.py`, `src/pipeline_stages.py`,
`src/execution/broker.py`'s write methods, `src/risk/*`,
`src/storage/db.py`'s write methods) was modified; deterministic
risk/execution semantics and Alpaca paper/live selection are unchanged.

**Checkpoint C: implementation-side verification complete, including the
completion slice** (API is read-only; runs independently of the trading
process; trading runs with the API absent/dead; API exposes no
order-execution capability; no secrets are returned;
account/positions/orders/trades are represented honestly;
agent/provider/model/token/cost/latency/status data is available; `run_id`
and `decision_id` support end-to-end decision reconstruction; PM → Risk →
deterministic gate → trade/rejection is reconstructable, **including a run
where the deterministic hard-risk gate blocked every candidate before
risk_manager ever ran** (Checkpoint C completion slice, `agent_name="risk_gate"`
forensic row); universe-expansion candidates are exposed via `/candidates`
(Checkpoint C completion slice, pure-function extraction, `TradingPipeline`
still never imported by `src/api/`); journal source records are available
without a second memory system; existing learning/reflection records are
exposed read-only; old/current SQLite DBs remain compatible — no schema
change; deterministic risk/execution behavior is unchanged; targeted + full
suite pass (1530/1530); Stage 3 not started).
**Awaiting operator acceptance before Stage 2 is marked DONE and Stage 3 is
authorized.**

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
