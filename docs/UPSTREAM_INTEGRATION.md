# Upstream Integration Policy

Upstream: `yebof/quant-agent`.
Primary fork: `RedstoneX/quant-agent`.
Fork baseline at project bootstrap: `6fc3cf14f4e6f9fde5f6c10fbe4a8d51e3d0f4e7`.

**Verified in Stage 0 (2026-08-09).** That SHA is `HEAD~1` in this fork and is
also the current `main` HEAD of `yebof/quant-agent` — the fork is *level with*
upstream, not behind it. Total divergence is one commit, `0fd004b`
(documentation only: 20 files, +598 lines, no change to `src/`, `tests/`,
`config/`, `scripts/` or `main.py`). Upstream mergeability is unencumbered.

Known policy gap (Stage 0 **D-6**): no `upstream` remote is configured in the
development environment, contrary to the policy below. Harmless while the fork
is level with upstream, but unmet.

## Policy
- Maintain an `upstream` remote in development environments.
- Prefer additive modules/adapters over edits deep inside trading logic.
- When core edits are unavoidable, keep them narrow, tested and documented.
- Do not reformat/reorganize unrelated upstream code.
- Record meaningful divergence and merge hazards here.
- Upstream updates are reviewed/merged deliberately, not auto-synchronized.

## QAMC-owned areas (expected, exact paths to be chosen after Stage 0)
- provider/config extension;
- telemetry/correlation extension;
- Mission Control thin API;
- Mission Control frontend;
- journal/search read model;
- AgentLens adapter.

## Upstream-owned behavior to preserve
Agent prompts/roles, PM/Risk flow, deterministic rules, order/protection lifecycle, memory/reflection/Meta Reflector, scheduler semantics and canonical trading records unless an explicit accepted decision changes them.
