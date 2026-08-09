---
paths:
  - "*.md"
  - "docs/**/*.md"
  - ".claude/**/*.md"
---

# Documentation rules

- `docs/STATE.md` is the only live status/authorization file.
- `docs/ROADMAP.md` is the concise active plan.
- `docs/decisions/ACTIVE.md` contains operative decisions.
- Do not duplicate current status across multiple documents.
- Do not rewrite historical acceptance/audit evidence to make it look current.
- `docs/history/` and legacy snapshots are evidence, not implementation instructions.
- Prefer updating one live source rather than adding another handoff/compass/status document.
- Claude implementation commits should record internal stage boundaries; do not create a new checkpoint document for every internal gate unless evidence cannot be represented by tests/commit history.
