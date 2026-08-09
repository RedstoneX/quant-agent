# QAMC Documentation Map

QAMC uses **progressive disclosure**. Availability in Git does not mean “load this into context.”

## 👀 Operator view

- `knowledge/PROJECT_COMPASS.md` — the single human-readable dashboard for what is done, happening now, next, held/later, blockers/decisions, and safety.

The Compass is a projection. Machine-facing authority below wins if anything disagrees.

## ✅ Current authority

Read only what the task needs:

- `OUTCOME.md` — desired product/experiment result.
- `STATE.md` — accepted state and current authorization.
- `work/ACTIVE.md` — the single current discovery/implementation handoff.
- `ROADMAP.md` — concise active roadmap/gates.
- `decisions/ACTIVE.md` — operative decisions.
- `ACCEPTANCE_CRITERIA.md` — global implementation/checkpoint criteria.

## 🏗️ Accepted / current technical contracts

- `architecture/SAFETY_BOUNDARIES.md` — safety invariants and verified carve-outs.
- `architecture/MODEL_PROVIDER_ARCHITECTURE.md` — accepted provider/attribution contract from Stages 0.5–1.
- `architecture/MISSION_CONTROL_API.md` — accepted Stage-2 read-only API contract.
- `architecture/SYSTEM_ARCHITECTURE.md` — current authority/failure-domain boundaries.
- `architecture/MISSION_CONTROL.md` — Mission Control boundaries and **provisional** post-Stage-2 design questions during Discovery R1.

## 📚 On-demand reference — not authority

Prior proposals/research are preserved because they may still save engineering work, but they are not implementation instructions:

- `reference/mission-control/` — prior UI vision, screen states, component map, donor research, and journal/search proposal.
- `reference/future/` — future conceptual architecture such as Sentinel/live trading; **not authorized work**.
- `reference/UPSTREAM_CLAUDE_2026-08-09.md` — previous large Claude instruction/reference file; read only relevant portions.
- `history/legacy/` — superseded governance, plans, operator summaries, AgentLens research, and other historical snapshots.

A path-scoped Claude rule automatically marks `reference/` and `history/` content as evidence only when opened.

`CHECKPOINT_*_ACCEPTANCE.md` and `STAGE0_BASELINE_AUDIT.md` remain audit evidence in place because active technical contracts legitimately cite them.

## 🔗 Historical compatibility indexes

- `DECISIONS.md` — points old numbered-decision references to the preserved historical ledger.
- `MILESTONES.md` — points old milestone references to the preserved historical narrative.

These two tiny shims remain only to keep older audit links intelligible. Do not update them as live project state.

## 🤖 Claude Code control plane

- root `CLAUDE.md` — small always-on project contract.
- `.claude/rules/` — path-scoped constraints, including reference-material and Compass rules.
- `.claude/skills/qamc-discover` — outcome exploration / architecture challenge.
- `.claude/skills/qamc-build` — accepted implementation only.
- `.claude/skills/qamc-checkpoint` — gate closure and Compass refresh.
- `.claude/agents/` — isolated reviewers/test workers.
- `.claude/settings.json` + hooks — permissions, sandboxing, secret protection, and deterministic guardrails.

## Rule of thumb

**Outcome/state/work/decisions tell Claude what is true. Accepted architecture tells Claude what has already been proven. Reference/history tells Claude what we once considered.**
