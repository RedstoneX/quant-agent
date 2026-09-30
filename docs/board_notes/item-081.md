## item 81 — RETIRED 2026-09-24

**Moved from WORK.md (2026-09-24) —** `SUBFLOOR_SIZE_CAPPED_STATUS` (`src/agents/portfolio_manager.py:59`) is assigned nowhere and asserted only by `tests/test_subfloor_catalyst_gate.py:841`. Keeping a key so a silent rename cannot resurrect a threshold is a real argument, so decide once and record it — keep with a ledger note, or delete. NO LIVE NUMBER'S VALUE CHANGES either way.

**Retired 2026-09-24** — decided: delete. `SUBFLOOR_SIZE_CAPPED_STATUS`, `RiskConfig.min_reward_risk_after_widening` and `ConstructorConfig.min_reward_risk_after_widening` were removed as zero-reader dead code, each re-verified against the current tree first. `REWARD_RISK_FLOOR` itself was NOT deleted — `ops/model_policy/deterministic_selection.py` still reads it in a real comparison for the model-selection benchmark. Full writeup in `docs/INCIDENT_HISTORY.md` (2026-09-24 entry).

