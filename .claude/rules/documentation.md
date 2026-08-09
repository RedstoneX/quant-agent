---
paths:
  - "*.md"
  - "docs/**/*.md"
  - ".claude/**/*.md"
---

# Documentation rules

- `docs/STATE.md` is the only live status/authorization authority.
- `docs/ROADMAP.md` is the concise active plan authority.
- `docs/work/ACTIVE.md` is the current discovery/implementation handoff contract.
- `docs/decisions/ACTIVE.md` contains operative decisions.
- `docs/knowledge/PROJECT_COMPASS.md` is the **operator-facing projection/dashboard**, not an authority. Keep it synchronized from the authoritative live files whenever meaningful project state changes.
- Do not create any additional handoff/compass/status document.
- Do not rewrite historical acceptance/audit evidence to make it look current.
- `docs/history/` and legacy snapshots are evidence, not implementation instructions.
- Claude implementation commits should record internal stage boundaries; do not create a new checkpoint document for every internal gate unless evidence cannot be represented by tests/commit history.

## Project Compass presentation contract

When refreshing `docs/knowledge/PROJECT_COMPASS.md`:
- preserve the existing operator-friendly structure and Obsidian callouts;
- write for a non-coder/operator who benefits from fast visual scanning and low information density;
- use emojis as consistent visual landmarks;
- prefer short plain-English bullets over technical prose;
- keep current state and next action near the top;
- preserve the core sections: **RIGHT NOW, PROJECT MAP, WHAT JUST HAPPENED, NEXT MOVES, BLOCKERS / DECISIONS, SAFETY**;
- use status markers consistently: ✅ DONE, 🟡 NOW, ⏸ HELD, ⬜ LATER, ❌ REMOVED;
- show what is complete, what is happening now, what comes next, and what remains later without requiring the operator to inspect `STATE.md` or `ROADMAP.md`;
- do not turn the Compass into a technical specification, changelog, or implementation handoff;
- derive facts from authoritative live files rather than independently inventing status.
