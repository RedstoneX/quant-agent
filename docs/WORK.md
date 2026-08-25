# QAMC Current Work

Status: **DEV AUTONOMY | OPERATOR MERGE/PRODUCTION GATES | CORE RECOVERY IN NATURAL PAPER VALIDATION**

## Current integration truth

- Production has been reported and verified at `a6758f935910c5cf380cc6a7acedc5f3b78f6366` after PR #69 deployment.
- PR #69 fixed the intraday chart data path by explicitly requesting Alpaca IEX for 5m/15m/1h bars. Production verification reported non-empty SPY/AAPL bars and working `5m Today`, `15m`, `1h`, and `1D` chart controls.
- Production remains Alpaca Paper. The seven existing timers remained intact, Mission Control remained private/read-only, and the governed local production override `config/settings.yaml: intraday_scan.enabled: true` was preserved.
- GitHub `main` may move ahead with documentation or later accepted work. **Production does not automatically follow `main`.**
- The previous “standing fast-lane” wording incorrectly allowed production promotion to be inferred after merge. That authorization is revoked by the operator-promotion rule below.
- PR #69 did not change `PriceChartPanel`; therefore the previously operator-observed live-price versus chart-right-edge mismatch is **not considered resolved merely because the timeframe/IEX fix is live**. Treat that as a separate DEV issue if still reproducible.

## Product / architecture principle

QAMC is one autonomous Alpaca trading system. Alpaca Paper is the currently authorized execution environment, not a separate trading architecture.

Trading-critical path remains:

**discovery → Specialists → Portfolio Manager → AI Risk Manager → deterministic gate → funding → broker execution → position/exit management → reflection**.

Mission Control, Journal, search and Telegram are observational/read-side surfaces and must not become authoritative trading state or broker-write control paths.

## Standing delivery workflow — HARD RULE

### DEV is autonomous

For already-authorized work, Claude/Codex may autonomously:

- work as `dev` in the development checkout/worktree;
- diagnose, implement and refactor inside accepted architecture;
- run the shortest sufficient tests/builds;
- start/stop the existing private Tailscale Vite DEV preview;
- use browser automation/screenshots against DEV;
- commit and push a dedicated branch/PR.

No separate operator approval is needed for those DEV actions.

### External review/merge gate

After the branch/PR is pushed, **STOP**.

- ChatGPT/operator reviews the actual diff and verification evidence.
- Claude does not merge its own implementation PR.
- Merge requires explicit operator authorization for that merge, unless the operator has explicitly delegated that exact merge to ChatGPT in the current task.

### Production gate

After merge, **STOP again**. Production remains untouched until the operator explicitly authorizes production deployment.

- Approval to merge is not approval to deploy.
- “Proceed”, “continue”, “fix it”, “finish this”, a green build, or a merged PR does not imply production authorization.
- A single instruction may authorize merge + production only if it clearly says both.
- Production deployment/verification must use `ubuntu` operating on the existing `qamc` production checkout and account boundaries.
- Browser verification against production occurs only after that production gate has been explicitly opened.

## Friction-reduction rules

The goal is **less operator handling without weakening the production gate**:

1. **No account shuttling during development.** Normal implementation stays under `dev`; do not ask the operator to bounce between `dev`, `qamc`, and `ubuntu` for information that can wait until the one privileged production step.
2. **One privileged production intervention.** After explicit production approval, bundle production preflight/deploy/restart/acceptance into the shortest safe `ubuntu` operation instead of repeated sudo snippets.
3. **Private DEV preview is standing-authorized.** Do not repeatedly ask whether Vite/browser verification may run in DEV.
4. **No broad archaeology for bounded fixes.** Read only `STATE.md`, `WORK.md`, the relevant contract/code, then execute the decisive test. Do not re-read the repository or repeat commissioning unless evidence requires it.
5. **Targeted verification first.** For a bounded dashboard/read-side fix, run the relevant tests/build/preview. Do not default to the entire repository suite unless the change surface or accepted contract justifies it.
6. **No gratuitous parallelism.** Do not spawn multiple agents/worktrees for a small fix merely to “double check” it. Parallelize only when it materially reduces wall-clock time.
7. **Stop on proof.** Once the requested DEV result is demonstrated and no blocker remains, stop and hand off. Do not keep re-validating the same fact.
8. **Concise handoff.** Report only: changed / verified / preview / unresolved blocker / exact next gate.
9. **No new security friction during this phase.** Preserve the existing `dev` / `qamc` / `ubuntu` separation and OneCLI boundaries, but do not invent additional permission layers, daemons, credential systems, proxies, or lockdown steps without separate operator approval.
10. **Do not conflate adjacent defects.** A task is not complete merely because a related fix deployed. Each operator-observed defect needs evidence against its own acceptance condition.

## Current authorized next work

### 1. Finish this governance repair

This branch should restore the operator-controlled merge/production gates while keeping the useful DEV-autonomy and low-friction rules above.

### 2. Live-price/chart-right-edge mismatch — DEV only until approved

If the mismatch remains reproducible:

- investigate and fix it only in DEV;
- preserve the separate semantics of historical bars, current-session quote, previous close, and live price;
- use the existing Lightweight Charts/Tremor stack;
- verify visually in the private DEV browser;
- push the branch and stop at the external review gate.

Do **not** deploy that fix automatically.

### 3. Continue natural trading validation

Observe normal Alpaca Paper sessions without manufacturing trades. The required natural evidence chain remains:

**opportunity discovered → evaluated → defensible bullish/bearish/neutral decision → executed when eligible → managed/exited → measured**.

Success is not “more trades.” When QAMC does not trade, the reason must be specific and defensible.

## Flagged separately — not bundled into dashboard workflow work

`src/execution/broker.py::get_latest_price` builds latest-trade/latest-quote requests without an explicit Alpaca feed and silently degrades to `None` on failure. Because that path is trading-critical, it requires separate operator/ChatGPT authorization and production investigation before code changes.

Lower-priority known issues remain news-narrative factual drift and `actual_provider` attribution oddity; do not interrupt the current task for them unless evidence shows they materially distort validation.

## Hard boundaries

- **Current execution authorization is Alpaca Paper only.**
- No margin, options or direct stock shorting; bearish expression remains through approved inverse ETFs.
- Deterministic Python/broker protections remain final safety authority.
- Do not force/manufacture trades or weaken safeguards to increase activity.
- Do not create paper-only trading semantics.
- No new daemon/service/database/proxy/security/credential/orchestration architecture without separate explicit approval.
- Preserve `dev` / `qamc` / `ubuntu` isolation and OneCLI secret handling.
- Mission Control remains private/read-only; Telegram remains output-only.
- No public exposure of QAMC or OneCLI.
