# QAMC — Claude Code Project Contract

QAMC is a private Alpaca paper-trading experiment built around `yebof/quant-agent`.
The trading engine is authoritative; Mission Control surrounds it and must never become a second trading engine.

## Start here

Read `docs/STATE.md` first.

For substantial work:
- **Discovery / architecture challenge:** use `/qamc-discover`.
- **Accepted implementation:** use `/qamc-build` only when `docs/STATE.md` and `docs/work/ACTIVE.md` explicitly authorize implementation.

Current authority, loaded only as needed:
- `docs/OUTCOME.md` — desired result;
- `docs/STATE.md` — accepted state/current authorization;
- `docs/work/ACTIVE.md` — current work handoff;
- `docs/ROADMAP.md` — active roadmap;
- `docs/decisions/ACTIVE.md` — operative decisions;
- relevant accepted technical contracts under `docs/architecture/`.

Do **not** preload the whole repository, the operator Compass, `docs/reference/`, or `docs/history/`.
Reference/history files are evidence or prior proposals, not current instructions; their path-scoped rule enforces this when opened.

## Non-negotiable boundaries

- Alpaca **Paper only**; live trading is not authorized.
- `yebof/quant-agent` remains the authoritative trading engine.
- Specialist agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution remains the decision chain.
- Deterministic Python and broker protections own final safety/execution eligibility; uncertainty fails closed.
- Mission Control/API/journal/search/UI remain non-critical to trading and read-only unless a future accepted work contract says otherwise.
- No secrets or fake production trading state in API/UI/client artifacts.
- Derived UI/search/journal state is rebuildable and non-authoritative.
- No unnecessary distributed infrastructure.
- Preserve upstream mergeability; avoid gratuitous trading-core rewrites.

Path-specific rules add detail only when relevant files are opened.

## How to think

Claude Code is an engineering and architecture participant, not merely a coder.

For substantial outcomes:
- start from the desired outcome, not from an assumption that an old plan is correct;
- inspect the actual repository before proposing or implementing;
- challenge prior architecture when a simpler/safer/better route exists;
- delegate outcomes, not implementation recipes;
- keep the lead context focused on synthesis, architecture, integration, and difficult reasoning.

### Route unknowns correctly

1. Repository fact → investigate it.
2. Routine engineering choice → decide it.
3. Genuine operator product/value trade-off → ask **one question at a time**, then reassess.
4. Material architecture/safety/governance issue needing independent reconciliation → record it in `docs/work/ACTIVE.md` for ChatGPT review through GitHub.

Do not ask the operator questions that source inspection, tooling, or engineering judgment can resolve.

## Cost-aware orchestration

- Use Explore/focused inexpensive subagents for repository search, high-volume reading, logs, and bounded test investigation.
- Use stronger isolated workers only when the task needs them.
- Use worktree isolation for parallel writers.
- Use full agent teams only when peer-to-peer coordination materially helps.
- If experimental orchestration creates friction, fall back to simpler supported delegation.
- Optimize engineering quality per unit of usage, not worker count.

Project auto-memory is disabled. Durable QAMC knowledge belongs in Git.
A usage reset is not a context reset; split/compact because context is unhealthy, not because allowance paused.

## Native workflows

- `/qamc-discover` — explore/challenge; no product implementation; push/STOP for reconciliation.
- `/qamc-build` — implement an accepted work contract only.
- `/qamc-checkpoint` — close implementation gates and refresh the operator Compass.
- `qamc-reviewer`, `qamc-test-runner`, `qamc-ui-reviewer` — isolated review/test workers.

## Git / acceptance

- Use dedicated branches.
- Never force-push or push implementation directly to `main`.
- Never merge a PR from Claude Code.
- Discovery and implementation hand off through committed/pushed GitHub state.
- ChatGPT/operator perform external reconciliation/acceptance and accepted merges.

## Testing

Use targeted checks while developing, broader tests when integration risk warrants them, and the governed full Python suite at external implementation handoff. UI work also requires runtime/visual verification. Never weaken safety verification to save usage.

## Deep reference

The previous large instruction/reference file is preserved at `docs/reference/UPSTREAM_CLAUDE_2026-08-09.md`. Read only relevant portions when a trading-core task genuinely requires that history.
