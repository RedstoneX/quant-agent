# Documentation Governance

## Authority order
1. `AGENTS.md` — permanent project operating rules.
2. Existing upstream `CLAUDE.md` — inherited implementation invariants unless explicitly superseded by an accepted QAMC decision.
3. `docs/DECISIONS.md` — accepted QAMC architecture/product decisions.
4. `docs/architecture/*.md` — detailed architecture contracts.
5. `docs/MILESTONES.md` — approved sequence/status.
6. `docs/ACCEPTANCE_CRITERIA.md` — completion gates.
7. `docs/knowledge/PROJECT_COMPASS.md` — navigation/current-state summary.
8. Other notes/reference material.

When documents conflict, higher authority wins. Do not silently resolve conflicts; record the reconciliation in `DECISIONS.md`.

## Change discipline
At every accepted milestone:
- update `MILESTONES.md` status;
- record material architecture decisions in `DECISIONS.md`;
- update relevant architecture docs if contracts changed;
- update `PROJECT_COMPASS.md` with current state and next bounded task.

## Obsidian
Obsidian is a reading/navigation layer over repository Markdown. Do not maintain a second authoritative QAMC plan outside Git.

## Upstream preservation
Do not rewrite upstream documentation just to match QAMC style. Add QAMC-specific documents and only edit upstream files when implementation genuinely requires it.
