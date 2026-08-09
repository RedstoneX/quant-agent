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
**Stage 0 was accepted 2026-08-09. The authorized stage is now Stage 0.5 —
the D-1 actual-model attribution hotfix** (`docs/MILESTONES.md`).

Work only within Stage 0.5's declared scope: the nine `insert_agent_log(...)`
call sites plus targeted tests. Do **not** touch `src/agents/base.py::_execute`,
the database schema, provider routing or trading behavior, and do not add
OpenRouter, Mission Control, journal code, risk changes or donor UI code.
Stage 1 stays blocked until Checkpoint A5 is accepted.

AgentLens is **out of plan** (DECISION #34) — do not instrument, integrate or
pilot it.
