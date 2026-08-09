# Upstream Integration Policy

Upstream: `yebof/quant-agent`  
Primary QAMC fork: `RedstoneX/quant-agent`

Bootstrap baseline: `6fc3cf14f4e6f9fde5f6c10fbe4a8d51e3d0f4e7`.

As re-verified on 2026-08-09, upstream `main` still points to that baseline. The QAMC fork now intentionally contains accepted QAMC implementation and governance changes beyond it; do **not** use the old “one documentation-only commit of divergence” description.

## Policy

- Treat upstream as the authoritative base project and QAMC as a controlled fork.
- Compare against current upstream before any sync; never assume upstream has or has not moved from old documentation.
- Prefer additive modules/adapters over deep changes to trading logic.
- Keep unavoidable core edits narrow, tested, and easy to review against upstream.
- Do not reformat/reorganize unrelated upstream code.
- Upstream updates are reviewed deliberately, never auto-merged into QAMC.
- Preserve accepted QAMC safety/experiment behavior when evaluating upstream changes.

## Current QAMC-owned surfaces

Accepted QAMC additions include:
- actual provider/model attribution and correlation plumbing;
- per-agent provider/model extension including the accepted OpenRouter path;
- additive forensic persistence needed for hard-risk reconstruction;
- the separate read-only Mission Control API;
- Claude-native QAMC repository operating/configuration files.

Mission Control UI/journal/search implementation remains **unaccepted/provisional during Discovery R1**.

## Preserve upstream behavior unless explicitly accepted otherwise

Agent roles/prompts, Portfolio Manager / AI Risk flow, deterministic risk rules, order/protection lifecycle, memory/reflection/Meta Reflector, scheduler semantics, and canonical trading records remain upstream-owned behavior except for narrow QAMC changes already accepted through checkpoints.

## Historical evidence

The Stage-0 baseline/divergence investigation is preserved in `docs/STAGE0_BASELINE_AUDIT.md`. Use that as historical evidence, not as a claim about future upstream state.
