# QAMC — Engineering Agent Contract

QAMC is a private autonomous AI-assisted Alpaca trading system built around `yebof/quant-agent`. Its currently authorized execution environment is Alpaca Paper.

## Start

Read `docs/STATE.md` first, then `docs/WORK.md`. Load only the accepted contracts needed for the task. `docs/OUTCOME.md` defines the product result; `docs/STATE.md` records accepted truth; `docs/WORK.md` defines current work. `docs/FUTURE_*` is conceptual only.

## Hard boundaries

- **Execution environment:** Alpaca Paper only. Live-broker order submission requires separate explicit authorization.
- One trading architecture: Paper vs live is a broker/runtime boundary, not a separate decision path.
- Specialists → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution → broker remains authoritative.
- Deterministic Python and broker protections own final safety eligibility; uncertainty fails closed.
- Dashboard/API/journal/search/UI remain read-only and non-critical to trading unless explicitly changed by accepted work.
- Do not expose secrets or fabricate production state.
- Significant new persistent infrastructure, credential/security architecture, databases, proxies or orchestration still require explicit architectural approval.

## Account model

- **`ubuntu` = engineering/operator.** Codex/Claude, Git/GitHub, development tooling, tests, browser verification, Docker/sudo engineering work and deployment orchestration.
- **`qamc` = runtime only.** Production checkout, runtime `.env`/OneCLI, services/timers and Paper execution.
- **`dev` = parked.** Do not use it in normal work or expand its permissions.

Do not run engineering agents as `qamc` or turn `qamc` into a development account.

## Paper-beta autonomous delivery — HARD RULE

While QAMC remains Alpaca Paper, an engineering agent working on already-authorized work may complete the full lifecycle autonomously:

**diagnose → implement → test → inspect → PR → merge → deploy → verify → rollback if needed**.

There is **no mandatory external code-review, merge or deployment gate during Paper beta**. A separate reviewer may be used when it adds real value, but it is evidence, not permission. Do not stop merely to ask the operator to repeat approval already implied by the current authorized Paper task.

Prefer a dedicated PR for traceability. Verify the final head before merge. Deploy through `ubuntu` to the existing `qamc` runtime. If production verification fails, stop further mutation and restore/preserve the last known-good state using the existing rollback path.

The autonomous Paper-beta fast lane does **not** authorize live capital, paid dependencies, secrets/credential redesign, destructive infrastructure replacement or other material architecture outside current authority.

## Parallelism and agent use — HARD RULE

Use parallel work and subagents **proactively when independent work can be done safely at the same time and doing so reduces wall-clock time**.

- Partition by genuinely independent surfaces, questions, tests or evidence streams.
- The lead agent owns integration and resolves conflicting findings.
- Avoid duplicate agents re-deriving the same facts or overlapping edits to the same files without a clear reason.
- Multiple worktrees are allowed when they materially simplify independent work; they are not required for trivial tasks.
- Do not serialize independent investigation, testing, visual verification or analysis merely because one agent could eventually do all of it.

Match intelligence to the task:

- **Strongest available reasoning model:** architecture, trading logic, safety-sensitive changes, complex debugging, ambiguous product/UX judgment, difficult code review and cross-system integration.
- **Cheaper/faster workers:** targeted tests, repository/file inventory, log parsing, mechanical searches, evidence collection, formatting and other bounded deterministic work.
- Escalate a cheap worker when the task becomes ambiguous or reasoning-heavy instead of burning turns.

Parallelism is an efficiency tool, not a quota. More agents are not better when coordination costs exceed the work saved.

## Execution discipline

- Prefer outcome-driven work over chains of micro-prompts.
- Run the narrowest decisive test first; broaden only when evidence requires it.
- Do not repeatedly re-read unchanged authority or re-prove settled facts.
- For dashboard work, rendered desktop and iPad inspection is required; tests/builds alone are insufficient.
- Stop once the requested result is proven. Repeated validation without new evidence is waste.
- Keep handoffs short: **changed / verified / preview if relevant / unresolved blocker / production state**.

## Decision discipline

- Repository fact → investigate it.
- Routine engineering choice → decide it.
- Genuine operator product/value trade-off → use best judgment unless current authority leaves a material ambiguity.
- Material architecture/safety conflict → stop and surface it.

Do not turn the operator into the technical architect.

## Git and continuity

Git is durable QAMC memory. Use dedicated branches/PRs for substantive work, do not force-push, and keep rollback possible. Paper-beta autonomy includes merging the agent's own verified PR and deploying it.

Do not create new handoff/status/governance documents. `STATE.md` owns accepted state, `WORK.md` owns current work, and `PROJECT_COMPASS.md` is human-only.