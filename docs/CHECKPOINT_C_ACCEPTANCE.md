# Checkpoint C Acceptance — Stage 2

Date: 2026-08-09

Operator decision: **ACCEPTED**.

Stage 2 — Thin Read-Only Mission Control API — is accepted as complete after independent review of the original Stage 2 implementation through commit `919bfd033f9933430796b98c05ef6e49aecfa31b` and the bounded Checkpoint C completion slice at commit `2900c73d0c7b619e4009469cca5a2d3630f78c64`.

Verified acceptance basis:
- the Stage 2 branch remained cleanly based on the accepted `main` baseline, with no integration drift;
- the API is a separate read-only FastAPI/uvicorn process and is not on the trading critical path;
- broker-live reads are isolated from SQLite history reads;
- SQLite history reads use independent `mode=ro` connections;
- API routes are GET-only and cannot place, cancel, modify, or bypass broker orders;
- secret-bearing config objects are not exposed through response models;
- API death/absence was tested independently from ordinary trading DB writes;
- the governed candidates/watchlist requirement is now satisfied through a shared pure aggregation helper over canonical `insights.missed_opportunities_json` data, without importing `TradingPipeline` into `src.api` or creating a second candidate engine;
- fully deterministic hard-risk-blocked decisions are now forensically reconstructable through additive `agent_logs` rows using the sentinel `agent_name="risk_gate"`, with no schema change and no change to deterministic risk calculations, limits, eligibility, execution semantics, broker behavior, or Alpaca paper/live selection;
- the forensic persistence helper is non-critical and fails open with respect to logging only: persistence failure cannot alter an already-made deterministic block;
- Claude Code reported 9 new completion-slice tests and a final full suite of **1530 passed, 0 failed**;
- Stage 3 was not started.

Independent review initially held Checkpoint C for two substantive gaps: candidates/watchlist exposure and missing canonical forensic evidence for fully hard-risk-blocked runs. Commit `2900c73` closes both. Independent re-review recommends acceptance.

## Clarifications that supersede imprecise Stage 2 wording

1. A `risk_gate` forensic row can be written in **two** situations:
   - `pre_rm`: the deterministic hard-risk gate blocks every candidate before `risk_manager` is called;
   - `post_rm_modifications`: `risk_manager` was called, returned modifications, and the deterministic re-filter blocks every resulting candidate.

   Therefore documentation that describes every `risk_gate` row as occurring before `risk_manager` runs is incomplete.

2. For `/decisions/{decision_id}`, `hard_risk_block` is **not** necessarily `None` whenever a `risk_manager` row exists. In the `post_rm_modifications` path, both a real `risk_manager` LLM row and a deterministic `risk_gate` row can legitimately exist for the same decision.

3. The original Stage 2 implementation did not modify trading-critical files. The Checkpoint C completion slice subsequently modified `src/pipeline.py` and `src/pipeline_stages.py` only to extract the pre-existing pure watchlist aggregation and add fail-safe forensic persistence calls. The accurate final statement is: **deterministic trading/risk/execution semantics were not changed**.

This acceptance record is authoritative and supersedes any temporary wording in `AGENTS.md`, `docs/MILESTONES.md`, `docs/DECISIONS.md`, `docs/knowledge/PROJECT_COMPASS.md`, `docs/architecture/MISSION_CONTROL_API.md`, or code comments/docstrings that still say Stage 2 is awaiting acceptance, Stage 3 is blocked specifically on Checkpoint C, or state one of the three imprecise descriptions above.

**Stage 2 is DONE. Checkpoint C is ACCEPTED. Stage 3 — QAMC Native Cockpit — is AUTHORIZED as NEXT.**

Stage 4 and later stages remain blocked by their existing prerequisite checkpoints. No live-trading milestone is authorized.

The next bounded Stage 3 Claude Code session must start fresh, rehydrate from the current merged `main`, reconcile the larger governance documents to this accepted state before implementation changes, and then proceed only within Stage 3 scope. Stage 3 remains a frontend/cockpit milestone; it must not weaken the Stage 2 read-only API/trading isolation boundary.