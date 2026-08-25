# QAMC Current Work

Status: **UBUNTU ENGINEERING AUTONOMY | QAMC RUNTIME ISOLATION | OPERATOR MERGE/PRODUCTION GATES | CORE RECOVERY IN NATURAL PAPER VALIDATION**

## Current integration truth

- Production has been reported and verified at `a6758f935910c5cf380cc6a7acedc5f3b78f6366` after PR #69 deployment.
- PR #69 fixed the intraday chart data path by explicitly requesting Alpaca IEX for 5m/15m/1h bars. Production verification reported non-empty SPY/AAPL bars and working `5m Today`, `15m`, `1h`, and `1D` chart controls.
- The chart live-price/current-price truth issue was already fixed earlier by commit `796558f184f8dd800c7e1cbb57f11173ad3d6f6b` (`fix(qamc): show session fills and live chart price`, 2026-08-21). Current `PriceChartPanel` keeps live quote data separate from historical bars, hides the historical series' default last-value line, and renders explicit `LIVE` / `PREV CLOSE` lines. It is not an outstanding task.
- Production remains Alpaca Paper. The seven existing timers remained intact, Mission Control remained private/read-only, and `config/settings.yaml: intraday_scan.enabled: true` was preserved.
- GitHub `main` may move ahead with documentation or later accepted work. **Production does not automatically follow `main`.**

## Stabilization account model — HARD RULE

Use two active accounts until QAMC is stable:

### `ubuntu` — engineering/operator

Use `ubuntu` for:

- Claude/Codex sessions;
- engineering checkout/worktrees outside `/home/qamc`;
- Git/GitHub;
- package and development-tool installation;
- targeted tests/builds;
- private Tailscale Vite preview and browser automation;
- Docker/sudo work required for engineering or an explicitly approved production operation;
- deployment orchestration after the production gate opens.

### `qamc` — runtime only

`qamc` owns:

- `/home/qamc/quant-agent` production checkout;
- runtime `.env` and OneCLI credential wiring;
- `qamc` user services/timers;
- QAMC paper execution.

Do not run Claude/Codex as `qamc` and do not turn it into a general development account.

### `dev` — parked

Do not use `dev` in the normal workflow. Do not grant it sudo, Docker or broader runtime access. Keep it intact for possible later reintroduction after stabilization.

The purpose is to eliminate hours of artificial account friction without collapsing the engineering/runtime boundary.

## Standing delivery workflow — HARD RULE

### Engineering is autonomous

For already-authorized work, Claude/Codex may autonomously from `ubuntu`:

- diagnose, implement and refactor inside accepted architecture;
- create/use an `ubuntu`-owned engineering checkout/worktree;
- install ordinary development tooling when required;
- run the shortest sufficient tests/builds;
- start/stop the existing private Tailscale Vite preview;
- use browser automation/screenshots;
- commit and push a dedicated branch/PR.

No separate operator approval is needed for those engineering actions.

### External review/merge gate

After the implementation branch/PR is pushed, **STOP**.

- ChatGPT/operator reviews the actual diff and verification evidence.
- Claude does not merge its own implementation PR.
- Merge requires explicit operator authorization unless the operator explicitly delegated that exact merge to ChatGPT in the current task.

### Production gate

After merge, **STOP again**. Production remains untouched until the operator explicitly authorizes production deployment.

- Approval to merge is not approval to deploy.
- “Proceed”, “continue”, “fix it”, “finish this”, green tests, or a merged PR do not imply production authorization.
- A single instruction may authorize merge + production only if it clearly says both.
- Before this gate opens, `ubuntu` privilege must not be used to modify `/home/qamc/quant-agent`, `qamc` services/timers, runtime credentials, or production configuration.
- After approval, `ubuntu` performs the shortest safe `sudo -u qamc` deploy/verify path directly.
- Production browser verification occurs after the production gate opens and deployment occurs.

## Friction-reduction rules

1. **No normal use of `dev`.** Do not make the operator or agents bounce through a deliberately restricted account.
2. **No manual account ping-pong.** `ubuntu` orchestrates approved privileged work; `qamc` remains the runtime identity.
3. **Private DEV preview is standing-authorized.** Do not repeatedly ask whether Vite/browser verification may run.
4. **No broad archaeology for bounded fixes.** Read only the current state/work contract and relevant code, then execute the decisive test.
5. **Targeted verification first.** Do not default to the entire repository suite unless the change surface actually warrants it.
6. **No gratuitous parallelism.** Small fixes do not need several agents/worktrees rediscovering the same facts.
7. **Stop on proof.** Once the requested engineering result is demonstrated and no blocker remains, stop and hand off.
8. **Concise handoff.** Report only: changed / verified / preview / unresolved blocker / exact next gate.
9. **No new lockdown while stabilizing.** Preserve `ubuntu` vs `qamc` runtime separation and OneCLI boundaries, but do not add new permission systems or security infrastructure without explicit approval.
10. **Do not conflate adjacent defects.** Each operator-observed problem must pass its own acceptance condition.
11. **One production intervention.** After production approval, bundle preflight/deploy/restart/acceptance into the shortest safe `ubuntu` operation instead of a chain of sudo snippets.

## Current authorized next work

### 1. Establish/use the `ubuntu` engineering checkout when the next engineering task begins

Do not migrate production or copy runtime secrets. Create/use a normal `ubuntu`-owned development checkout outside `/home/qamc` and install only the development tooling required by the task. `dev` is no longer a prerequisite.

### 2. Continue natural trading validation

Observe normal Alpaca Paper sessions without manufacturing trades. The required natural evidence chain remains:

**opportunity discovered → evaluated → defensible bullish/bearish/neutral decision → executed when eligible → managed/exited → measured**.

Success is not “more trades.” When QAMC does not trade, the reason must be specific and defensible.

### 3. Trading-critical `get_latest_price` follow-up — separate authorization required

`src/execution/broker.py::get_latest_price` builds latest-trade/latest-quote requests without an explicit Alpaca feed and silently degrades to `None` on failure. Because that path is trading-critical, investigate it only after separate operator/ChatGPT authorization; do not bundle it into dashboard or workflow work.

Lower-priority known issues remain news-narrative factual drift and `actual_provider` attribution oddity; do not interrupt the current task for them unless evidence shows they materially distort validation.

## Hard boundaries

- **Current execution authorization is Alpaca Paper only.**
- No margin, options or direct stock shorting; bearish expression remains through approved inverse ETFs.
- Deterministic Python/broker protections remain final safety authority.
- Do not force/manufacture trades or weaken safeguards to increase activity.
- Do not create paper-only trading semantics.
- No new daemon/service/database/proxy/security/credential/orchestration architecture without separate explicit approval.
- Keep `qamc` runtime-only and preserve OneCLI secret handling.
- Do not expand `dev` privileges during stabilization.
- Mission Control remains private/read-only; Telegram remains output-only.
- No public exposure of QAMC or OneCLI.
