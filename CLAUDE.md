# QAMC — Claude Code Project Contract

QAMC is a private Alpaca paper-trading experiment built around `yebof/quant-agent`.
The trading engine is authoritative; Mission Control surrounds it and must never become a second trading engine.

## Start here

Read `docs/STATE.md` first.

For substantial new work, the repository has two distinct phases:
- **Discovery / architecture challenge:** use `/qamc-discover`.
- **Accepted implementation:** use `/qamc-build` only when `docs/STATE.md` and `docs/work/ACTIVE.md` explicitly authorize implementation.

Useful live documents:
- `docs/OUTCOME.md` — the result the product must achieve.
- `docs/STATE.md` — what is accepted and what is authorized now.
- `docs/work/ACTIVE.md` — the single current discovery/implementation handoff contract.
- `docs/ROADMAP.md` — concise product roadmap/gates.
- `docs/decisions/ACTIVE.md` — operative decisions.
- `docs/architecture/` — detailed contracts, read on demand.

Do not pre-read `docs/history/`, legacy governance snapshots, or the whole repository.
Historical material is evidence, not current authority.

## Non-negotiable product boundaries

- Alpaca **Paper only**. Live trading is not authorized.
- `yebof/quant-agent` remains the authoritative trading engine.
- Specialist agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution remains the authoritative decision chain.
- Deterministic Python and broker protections own final safety/execution eligibility.
- Risk-system failure must fail closed.
- Mission Control/API/journal/search/UI must be non-critical to trading.
- Mission Control cannot place, cancel, modify, or bypass broker orders unless a future explicitly accepted write stage says otherwise.
- No secrets in API responses or client bundles.
- No production mock/demo state masquerading as real system state.
- Derived UI/search/journal indexes are rebuildable and non-authoritative.
- No unnecessary distributed infrastructure.
- AgentLens is out of plan.
- Preserve upstream mergeability; avoid gratuitous trading-core rewrites.

Path-specific safety rules under `.claude/rules/` load additional detail only when relevant.

## How to think

Claude Code is an engineering and architecture participant, not merely a coder.

For substantial new outcomes:
- start from `docs/OUTCOME.md`, not from an assumption that the existing plan is correct;
- explore the actual repository before proposing or implementing;
- challenge existing architecture when a simpler/safer/better route exists;
- delegate **outcomes**, not implementation recipes;
- keep the lead context focused on synthesis, architecture, integration, and difficult reasoning.

### Question routing

Do not turn the operator into the technical architect.

1. Repository fact → investigate it yourself.
2. Routine engineering choice → decide it yourself.
3. Genuine operator product preference/value trade-off → ask **one question at a time**, wait for the answer, then reassess.
4. Material architecture/safety/governance issue needing independent reconciliation → record it in `docs/work/ACTIVE.md` for ChatGPT review through GitHub.

Do not ask questions merely because asking is cheaper than investigating.

## Cost-aware orchestration

- Use built-in Explore or focused Haiku subagents for high-volume reading, repository search, logs, and bounded test investigation.
- Use a stronger focused worker when implementation/review actually needs it.
- Use worktree isolation for parallel writers.
- Use full agent teams only when workers genuinely need peer-to-peer communication or sustained independent ownership.
- If experimental orchestration creates friction, fall back immediately to simpler supported delegation.
- Optimize engineering quality per unit of usage, not worker count.

Project auto-memory is intentionally disabled. Durable QAMC knowledge belongs in Git.
A subscription usage reset is not a context reset; compact/split because context is unhealthy, not because allowance paused.

## Native project workflows

- `/qamc-discover` — explore/challenge a substantial outcome; **no implementation**; GitHub handoff afterward.
- `/qamc-build` — implement only an accepted/authorized work contract.
- `/qamc-checkpoint` — close an internal or external implementation gate.
- `qamc-reviewer` — fresh-context independent implementation review.
- `qamc-test-runner` — bounded low-cost test execution/investigation.
- `qamc-ui-reviewer` — independent UI/runtime review when rendering tools exist.

## Git and publication

- Use dedicated branches for discovery and implementation.
- Never force-push.
- Never push implementation directly to `main`.
- Never merge a PR from Claude Code; ChatGPT/operator governance handles accepted merges.
- Discovery must be committed/pushed before architecture reconciliation.
- Implementation must be committed/pushed before external acceptance.
- GitHub is the handoff between Claude sessions and between Claude and ChatGPT.

Project settings/hooks enforce a small set of secret/publication/live-safety boundaries mechanically.

## Testing

- During implementation, run the smallest targeted checks that exercise changed behavior.
- Run broader tests when integration risk warrants them.
- Run the governed full Python suite at external implementation handoff.
- UI work requires runtime/visual verification as well as build/type/test checks.
- Never weaken safety verification to save tokens.

## Reference material

The previous large upstream/QAMC `CLAUDE.md` is preserved at
`docs/reference/UPSTREAM_CLAUDE_2026-08-09.md`.
Read only relevant portions when a trading-core task genuinely requires that history.
