# QAMC Documentation Map

QAMC documentation is organized for **progressive disclosure**. Do not load everything.

## Live documents

- `OUTCOME.md` — desired product/experiment result. Start here for substantial new work.
- `STATE.md` — what is accepted/authorized now.
- `work/ACTIVE.md` — single current discovery/implementation handoff contract.
- `ROADMAP.md` — concise product roadmap/gates.
- `decisions/ACTIVE.md` — decisions that currently govern the system.
- `ACCEPTANCE_CRITERIA.md` — global safety/completion criteria for implementation.

`PROJECT_CHARTER.md` is retained as a compatibility pointer to `OUTCOME.md`; do not maintain a second competing statement of the project objective.

## Architecture — read only when relevant

`architecture/` contains detailed contracts:
- `SYSTEM_ARCHITECTURE.md`
- `SAFETY_BOUNDARIES.md`
- `MODEL_PROVIDER_ARCHITECTURE.md`
- `MISSION_CONTROL.md`
- `MISSION_CONTROL_API.md`
- `JOURNAL_AND_SEARCH.md`
- `LIVE_TRADING.md` (future architecture only; not an implementation authorization)

UI source/donor references:
- `DONOR_COMPONENTS.md`
- `ui/UI_COMPONENT_MAP.md`

## Claude Code operating model

Claude-specific behavior uses native project primitives:
- root `CLAUDE.md` — small always-on contract;
- `.claude/rules/` — path-scoped rules;
- `.claude/skills/qamc-discover` — outcome exploration/architecture challenge;
- `.claude/skills/qamc-build` — accepted implementation only;
- `.claude/skills/qamc-checkpoint` — implementation gate closure;
- `.claude/agents/` — isolated reviewers/test workers;
- `.claude/settings.json` + hooks — permissions, sandboxing and deterministic guardrails.

## Historical evidence

Checkpoint reports, audits, previous governance systems, and superseded state summaries are evidence, not current instructions unless a live document explicitly links to them.

The pre-redesign governance snapshots are preserved under `history/legacy/`.
Existing checkpoint acceptance files remain audit records.
