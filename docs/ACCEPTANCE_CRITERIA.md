# QAMC Acceptance Criteria

## Global criteria for every milestone
- No regression to deterministic hard risk or broker protection.
- Alpaca environment remains unmistakably paper-only.
- Upstream tests pass, plus new targeted tests for changed behavior.
- Trading process remains independent of dashboard and AgentLens availability.
- No mock/demo data may masquerade as production state.
- Actual model/provider used is recorded for every LLM invocation relevant to experimentation.
- New persistence used for UI/search is derived/rebuildable unless explicitly declared canonical.
- No new infrastructure without documented necessity.
- Documentation and Project Compass updated before milestone is marked DONE.

## Engineering stop rule
If an optional feature requires invasive changes across multiple upstream subsystems, broad schema migration, a new distributed service, or sustained debugging disproportionate to experimental value, stop and report alternatives. Deferral is an acceptable outcome.

## Safety regression tests
At minimum validate:
- hard risk still fails closed;
- dashboard/API shutdown does not stop trading;
- AgentLens shutdown does not stop trading;
- frontend cannot place/circumvent orders directly;
- paper/live configuration cannot be casually confused;
- approved provider changes cannot alter hard safety limits;
- prompt evolution cannot modify protected risk behavior.
