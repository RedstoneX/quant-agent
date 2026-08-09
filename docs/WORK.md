# QAMC Current Work

Status: **DISCOVERY / ARCHITECTURE CHALLENGE — COMPLETE, AWAITING RECONCILIATION**

## Goal

Re-evaluate the post-Stage-2 Mission Control direction against `docs/OUTCOME.md` and the actual repository before any implementation.

Use `/qamc-discover`. No product implementation is authorized in this work phase.

## Required discovery result

Replace the placeholders below with concise evidence. Keep the result at capability/constraint/acceptance level, not a file-by-file build recipe.

### Repository findings

- Stage 2 API (`src/api/`) is real, isolated, tested: GET-only, independent read-only SQLite connections, typed Pydantic responses, no frontend anywhere in the repo. Clean slate for Stage 3 — no coupling risk.
- `decision_id` is generated once per PM call (`src/pipeline_stages.py:485-486`) and threaded to `risk_manager` + `trades` only. Research-phase agents (macro/tech/news/earnings) correctly log `NULL` `decision_id` — this is deliberate and pinned by `test_morning_session_decision_id_correlates_pm_rm_and_trade`, not an oversight.
- Each research-phase agent persists **one `agent_logs` row per run**, covering *all* symbols in one batched LLM call (e.g. `tech_analyst` logs one row for N symbols; `output_summary` is a terse line like `"AAPL:BUY, MSFT:HOLD"`, `full_response` is the raw call text). The rich structured per-symbol result the agent actually produces (`TechAnalysisResult`: rating, conviction, `reasoning_chain`, risk/reward, entry/stop/target — `src/models.py:93`) exists in memory during the run but is **not persisted per symbol**. `TechStore` (`data/tech/last_ratings.json`) is a small mutable rating/age cache, not a queryable audit trail.
- Consequence: "what specialist agents concluded and where they disagreed" (OUTCOME.md) is already fully answerable **per run/day** from the existing API with zero backend changes (`/runs/{run_id}` already returns every agent's full logged output for that run). A true **per-candidate** drill-down needs new additive persistence — confirmed with the operator (see below).
- Stage 2 Checkpoint C already established the precedent this needs: an additive-only forensic `agent_logs` row (`agent_name="risk_gate"`) that never alters trading/risk semantics. Extending additive, non-authoritative persistence to per-symbol specialist output follows accepted precedent rather than opening a new risk category.
- The prior "Stages 3-5 as one coordinated tranche, no intermediate STOP" authorization (`docs/MISSION_CONTROL_BUILD_TRANCHE.md`, commit `fbe031c`) was already superseded/pruned by the repository-hygiene pass before this discovery started. `docs/architecture/JOURNAL_AND_SEARCH.md` (Stage 5 design) was pruned in the same pass; its recovered content is sound — canonical-projection-only journal, SQLite FTS5 search, structured dimensions keyed by `run_id` (not a fabricated trace id), no arbitrary LLM-generated SQL, `scripts/replay_decision.py` (still present) as the offline re-execution path.
- Stage 0 donor evaluation is complete and sound: OpenTradex (`deonmenezes/opentradex` @ `30b23f5e`, MIT) — layout/visual-language donor only. Orallexa (`alex-jb/orallexa-ai-trading-agent` @ `794a2ec0`, MIT) — `PerspectivePanelCard` maps closely onto the four specialist analysts; noted adaptation cost is a naming inversion (Orallexa's `PortfolioManagerCard` carries QAMC's AI Risk Manager semantics). Neither donor supplies a PM→Risk→Gate→Execution decision-chain view — that stays a native build regardless. Both already correctly scoped as "adapt, don't vendor," not mandatory.
- No Redis/Kafka/Kubernetes/Postgres/Mongo or other heavy infra present anywhere in the repo. SQLite 3.45.1 (FTS5-capable) remains sufficient for Stage 5 search at current scale.

### KEEP / CHANGE / REMOVE / ADD

**KEEP**
- Stage 2 read-only, GET-only, isolated-process API architecture and its typed-response discipline — matches outcome constraints exactly (1530/1530 passing); no changes needed.
- Donor posture: OpenTradex + Orallexa as non-mandatory presentation/layout donors, adapt-not-vendor, already correctly scoped in Stage 0.
- Recovered Journal & Search design (canonical projection, FTS5, structured dimensions via `run_id`, no LLM-generated SQL, no second trading-memory system) as the Stage 5 target design — needs to be re-added to `docs/architecture/` (a docs-location fact, not a re-decision).
- TradingView Lightweight Charts as the financial-charting candidate — no materially better fit found in repo evidence.
- Existing safety/infra invariants (no distributed infra without demonstrated need, Mission Control non-critical to trading, no live/broker-write paths) — no drift found anywhere in the current accepted architecture docs.

**CHANGE**
- Sequencing: do not revive the old "Stage 3→4→5 as one coordinated tranche, no intermediate STOP" model. Reinstate a real external STOP/reconciliation boundary at least between Stage 3 and Stage 4, because Stage 4 now carries a genuine new backend-scope item (per-candidate attribution) that should land and be reviewed before frontend work builds on top of it.
- Stage 4 scope: per operator decision below, target per-candidate specialist-agent attribution from the start rather than a run-level-only view. The additive backend read-model (see ADD) precedes/accompanies Stage 4 frontend work rather than being discovered mid-build.

**REMOVE**
- Nothing structural in-repo — the backend is already lean. Do not restore `MISSION_CONTROL_BUILD_TRANCHE.md`'s single-shot, no-gate authorization mechanism as-is; it is superseded by the repository-hygiene pass and this discovery.

**ADD**
- A small, additive-only, non-trading-semantic read-model that persists each research-phase agent's already-computed structured per-symbol result (rating/conviction/reasoning/risk-reward, already produced in memory) keyed by `run_id` + `symbol` + `agent_name`, following the same additive-forensic-persistence precedent Stage 2 Checkpoint C established for the `risk_gate` row. Must not add a `decision_id` to research-phase rows (preserves the existing, deliberately-tested `NULL` invariant) and must not touch trading/risk decision logic.
- A corresponding read-only API surface for the above, exposed the same way existing Stage-2 endpoints are (typed Pydantic response, independent read-only SQLite connection, GET-only), so Stage 4 can render true per-candidate agent cards.

### Operator product decisions

- **Stage 4 fidelity (asked, answered):** the operator chose per-candidate specialist-agent attribution from the start, accepting the added backend read-model scope, over a run-level-only view that would need no new persistence.

### Architecture consultation

- None requiring escalation beyond the CHANGE items above. The proposed sequencing/scope changes stay inside already-accepted safety/scope boundaries (additive-only, non-trading-semantic, read-only, no new distributed infrastructure), so they are presented as a proposed contract for ChatGPT/operator review rather than a conflict.

### Proposed implementation outcome contract

**Capabilities**
1. Stage 3 QAMC-native cockpit (React/Vite/Tailwind, TradingView Lightweight Charts) rendering real Stage-2 API data: account/P&L, positions, orders, trades, candidates, health. Responsive desktop + iPad. No mock data in production paths.
2. A small additive backend read-model + endpoint(s) persisting and exposing per-symbol structured specialist-agent output (rating, conviction, reasoning, risk/reward) per run, following the Checkpoint C additive-forensic-persistence precedent.
3. Stage 4 AI Decision Interface built on (1)+(2): per-candidate agent cards, disagreement/consensus visualization, and the PM→AI Risk→deterministic gate→executed/rejected delta view, using selective Orallexa/OpenTradex presentation adaptation only where cheaper than native work.
4. Stage 5 Journal & Search restoring the recovered design: canonical-projection daily journal (calendar/list/day views over the 12 documented sections), SQLite FTS5 structured/full-text search over the documented dimensions, visible validated filters if NL search is attempted, no arbitrary LLM-generated SQL.

**Constraints**
- Alpaca Paper only; `yebof/quant-agent` remains the authoritative engine; deterministic Python/broker protections remain final authority; specialist→PM→AI Risk→deterministic chain preserved.
- All new persistence is additive-only and never alters trading/risk decisions, execution eligibility, or existing `decision_id` semantics (research-phase `agent_logs` rows keep their correctly-`NULL` `decision_id`).
- Mission Control/API/journal/search remain read-only and non-critical to trading; API/UI failure has zero trading impact.
- No secrets, no fake production trading state, no new distributed infrastructure without demonstrated need.
- Frontend work is verified visually (running preview at desktop + iPad breakpoints, empty/error/loading states, real API-backed screens), not unit tests alone.

**Acceptance conditions**
- Stage 3: cockpit renders every accepted Stage-2 endpoint's real data with no mock fallback in the production code path; existing full suite stays green; visual verification evidence recorded at the stage boundary.
- New read-model: additive migration/tables only; full suite stays green; a dedicated test proves research-phase `agent_logs` rows still carry `NULL` `decision_id` (guarding the existing invariant); a new test proves per-symbol structured signals correctly join to their `run_id`.
- Stage 4: one candidate/decision can be followed end-to-end (per-symbol analyst view → PM proposal → AI Risk response → deterministic gate outcome → executed/rejected trade) without reading raw logs, verified against a real historical `run_id`.
- Stage 5: journal daily view reconstructs all 12 documented sections from canonical data only; search returns correct results against the documented structured dimensions; no endpoint accepts or generates arbitrary SQL.
- External STOP after Stage 3 self-verification and again after Stage 4 self-verification (not one silent pass through to Stage 5), each with a pushed branch and recorded evidence for ChatGPT/operator review.

## Handoff

When discovery is complete, commit/push the discovery branch and **STOP before implementation**. ChatGPT/operator review the actual GitHub result. An accepted result is merged before a fresh Claude implementation session starts.
