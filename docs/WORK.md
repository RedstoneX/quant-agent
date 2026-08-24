# QAMC Current Work

Status: **CORE RECOVERY IN NATURAL PAPER VALIDATION | PRODUCTION CONVERGENCE COMPLETE**

## Current integration truth

- Actual VPS production is verified at `2b3faaf69c0b842a08f991a9ca517a3989bdaf93` (PR #63 merge).
- Production contains the accepted runtime payload through PR #56, enriched Telegram output, PR #60 Mission Control/data-correctness work, and PR #63 session-execution/intraday-chart work.
- Production has one intended tracked local delta: `config/settings.yaml` with `intraday_scan.enabled: true`.
- `ubuntu` / `qamc` / `dev` isolation remains intact.
- All seven expected qamc timers remain enabled.
- OneCLI remains private and the production API remains private/read-only.
- Alpaca Paper remains the only authorized execution environment.
- A 2026-08-24 preflight showed no runtime/code/config delta between production and the then-current `main`; only `docs/STATE.md` and `docs/WORK.md` differed. No application deployment was required.
- A 2026-08-24 production acceptance pass completed with 0 failures.

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

### 1. Continue natural trading validation

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

### 2. Use Mission Control and Telegram as validation evidence

Mission Control and Telegram are now production-converged observational surfaces. Use them to inspect and explain natural sessions rather than reopening resolved UI/deployment work without new evidence.

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

### 3. Address lower-priority issues only if they distort validation

Two known lower-priority issues remain outside the current gate:

- news-narrative factual drift;
- `actual_provider` attribution oddity.

Do not interrupt natural trading validation for these unless evidence shows they materially damage decision quality, truthfulness or operator understanding.

## Mission Control acceptance now present in production

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
- `5m Today`, `15m`, `1h`, `1D` read-only chart timeframes with timestamp-preserving intraday OHLCV;
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
- no runtime/code/config deployment delta remained.

Final production acceptance on 2026-08-24 completed with **0 failures**.

## Remaining uncertainty

The trading recovery is strongly supported by production forensics, deterministic tests and a converged read-side surface, but the complete opportunity→decision→execution→management chain still needs natural market validation.

That is now the primary gate.

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
