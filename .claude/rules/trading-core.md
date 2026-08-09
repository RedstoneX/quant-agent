---
paths:
  - "main.py"
  - "src/pipeline.py"
  - "src/pipeline_stages.py"
  - "src/risk/**/*"
  - "src/execution/**/*"
  - "config/settings.yaml"
---

# Trading-core safety rules

These files are safety-sensitive.

- Do not change deterministic risk limits, risk-gate semantics, broker protection, order eligibility, or Alpaca paper/live selection unless the current authorization explicitly includes that change.
- **Current Discovery R1 authorizes no trading-core behavior changes.** Any later Mission Control work also needs explicit authorization before touching trading/risk semantics.
- Risk failures must fail closed.
- Preserve the decision chain: specialist agents → PM → AI Risk Manager → deterministic Python → broker.
- A logging/forensic persistence failure must never relax a deterministic block.
- Preserve exact SELL allocation semantics and existing cash/margin safeguards.
- Treat `src/agents/base.py::_execute()` retry/deadline/failover behavior as hardened; do not casually refactor adjacent provider execution.
- Before changing trading-core behavior, read `docs/architecture/SAFETY_BOUNDARIES.md` and only the relevant portion of `docs/reference/UPSTREAM_CLAUDE_2026-08-09.md`.
- Run targeted safety tests for any touched trading-critical path.
