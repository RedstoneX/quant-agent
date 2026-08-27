# QAMC Engineering Contract

Current engineering lead: **Codex**. This contract is intentionally agent-neutral so any capable engineering agent can follow the same rules.

## Start

Read `docs/STATE.md`, then `docs/WORK.md`. Use `docs/OUTCOME.md` for product intent and only the accepted architecture/contracts relevant to the task. `docs/FUTURE_*` is conceptual only.

## Paper-beta autonomy

While QAMC remains Alpaca Paper, already-authorized engineering may run end-to-end without a human review/merge/deploy gate:

**diagnose → implement → test → inspect → PR → merge → deploy → verify → rollback if needed**

Use `ubuntu` for engineering/deployment orchestration. Keep `qamc` runtime-only. Keep `dev` parked.

This fast lane does not authorize live capital, paid dependencies, secrets/credential redesign, destructive infrastructure replacement, or material architecture outside current authority.

## Parallelism — systemwide engineering policy

Use parallel workers/subagents proactively when independent work can safely run at the same time and doing so shortens the critical path.

- Parallelize independent investigation, code surfaces, targeted tests, logs/evidence, browser/visual verification and documentation checks.
- The lead agent owns integration and resolves conflicting findings.
- Avoid duplicate fan-out, repeated fact-finding, or overlapping writes without clear ownership.
- Use separate worktrees when they materially simplify independent implementation.
- Use the strongest available reasoning model for architecture, trading logic, safety-sensitive changes, hard debugging, difficult review and ambiguous UX/product judgment.
- Use cheaper/faster workers for bounded tests, searches, inventories, log parsing and mechanical evidence collection.
- Escalate a cheap worker when the work becomes ambiguous or reasoning-heavy.

Parallelism is an efficiency tool, not an agent-count target.

## Hard boundaries

- Alpaca Paper only; live-broker order submission needs separate explicit authorization.
- Preserve **Specialists → Portfolio Manager → AI Risk → deterministic Python → broker**.
- Deterministic risk/broker protections remain final authority and fail closed.
- Mission Control/API/journal/search/UI remain read-only and non-critical to trading unless accepted work explicitly changes that.
- Do not expose secrets or fabricate production state.
- Existing trading records remain canonical; UI/journal/search projections are derived and must not become a second trading-memory system.
- Before trading-core changes, read `docs/architecture/SAFETY_BOUNDARIES.md`.
- For API/read-side changes, preserve the isolation contract in `docs/architecture/MISSION_CONTROL_API.md`.

## Execution discipline

- Prefer outcome-driven work over micro-prompts.
- Run the narrowest decisive test first; broaden only when evidence requires it.
- Do not re-read unchanged authority or re-prove settled facts.
- UI/frontend acceptance requires rendered desktop and iPad inspection; tests/builds alone are insufficient.
- Stop when the result is proven. Re-validation without new evidence is waste.
- Keep handoffs short: **changed / verified / preview if relevant / unresolved blocker / production state**.

## Git and continuity

Use dedicated branches/PRs for substantive work. Do not force-push or push implementation directly to `main`. Paper-beta autonomy includes merging the agent's own verified PR and deploying it. Keep rollback possible.

Do not create new governance/status documents. `STATE.md` owns accepted truth, `WORK.md` owns active work, and `PROJECT_COMPASS.md` is human-only.

## Governance ratification

`OUTCOME.md`, `STATE.md` and `WORK.md` define the mandate this system is built to. Agents may **propose** changes to them; only the **owner** may **accept** those changes.

- Put proposed governance changes in a PR and state the change plainly in the PR description. It becomes accepted truth when the owner merges it — not before.
- Never record an engineering decision in these documents as though it were an owner requirement. If scope was cut, a capability deferred, or a constraint adopted for implementation convenience, say so explicitly and attribute it to the agent that decided it.
- Treat any constraint already present in these documents as **unverified** if it lacks such attribution. Ask rather than inherit it.

**Why this rule exists.** A 2026-08-27 review found that scope decisions taken by coding agents had been written into these documents as accepted constraints, and every later agent then treated them as instructions from the owner. Direct short selling is the clearest case: an agent decided it was out of scope, recorded that in the governance docs, and the system was subsequently built long-only against an owner mandate that never excluded shorting. The same mechanism recorded the project's purpose as a research question about model quality, when the actual mandate is to make money.

Nobody misrepresented anything. Decisions laundered into requirements because there was no ratification step. This is that step.