# QAMC Agent Operating Rules

This fork is the authoritative repository for **Quant Agent Mission Control (QAMC)**, a private paper-trading experiment built around upstream `yebof/quant-agent`.

## Read order before any work
1. Existing root `CLAUDE.md` — inherited upstream trading invariants and implementation knowledge.
2. Current explicit acceptance/authorization records relevant to the task. For the active Mission Control UI tranche, read `docs/CHECKPOINT_C_ACCEPTANCE.md` and `docs/MISSION_CONTROL_BUILD_TRANCHE.md` **before older navigation/status documents**, because they supersede temporary Stage-2/3/4/5 wording that has not yet been fully reconciled everywhere.
3. `docs/knowledge/PROJECT_COMPASS.md` — navigation and project history/current-state detail.
4. `docs/knowledge/AI_OPERATING_SYSTEM.md` — model/session/context economy and AI development workflow.
5. `docs/DECISIONS.md` — frozen decisions; do not reopen them casually.
6. `docs/MILESTONES.md` — stage definitions and acceptance criteria.
7. The architecture document(s) relevant to the authorized work.
8. `docs/ACCEPTANCE_CRITERIA.md` before claiming completion.

## Non-negotiable operating rules
- `yebof/quant-agent` remains the authoritative trading engine. Do not redesign or replace it.
- Alpaca Paper only. Live trading is not authorized.
- AI may analyze, judge, critique and reflect; deterministic Python remains final safety/execution authority.
- Risk-system failure must fail closed.
- Do not move trading logic into Mission Control.
- Dashboard/API/journal/search failure must not stop trading or weaken broker-side protection.
- Prefer existing upstream functionality and approved donor components over custom code when adaptation is genuinely cheaper.
- No optional integration is entitled to unlimited engineering effort. If a feature requires invasive architecture changes, broad safety-sensitive schema migration or substantial bespoke infrastructure, STOP and report the finding.
- Work only inside the currently authorized stage or tranche. Within an authorized tranche, maximize safe parallelism under `docs/knowledge/AI_OPERATING_SYSTEM.md` rather than serializing work unnecessarily.
- Do not introduce Redis, Kafka, Kubernetes, MongoDB, PostgreSQL or other infrastructure unless a demonstrated requirement is approved.
- Preserve upstream mergeability: isolate QAMC changes, avoid gratuitous rewrites, and document divergence.
- AgentLens is out of plan (DECISION #34). Do not instrument, integrate or pilot it.

## Documentation authority
GitHub Markdown in this repository is the durable source of truth. Obsidian may index/read it but is not an independent authority.

When an explicit checkpoint acceptance or tranche-authorization record states that it supersedes older temporary status wording, the newer record is authoritative until the larger navigation documents are reconciled.

## Current accepted state
- Stage 0 / Checkpoint A: **DONE / ACCEPTED**.
- Stage 0.5 / Checkpoint A5: **DONE / ACCEPTED**.
- Stage 1 / Checkpoint B: **DONE / ACCEPTED**.
- Stage 2 / Checkpoint C: **DONE / ACCEPTED**. Authoritative record: `docs/CHECKPOINT_C_ACCEPTANCE.md`.
- Stage 2 delivered the separate read-only Mission Control API and the Checkpoint C completion slice: real `/candidates` derived from canonical insights plus additive `risk_gate` forensic records for fully deterministic hard-risk blocks. Final reported suite: **1530 passed, 0 failed**.

## Current authorization
**Stages 3–5 are authorized as one coordinated Mission Control build tranche** under `docs/MISSION_CONTROL_BUILD_TRANCHE.md`.

This is a bounded exception to the normal external STOP between every stage:
- Claude Code is authorized to orchestrate and implement Stage 3 → Stage 4 → Stage 5 in one coherent tranche;
- it must self-verify and create clear commit/documentation boundaries at each internal stage gate;
- it may proceed across those internal gates when green without waiting for intermediate operator/ChatGPT acceptance;
- it must STOP after Stage 5 / Checkpoint E self-verification, push the tranche branch and hand off for independent ChatGPT/operator review before merge.

Stages 7–9 are outside this authorization. Stage 8 write controls and all live-trading work remain unauthorized.

## Mission Control tranche architecture headline
Keep quant-agent intact. Consume the accepted Stage-2 read-only API. Build the QAMC-native React/Vite/Tailwind Mission Control experience using OpenTradex and Orallexa only as selective presentation/concept donors, TradingView Lightweight Charts for financial visualization, and canonical-derived/rebuildable journal/search state. Do not import donor backend/trading assumptions.
