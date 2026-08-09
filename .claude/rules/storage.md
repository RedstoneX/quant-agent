---
paths:
  - "src/storage/**/*"
  - "src/watchlist_candidates.py"
---

# Persistence rules

- Existing trading records remain canonical unless an accepted decision says otherwise.
- UI/journal/search projections and indexes are derived, non-authoritative, and rebuildable.
- Do not create a second trading-memory system.
- Prefer the existing SQLite/local-storage architecture.
- Canonical schema changes require a demonstrated need; use the project's additive/idempotent migration pattern when a governed change is necessary.
- Any later read/search index must be explicitly required by the accepted work contract and its loss must have zero effect on trading.
