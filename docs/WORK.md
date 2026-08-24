# QAMC Current Work

Status: **DASHBOARD TIMEFRAME REGRESSION AUTHORIZED | PRIVATE DEV HOT-RELOAD AUTHORIZED | CORE RECOVERY IN NATURAL PAPER VALIDATION**

## Current integration truth

- Actual VPS production is verified at `2b3faaf69c0b842a08f991a9ca517a3989bdaf93` (PR #63 merge).
- Production contains the accepted runtime payload through PR #56, enriched Telegram output, PR #60 Mission Control/data-correctness work, and PR #63 session-execution/intraday-chart work.
- Production has one intended tracked local delta: `config/settings.yaml` with `intraday_scan.enabled: true`.
- `ubuntu` / `qamc` / `dev` isolation remains intact.
- All seven expected qamc timers remain enabled.
- OneCLI remains private and the production API remains private/read-only.
- Alpaca Paper remains the only authorized execution environment.
- A 2026-08-24 production acceptance pass completed with 0 failures for the tested API/read-only surface.
- Current frontend source already defines chart controls for `5m Today`, `15m`, `1h`, and `1D`, and production API calls for `5m`, `15m`, `1h`, `1d` all passed.
- **New operator evidence:** the actual production dashboard currently exposes only the day / `1D` chart view. Minute/hour controls are not visible or usable. This is the current bounded dashboard defect.

## Product / architecture principle

QAMC is one autonomous Alpaca trading system. Alpaca Paper is the currently authorized execution environment, not a separate trading architecture.

Trading-critical path remains:

**discovery → Specialists → Portfolio Manager → AI Risk Manager → deterministic gate → funding → broker execution → position/exit management → reflection**.

Mission Control, Journal, search and Telegram output are observational/read-side surfaces. They must not become authoritative trading state or broker-write control paths.

## Goal

Use natural market evidence to determine whether QAMC reliably:

**finds opportunity → evaluates it → makes a defensible bullish, bearish or neutral decision → executes when eligible → manages/exits the position → measures the result.**

Success is not “more trades.” Do not force activity, weaken safety or hindsight-tune.

When QAMC does not trade, the reason must be specific and defensible.

## Next authorized work

### 0. Standing private DEV visual-review workflow

The operator explicitly authorizes a **temporary, development-only Vite hot-reload server** for dashboard work so visual changes can be reviewed immediately while Claude works.

This is the preferred dashboard development loop when practical:

**`dev` checkout/worktree → Vite hot reload → private Tailscale browser view → visual iteration → branch/PR review → separate governed production rollout.**

Boundaries:

- run only as `dev`, from the development checkout or its task worktree;
- use the repository's existing React/Vite frontend and existing Vite API proxy to the read-only Mission Control API on `127.0.0.1:8800`;
- bind the Vite server only to the VPS Tailscale interface/address, never `0.0.0.0`, the public VPS interface, or another public listener;
- session/development process only: **no systemd unit, daemon, boot persistence, new proxy, new service architecture, or production dependency**;
- do not copy production credentials into the frontend or expose OneCLI;
- do not add broker-write controls or routes; the existing Mission Control API remains read-only;
- production under `qamc` remains untouched while DEV work is being reviewed;
- Claude may start/restart/stop this temporary DEV preview autonomously during an authorized dashboard task and report the private Tailscale URL to the operator;
- `branch_preview.py` remains available for deterministic built-artifact verification, but is **not required instead of hot reload** during normal visual iteration. Use it when a static-build acceptance check is useful.

This authorization is specifically intended to remove repeated operator friction. Claude does not need to ask again whether it may run the temporary private Vite preview while working on an already-authorized dashboard task, provided these boundaries are preserved.

### 1. Fix the production chart-timeframe regression

This is an **implementation-authorized bounded dashboard defect** under `/qamc-build`.

Desired verified outcome:

- the actual production chart visibly offers `5m Today`, `15m`, `1h`, and `1D`;
- each control is usable and actually requests/renders its corresponding timeframe;
- switching timeframes does not break live-price / previous-close truthfulness, BUY/SELL markers, selected symbol/session context, responsive layout, or read-only behavior;
- desktop and iPad/tablet rendered verification proves the controls are visible and usable in the real UI, not merely present in source or passing unit tests.

Execution guidance:

- begin from current `main` and current repository authority;
- use the authorized private Vite hot-reload DEV workflow above for rapid visual diagnosis/iteration;
- investigate why production exposes only `1D` despite accepted source and working API support;
- distinguish source-code correctness from built/static asset, serving, CSS/layout, caching, responsive, or deployment-state problems using evidence rather than assumption;
- make the **smallest justified fix**;
- use the existing Vite/React/Tremor/Lightweight-Charts stack and existing API; do not introduce a new chart library, frontend framework, service, proxy, or backend architecture;
- preserve Mission Control as private/read-only and non-critical to trading;
- do not alter trading/risk/execution semantics, timers, credentials, account boundaries, OneCLI, or the governed `intraday_scan.enabled: true` production override;
- run the relevant frontend tests/build and any targeted backend/API checks needed to prove no contract regression;
- perform rendered desktop and iPad/tablet verification per the frontend verification rules;
- use routine engineering autonomy, helpers/worktrees if useful, and internal checkpoints without asking the operator for implementation choices;
- at the external gate, push the implementation branch with evidence and STOP for ChatGPT/operator review. Do not merge the implementation PR from Claude Code.

Do not expand this defect into a broad dashboard redesign. Other dashboard problems require separate evidence/authorization unless they are inseparable from this fix.

### 2. Continue natural trading validation

Observe normal Alpaca Paper sessions without manufacturing trades.

Natural sessions still need to demonstrate:

- worthwhile opportunities survive discovery and reach PM/Risk;
- eligible trades can reach funded broker submission;
- supported bearish opportunities can be expressed through approved inverse ETFs;
- no-trade decisions remain possible and explainable;
- positions and exits behave coherently after entry;
- funding/execution failures are visible rather than mistaken for investment judgment;
- outcomes and missed opportunities can be measured without hindsight tuning.

The required end-to-end evidence chain is:

**opportunity discovered → evaluated → defensible bullish/bearish/neutral decision → executed when eligible → managed/exited → measured**.

### 3. Use Mission Control and Telegram as validation evidence

Except for the explicitly authorized chart-timeframe regression above, treat Mission Control and Telegram as production-converged observational surfaces. Use them to inspect and explain natural sessions rather than reopening resolved UI work without new evidence.

Production acceptance verified:

- `/health` healthy with `paper=true` and broker reachable;
- `/cockpit` final HTTP 200;
- `/ui` final HTTP 200;
- `/quotes?symbols=SPY` works;
- `/prices/SPY` works for `5m`, `15m`, `1h`, `1d`;
- session execution read path works;
- POST / PUT / PATCH / DELETE are rejected;
- Telegram production credentials are present, unmuted and notifier-enabled (`--dry-run`; no closeout test message sent);
- OneCLI remains private;
- expected timers and account boundaries remain intact.

Use these surfaces to answer, from real evidence:

- what opportunity was found;
- which agents evaluated it;
- what PM/Risk decided;
- what deterministic gate/funding/execution did;
- why a trade did or did not happen;
- what happened after entry;
- how the result compared with missed opportunities.

### 4. Address lower-priority issues only if they distort validation

Two known lower-priority issues remain outside the current gate:

- news-narrative factual drift;
- `actual_provider` attribution oddity.

Do not interrupt natural trading validation for these unless evidence shows they materially damage decision quality, truthfulness or operator understanding.

## Mission Control acceptance baseline

Accepted behavior includes:

- professional ordinary UI primitives via Tremor/TanStack;
- Lightweight Charts for market-price visualization and trade markers;
- Dockview for the desktop support workspace;
- custom visualization only where QAMC-specific decision topology justifies it;
- separate current-session quote vs historical bar semantics;
- pinned session/run context that cannot be silently overwritten by stale polls;
- truthful `AUTO / PRIMARY` behavior;
- candidate-correct PM/Risk attribution;
- persisted execution-skip explanations and explicit reason-not-recorded states;
- Journal decision ledger and exact-run navigation;
- session-specific execution rows with click-to-chart behavior;
- accepted `5m Today`, `15m`, `1h`, `1D` read-only chart timeframes with timestamp-preserving intraday OHLCV, subject to the currently authorized production-visibility fix;
- stale/degraded read-side data identified rather than silently presented as current;
- no ArcGauge, handmade RunTimeline or old ECharts candidate funnel.

Do not reopen those resolved product decisions without new evidence.

## Verification already recorded

PR #63 pre-merge verification reported:

- backend: **2,030 passed**;
- frontend: **55 passed**;
- production frontend build: passed;
- desktop/iPad browser verification completed with zero console/page errors against the branch verification setup.

Production preflight on 2026-08-24 additionally verified:

- actual production SHA `2b3faaf69c0b842a08f991a9ca517a3989bdaf93`;
- tracked production files clean except the governed intraday override;
- `intraday_scan.enabled: true`;
- seven qamc timers enabled;
- `quant-agent-api.service` active;
- `/health` healthy, DB and broker reachable, `paper=true`;
- OneCLI private listeners present;
- no runtime/code/config deployment delta remained at that checkpoint.

Final production API/read-only acceptance on 2026-08-24 completed with **0 failures**. The later operator report that only `1D` is visible is new browser-level evidence and is not contradicted by those API checks.

## Remaining uncertainty

The trading recovery is strongly supported by production forensics, deterministic tests and a converged read-side API surface, but the complete opportunity→decision→execution→management chain still needs natural market validation.

Separately, the chart timeframe controls must now be proven visible and functional in the actual production browser before that UI capability is considered production-verified.

## Hard boundaries

- **Current execution authorization is Alpaca Paper only.**
- No margin, options or direct stock shorting; bearish expression remains through approved inverse ETFs.
- Deterministic Python/broker protections remain final safety authority.
- Do not force/manufacture trades or weaken safeguards to increase activity.
- Do not create paper-only trading semantics.
- No new daemon/service/database/proxy/security/credential architecture without explicit approval.
- Preserve `dev` / `qamc` / `ubuntu` isolation and OneCLI secret handling.
- Mission Control remains private/read-only; Telegram remains output-only.
- No public exposure of QAMC or OneCLI.
