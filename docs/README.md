# QAMC Documentation Map

QAMC documentation is organized for **progressive disclosure**. Do not load everything.

## Live documents

- `STATE.md` — what is accepted/authorized now.
- `ROADMAP.md` — concise active stages/gates.
- `decisions/ACTIVE.md` — decisions that currently govern the system.
- `PROJECT_CHARTER.md` — experiment objective and success definition.
- `ACCEPTANCE_CRITERIA.md` — global safety/completion criteria.

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

Claude-specific operating behavior does **not** live in a stack of governance documents anymore.

- root `CLAUDE.md` — small always-on project contract;
- `.claude/rules/` — path-scoped rules loaded only when relevant;
- `.claude/skills/` — on-demand workflows such as build/checkpoint;
- `.claude/agents/` — reusable isolated reviewers/test workers;
- `.claude/settings.json` + hooks — deterministic guardrails.

## Historical evidence

Checkpoint reports, audits, previous governance systems, and superseded state summaries are historical evidence.
They are not instructions for current implementation unless a live document explicitly links to them.

The pre-redesign governance snapshots are preserved under `history/legacy/`.
Existing checkpoint acceptance files in `docs/` remain audit records.

When current and historical text conflict, `STATE.md`, `decisions/ACTIVE.md`, the relevant current architecture contract, and root/native Claude configuration govern current work.
