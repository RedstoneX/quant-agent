# QAMC — Claude Code Contract

QAMC is a private autonomous AI-assisted Alpaca trading system built around `yebof/quant-agent`. Its currently authorized execution environment is Alpaca Paper.

**Terminology:** **QAMC / Mission Control** means the whole product/system. **Dashboard** means the browser/iPad read-side UI and its concurrent frontend/UX workstream. **Core recovery** means the trading/backend deployment-and-validation workstream. Do not use “Mission Control workstream” as shorthand for dashboard-only work.

## Start

Read `docs/STATE.md` first.

For substantial work:
- use `/qamc-discover` when discovery/architecture challenge is authorized;
- use `/qamc-build` only when `docs/STATE.md` and `docs/WORK.md` explicitly authorize implementation.

Load only what the task needs. Do **not** preload the whole repository or `docs/PROJECT_COMPASS.md`. Use Git history only for specific evidence needed by the current task.

Current machine-facing authority is intentionally small:
- `docs/OUTCOME.md` — desired result and hard product constraints;
- `docs/STATE.md` — accepted state and current authorization;
- `docs/WORK.md` — the one current work/handoff contract;
- relevant accepted contracts under `docs/architecture/`.

Any `docs/FUTURE_*` file is conceptual only and never implementation authorization.

## Hard boundaries

- **Current execution authorization:** Alpaca Paper only. Live-broker order submission is not authorized until a separate future authorization.
- **One trading architecture:** Paper vs live is a broker/runtime environment distinction, not a separate decision architecture. Specialist reasoning, Portfolio Manager intent, AI Risk review, deterministic risk/execution, position management, exits, journaling, telemetry and Dashboard semantics must not use paper-specific shortcuts or materially different logic. Genuine broker-environment differences (for example fill behavior, credentials or endpoints) belong at the broker/configuration boundary.
- `yebof/quant-agent` remains the authoritative trading engine.
- Specialist agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution remains the decision chain.
- Deterministic Python and broker protections own final safety/execution eligibility; uncertainty fails closed.
- Dashboard/API/journal/search/UI remain non-critical to trading and read-only unless a future accepted work contract explicitly says otherwise.
- No secrets or fake production trading state in API/UI/client artifacts.
- Derived UI/search/journal state is rebuildable and non-authoritative.
- Avoid unnecessary infrastructure and gratuitous trading-core rewrites; preserve upstream mergeability.

Path-scoped rules add detail only when relevant files are touched.

## Decision discipline

- Repository fact → investigate it.
- Routine engineering choice → decide it.
- Genuine operator product/value trade-off → ask one question at a time.
- Material conflict with accepted architecture/safety/scope → record it in `docs/WORK.md` for external reconciliation.

Do not turn the operator into the technical architect.

## Architectural authority boundary — HARD RULE

Claude has broad autonomy to implement, debug, test, refactor, and make routine engineering decisions **inside the currently accepted architecture and authorized work**.

Claude does **not** have authority to independently introduce, replace, or build significant architecture, infrastructure, services, platforms, frameworks, security systems, credential systems, databases, proxies, orchestration layers, or other durable components that are not already explicitly authorized.

If an approved component is unavailable, unsuitable, blocked, unexpectedly heavy, incompatible, or otherwise creates a fork in the architectural path: **STOP AND ASK.** Do not interpret a blocker as permission to build an alternative.

In particular, never respond to “the approved product/tool cannot be installed or does not work as expected” by building an in-house equivalent without explicit architectural approval.

Permission is required before any change that would:
- introduce a new persistent service or daemon;
- introduce a new infrastructure dependency;
- replace an approved external/open-source component;
- create a custom implementation of functionality that an existing product was intended to provide;
- materially increase long-term maintenance burden;
- create a new security-sensitive component;
- change account, network, or trust boundaries;
- materially alter the accepted architecture;
- consume substantial engineering time because the original approach encountered a roadblock.

At such a fork, report concisely:
1. what was discovered;
2. why the authorized path is blocked or unattractive;
3. the viable alternatives;
4. the recommended choice;
5. the engineering and maintenance consequences.

