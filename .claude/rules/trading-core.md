---
paths:
  - "main.py"
  - "src/pipeline.py"
  - "src/pipeline_stages.py"
  - "src/risk/**/*"
  - "src/execution/**/*"
  - "config/settings.yaml"
---

# Trading-core safety

These files are safety-sensitive.

- Do not change deterministic risk limits, risk-gate semantics, broker protection, order eligibility or Alpaca paper/live selection unless `docs/STATE.md` + `docs/WORK.md` explicitly authorize it.
- Risk failures must fail closed.
- Preserve specialist agents → PM → AI Risk Manager → deterministic Python → broker.
- Logging/forensic persistence failure must never relax a deterministic block.
- Preserve SELL allocation and existing cash/margin safeguards.
- Treat `src/agents/base.py::_execute()` retry/deadline/failover behavior as hardened; do not casually refactor it.
- Before authorized trading-core changes, read `docs/architecture/SAFETY_BOUNDARIES.md` and inspect the relevant code/tests/Git history.
- Run targeted safety tests for touched trading-critical paths.
