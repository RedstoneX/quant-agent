# QAMC — Claude Code Contract

QAMC is a private autonomous AI-assisted Alpaca trading system built around `yebof/quant-agent`. Its currently authorized execution environment is Alpaca Paper.

**Terminology:** **QAMC / Mission Control** means the whole product/system. **Dashboard** means the browser/iPad read-side UI. **Core recovery** means the trading/backend deployment-and-validation workstream.

## Start

Read `docs/STATE.md` first, then `docs/WORK.md` for the active authorization. Load only what the task needs. Do not preload the whole repository or `docs/PROJECT_COMPASS.md`.

Current machine-facing authority is intentionally small:
- `docs/OUTCOME.md` — desired result and hard product constraints;
- `docs/STATE.md` — accepted state;
- `docs/WORK.md` — current work/handoff contract;
- relevant accepted contracts under `docs/architecture/`.

Any `docs/FUTURE_*` file is conceptual only and never implementation authorization.

## Hard boundaries

- **Current execution authorization:** Alpaca Paper only. Live-broker order submission is not authorized.
- One trading architecture: Paper vs live is a broker/runtime boundary, not a separate decision architecture.
- Specialist agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution remains the decision chain.
- Deterministic Python and broker protections own final safety/execution eligibility; uncertainty fails closed.
- Dashboard/API/journal/search/UI remain non-critical to trading and read-only unless a future accepted work contract explicitly says otherwise.
- No secrets or fake production trading state in API/UI/client artifacts.
- Avoid unnecessary infrastructure and gratuitous trading-core rewrites; preserve upstream mergeability.

## Stabilization account model — HARD RULE

During the current stabilization phase, use **two active accounts**:

- **`ubuntu` = engineering/operator account.** Claude/Codex development, Git/GitHub, package/tool installation, private DEV preview, browser automation, tests, Docker/sudo tasks, and deployment preparation run from `ubuntu`.
- **`qamc` = production runtime account only.** It owns the production checkout, runtime `.env`/OneCLI wiring, user services/timers, and QAMC execution.
- **`dev` = parked.** Do not use it in the normal workflow and do not add permissions to it. Keep it available for possible later reintroduction after stabilization.

The important isolation boundary is **engineering/operator (`ubuntu`) vs runtime (`qamc`)**. Do not run Claude/Codex as `qamc`, move development credentials into `qamc`, or make `qamc` a general engineering account.

`ubuntu` has privilege, but that privilege is not standing production authorization. Before the production gate opens, Claude must not use sudo or `sudo -u qamc` to mutate `/home/qamc/quant-agent`, `qamc` services/timers, runtime credentials, or production configuration.

## Environment promotion boundary — HARD RULE

**Engineering autonomy and production authority are separate.**

Inside an already-authorized task, Claude may autonomously from `ubuntu`:
- diagnose, implement and refactor in an `ubuntu`-owned engineering checkout/worktree outside `/home/qamc`;
- install or repair normal development tooling when needed;
- run targeted tests, builds, private Vite previews and browser verification;
- use existing private Tailscale development access;
- commit and push a dedicated branch/PR.

The default rule is then **STOP at the external gate**.

Without explicit operator authorization for the exact promotion step, Claude must not:
- merge its own implementation PR or otherwise advance its implementation into `main`;
- change `/home/qamc/quant-agent`;
- restart or alter a `qamc` production service/timer;
- modify runtime credentials/configuration;
- run a production deployment/convergence entrypoint;
- treat a production browser check as permission to deploy first.

A bounded `docs/WORK.md` contract may itself contain explicit operator authorization for merge and/or production deployment of that exact tranche. When it does, that authorization is valid and no second prompt is required merely to repeat the same gate. It does **not** become standing permission for unrelated work.

Merge approval and production-deployment approval are otherwise distinct gates. Generic instructions such as “proceed”, “continue”, “fix it”, “make it happen”, or “finish this” never escalate environments by inference.

After explicit production approval, `ubuntu` performs the shortest governed privileged deploy/verify operation against the existing `qamc` runtime. Do not make the operator manually shuttle between accounts when `ubuntu` can perform the approved `sudo -u qamc` steps directly.

## Decision discipline

- Repository fact → investigate it.
- Routine engineering choice → decide it.
- Genuine operator product/value trade-off → ask one question at a time.
- Material architecture/safety/scope conflict → stop and surface it.

Do not turn the operator into the technical architect.

## Architectural authority boundary — HARD RULE

Claude has broad autonomy to implement, debug, test, refactor, install ordinary development tooling, and make routine engineering decisions **inside the accepted architecture and authorized work**.

Claude does **not** have authority to independently introduce or replace significant architecture, persistent services, infrastructure platforms, credential/security systems, databases, proxies, orchestration layers, or other durable components.

If the accepted path is blocked or unexpectedly heavy, stop and report the blocker and viable alternatives. Do not build an in-house substitute merely because the approved component is inconvenient.

## Execution discipline

For substantial work with a measurable end state, prefer outcome-driven execution over chains of micro-prompts.

- **Minimal-sufficient execution — HARD RULE:** once safety prerequisites are known, execute the shortest decisive test. Do not inspect adjacent docs, packaging, environment, or architecture unless the test fails or a genuine blocker appears.
- For bounded fixes, run the narrowest sufficient test/build/preview first. Do not default to the entire repository suite, multi-agent fan-out, multiple worktrees, or broad commissioning.
- Do not repeatedly re-read unchanged authority files or re-prove facts already established in the same task unless relevant state changed.
- **Dashboard visual work:** tests/builds are not enough. Inspect rendered desktop and iPad views against `docs/OUTCOME.md` and the vision board.
- Use subagents/parallelism only when they materially reduce wall-clock time or isolate genuinely separate work.
- Prefer CLI tools for external systems when available.
- Stop as soon as the requested result is proven and no blocker remains; unnecessary re-validation is execution failure, not diligence.
- At a normal engineering completion gate, report only: **changed / verified / preview (if relevant) / unresolved blocker / exact next gate**.

The governing principle is: **fast autonomous engineering under `ubuntu`; isolated runtime under `qamc`; promotion only when the exact work contract authorizes it.**

## Git and continuity

Git is durable QAMC memory. Use dedicated branches. Never force-push or push implementation directly to `main`. Implementation normally reaches `main` through a PR. Codex may merge its own dedicated PR only when the current `docs/WORK.md` explicitly authorizes that exact tranche through merge; otherwise it stops at the external gate.

Treat a blocked GitHub write as a safety signal, not permission to improvise around it. Verify the relevant ref/PR state before choosing a safe forward operation.

Do not create new handoff/status/governance documents. `STATE.md` owns current accepted state, `WORK.md` owns the current handoff, and `PROJECT_COMPASS.md` is a human-only projection.
