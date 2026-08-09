# QAMC — Claude Code Project Contract

QAMC is a private Alpaca paper-trading experiment built around `yebof/quant-agent`.
The trading engine is authoritative; Mission Control surrounds it and must never become a second trading engine.

## Start here

For implementation work, read `docs/STATE.md` first. Then read only the architecture or product documents relevant to the task.
Do **not** pre-read `docs/history/`, legacy governance snapshots, or the whole repository.

Useful live documents:
- `docs/STATE.md` — current accepted state and current authorization.
- `docs/ROADMAP.md` — active roadmap and gate structure.
- `docs/decisions/ACTIVE.md` — active architecture/product decisions.
- `docs/architecture/` — detailed contracts, read on demand.
- `docs/DONOR_COMPONENTS.md` and `docs/ui/UI_COMPONENT_MAP.md` — UI donor/product mapping when working on Mission Control.

Historical material is evidence, not current authority.

## Non-negotiable product boundaries

- Alpaca **Paper only**. Live trading is not authorized.
- `yebof/quant-agent` remains the authoritative trading engine.
- Specialist agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution remains the authoritative decision chain.
- Deterministic Python and broker protections own final safety/execution eligibility.
- Risk-system failure must fail closed.
- Mission Control, its API reads, journal, search, and UI must be non-critical to trading.
- Mission Control cannot place, cancel, modify, or bypass broker orders unless a future explicitly accepted write stage says otherwise.
- No secrets in API responses or client bundles.
- No production mock/demo state masquerading as real system state.
- Derived UI/search/journal indexes are rebuildable and non-authoritative.
- No Redis, Kafka, Kubernetes, MongoDB, PostgreSQL, or similar infrastructure without an accepted demonstrated need.
- AgentLens is out of plan.
- Preserve upstream mergeability; avoid gratuitous rewrites of the trading core.

Path-specific safety rules under `.claude/rules/` add detail only when relevant files are opened.

## How to work

You are the engineering lead for authorized implementation work. Own routine implementation decisions, decomposition, integration, testing, and debugging inside the accepted boundaries.

- Delegate **outcomes**, not implementation recipes.
- Do not ask the operator to decide routine engineering details that can be resolved from the repository or your judgment.
- Prefer safe parallelism over serial work when interfaces/file ownership are clean.
- Keep the lead context focused on architecture, integration, difficult decisions, and synthesis.
- Use built-in Explore or focused Haiku subagents for repository search, log/test investigation, and other bounded work where only the conclusion matters.
- Use a stronger focused worker for implementation/review when the task actually needs it.
- Use worktree isolation for parallel writers.
- Use full agent teams only when workers genuinely need peer-to-peer communication or sustained independent ownership; they are higher-cost and experimental, not a default.
- If an orchestration feature is unavailable or creates friction, fall back immediately to simpler subagents/sessions rather than debugging the orchestration framework.

Project auto-memory is intentionally disabled. Durable project knowledge belongs in Git, not in one Claude environment.

A subscription usage reset is not a context reset. Resume a coherent session after reset if its context remains healthy.
Split or compact because the **context** is unhealthy, not merely because the plan allowance paused.

## Native project workflows

- Use `/qamc-build` for the currently authorized implementation tranche.
- Use `/qamc-checkpoint` when closing an internal or external gate.
- Use the `qamc-reviewer` subagent for fresh-context independent review.
- Use the `qamc-test-runner` subagent for bounded test execution/investigation.
- Use the `qamc-ui-reviewer` subagent for UI/runtime review where browser/rendering tools are available.

## Git and publication

- Start implementation from current accepted `main` on a dedicated branch.
- Never force-push.
- Never push implementation directly to `main`.
- Never merge a PR from Claude Code. ChatGPT/operator governance handles accepted merges.
- Create clear commit boundaries at meaningful internal gates.
- Do not spend implementation-model usage on GitHub administration that can be handled after handoff.

Project hooks enforce a small set of these publication/live-safety boundaries deterministically.

## Testing

- During implementation, run the smallest targeted tests/checks that exercise changed behavior.
- Run broader tests when integration risk justifies them.
- Run the governed full Python suite at the external tranche/checkpoint handoff.
- UI work requires runtime/visual verification in addition to unit/type/build tests.
- Never weaken safety verification to save tokens.

## Common backend commands

```bash
pytest tests/ -q
python -m src.api
```

The API dependency is optional (`pip install -e '.[api]'`).

## Reference material

The previous large upstream/QAMC `CLAUDE.md` is preserved at
`docs/reference/UPSTREAM_CLAUDE_2026-08-09.md` for deep implementation history.
Read only the relevant section when a trading-core task genuinely requires it; do not load it by default.
