# QAMC Agent Operating Rules

This fork is the authoritative repository for **Quant Agent Mission Control (QAMC)**, a private paper-trading experiment built around upstream `yebof/quant-agent`.

## Read order before any work
1. Existing root `CLAUDE.md` — inherited upstream trading invariants and implementation knowledge.
2. `docs/knowledge/PROJECT_COMPASS.md` — navigation and current project state.
3. `docs/knowledge/AI_OPERATING_SYSTEM.md` — model/session/context economy and AI development workflow.
4. `docs/DECISIONS.md` — frozen decisions; do not reopen them casually.
5. `docs/MILESTONES.md` — current approved work stage.
6. The architecture document(s) relevant to the bounded task.
7. `docs/ACCEPTANCE_CRITERIA.md` before claiming completion.

## Non-negotiable operating rules
- `yebof/quant-agent` remains the authoritative trading engine. Do not redesign or replace it.
- Alpaca Paper only. Live trading is not authorized.
- AI may analyze, judge, critique and reflect; deterministic Python remains final safety/execution authority.
- Risk-system failure must fail closed.
- Do not move trading logic into Mission Control.
- Dashboard or observability failure must not stop trading or weaken broker-side protection.
- Prefer existing upstream functionality and approved donor components over custom code.
- No optional integration is entitled to unlimited engineering effort. If a feature requires invasive architecture changes, broad schema migration or substantial bespoke infrastructure, STOP and report the finding.
- One bounded slice at a time. Verify, test, document, commit, checkpoint.
- Follow `docs/knowledge/AI_OPERATING_SYSTEM.md` for Claude model choice, fresh-session discipline, context limits and testing economy.
- Do not implement a later milestone until its prerequisite checkpoint is accepted.
- Do not introduce Redis, Kafka, Kubernetes, MongoDB, PostgreSQL or other infrastructure unless a demonstrated requirement is approved.
- Preserve upstream mergeability: isolate QAMC changes, avoid gratuitous rewrites, and document divergence.

## Documentation authority
GitHub Markdown in this repository is the durable source of truth. Obsidian may index/read it but is not an independent authority.

## Current stage restriction
**Stage 0, Stage 0.5 (D-1 actual-model attribution hotfix) and Stage 1
(Provider, Model & Correlation Plumbing) are all accepted. Checkpoint B was
ACCEPTED by the operator 2026-08-09** (`docs/CHECKPOINT_B_ACCEPTANCE.md`,
`docs/MILESTONES.md`).

Stage 1's scope was: explicit per-agent provider/model configuration
alongside the existing prefix-inference (backward compatible), one new
inexpensive-provider path (OpenRouter) through the least-invasive seam,
additive `agent_logs`/`trades` telemetry columns via the existing
`_ensure_column` mechanism, and a minimal `decision_id` correlation column.
`src/agents/base.py::_execute()`'s hardened retry/deadline/failover loop body
was preserved untouched, per Stage 0's explicit recommendation.

**Stage 2 (Thin Read-Only Mission Control API) is AUTHORIZED as the current
bounded stage.** Mission Control UI, journal code, risk changes and donor UI
code remain out of Stage 2's scope and stay BLOCKED behind their own
prerequisite checkpoints.

AgentLens is **out of plan** (DECISION #34) — do not instrument, integrate or
pilot it.
