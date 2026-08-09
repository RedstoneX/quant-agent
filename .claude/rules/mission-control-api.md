---
paths:
  - "src/api/**/*"
---

# Mission Control API rules

- The API is a read-only adapter over broker-readable state and canonical/history data.
- Keep broker-live reads structurally separate from SQLite historical reads.
- Historical SQLite access uses independent read-only connections; do not share the trading writer connection.
- Do not expose broker order-placement/cancel/replace methods.
- Do not expose secrets or secret-bearing config objects.
- Do not import Mission Control into the trading process.
- API death/absence must have zero effect on trading or broker protection.
- Prefer typed response contracts; do not return arbitrary database rows.
- Additional Stage 4–5 read endpoints/read models are allowed only when they preserve these properties.
