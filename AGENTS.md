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