Then wait for architectural approval.

**Default rule: optimize autonomously within the architecture; never autonomously redesign the architecture.** When uncertain whether something is a routine implementation choice or an architectural decision, treat it as architectural and ask.

## Claude Code execution discipline

For substantial work with a measurable end state, prefer **outcome-driven execution** over chains of micro-prompts:

- Use `/goal <verifiable completion condition>` when the installed Claude Code version supports it. State the end state, hard boundaries, and a check Claude can actually run; do not prescribe every implementation step.
- Give Claude a verification loop before implementation. Prefer tests, health checks, builds, screenshots, or other deterministic pass/fail evidence. Completion means showing the evidence, not asserting that the work looks done.
- **Minimal-sufficient execution — HARD RULE:** for bounded, reversible, read-only, or low-risk tasks, once the required safety prerequisites are confirmed, execute the shortest decisive test immediately. Do not inspect adjacent docs, code, packaging, environment, or architecture unless the test fails or a genuine safety blocker appears. Prefer one direct experiment over chains of inferred checks. Stop as soon as the requested result is proven and no blocker remains; unnecessary re-validation is execution failure, not diligence.
- **Dashboard visual work has an additional acceptance gate:** builds/tests and absence of overlap are not sufficient. Inspect rendered desktop and iPad screenshots against `docs/OUTCOME.md` and the vision board. Typography hierarchy, proportion, spacing/grid rhythm, information density, component-to-container scale and sparse/empty-state composition are product correctness; obvious visual imbalance remains unfinished work.
- `/goal` controls persistence across turns; permission/autonomy modes control tool approvals. Treat them as separate concerns. Never weaken QAMC safety boundaries merely to remove approval friction.
- Use `/plan` or discovery mode when architecture or authorization is unresolved; once implementation is authorized, avoid repeatedly returning to planning for routine engineering choices.
- Protect the main context aggressively. Use subagents for file-heavy investigation and fresh-context/adversarial verification; use `/btw` for disposable side questions; use targeted `/compact` when continuity matters; use `/clear` between unrelated tasks or after repeated failed/corrected approaches.
- Use worktree isolation for parallel edits that could collide. Use `/batch` only for genuinely large, decomposable work; do not invoke parallelism for its own sake.
- Prefer concise `CLAUDE.md` rules that apply broadly. Put conditional domain knowledge and repeatable workflows in skills so they load on demand; use hooks for invariants that must happen every time rather than relying on advisory prose.
- Prefer CLI tools for external systems when available; they are usually more context-efficient than manually narrating or scraping equivalent state.
- Use background/session tools (`/background`/`/bg`, tasks, resume/continue) when they improve continuity without weakening reviewability.
- Do not assume Claude Code command availability or semantics from memory. The CLI evolves; before relying on a command-specific workflow, verify the installed version/help or current official Claude Code documentation.
- Stop for the operator only when required by credentials, unavailable privilege, an explicit product/value decision, or a material architecture/safety conflict. Bundle operator-only actions into one concise intervention whenever safe.

The governing principle is: **state the desired verified outcome and hard boundaries; give Claude enough autonomy to choose the engineering path, while keeping context clean and verification objective.**

## Git and continuity

Git is durable QAMC memory. Project auto-memory is disabled. A new Claude session rehydrates from the repository, not from old chat transcripts. A usage reset is not a context reset.

Use dedicated branches. Never force-push, push implementation directly to `main`, or merge a PR from Claude Code. Discovery/implementation handoffs must be committed and pushed before external review.

Treat a blocked or rejected GitHub write as a **safety signal**, not permission to improvise around it. Before choosing any alternate Git/GitHub operation, verify the current branch/ref, expected base and head SHA, PR state, relevant history, and the protection or API reason for the rejection. Then use only a safe forward operation consistent with that verified state; never bypass protection with force updates or speculative alternate writes.

Do not create new handoff/status/governance documents. `STATE.md` owns current authorization, `WORK.md` owns the current handoff, and `PROJECT_COMPASS.md` is a human-only projection.