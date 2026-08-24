# QAMC Current Work

Status: **CORE RECOVERY IN NATURAL PAPER VALIDATION | READ-SIDE + TELEGRAM PRODUCTION CONVERGENCE PENDING**

## Current integration truth

- The last governed production record pins QAMC at `d14e28dfc63ca6e4da920229b0ab5ba0f33b93df` with one intended local delta: `intraday_scan.enabled: true`.
- Deployment preflight must verify the actual VPS checkout, working tree, services and timers before relying on that recorded pin.
- `main` includes the already-accepted:
  - `e113f5c` — enriched Telegram trader decision feed;
  - PR #60 — integrated professional Mission Control cockpit plus data truth / run-history / explainability, code-merged at `c0edf5f24ee5771d37587aa9188f28857c6fee57`;
  - PR #63 — session-specific executions, BUY/SELL chart markers, live-price distinction and read-only `5m Today` / `15m` / `1h` / `1D` chart data, merged at `2b3faaf69c0b842a08f991a9ca517a3989bdaf93`;
  - subsequent intentional governance/documentation reconciliation only unless current Git history proves otherwise at deployment time.
- PR #59 is superseded by PR #60 and must not be deployed separately.
- The Telegram / PR #60 / PR #63 read-side improvements are merged but **not yet recorded as deployed to production**.

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

### 1. Production preflight and exact target resolution

Before any production cutover:

- verify the actual VPS production SHA, working tree and intended local changes;
- verify the existing `qamc` user services/timers and current healthy baseline;
- verify Alpaca Paper, OneCLI and account-isolation boundaries remain intact;
- read current `main` and record the exact candidate target SHA;
- inspect the complete delta from actual production to that candidate target.

Do not hard-code a self-referential “current main” SHA into governance docs because documentation merges themselves advance `main`.

The deployment target must contain the already-accepted:

- Telegram enrichment (`e113f5c`);
- PR #60 Mission Control/data-correctness integration (`c0edf5f` code merge);
- PR #63 session-execution/intraday-chart integration (`2b3faaf` merge);
- only intentional later changes confirmed by current Git history.

If current `main` contains a later material code change not represented here, reconcile it before deployment rather than assuming this file is exhaustive.

### 2. Governed production convergence

Deploy the exact reviewed target through the existing governed rollout path.

Preserve the authorized local production override:

`intraday_scan.enabled: true`

Preserve the existing `qamc` user timers unless current repository authority explicitly records an accepted change.

Confirm no trading-critical semantics, account boundaries or credential architecture changed beyond already accepted code.

### 3. Post-deploy acceptance

After deployment, verify at minimum:

- production pin equals the exact reviewed target SHA;
- production working tree contains only intentional governed local configuration;
- `/health` is healthy and reports `paper=true`;
- `/cockpit` and `/ui` return 200;
- `/quotes` is available and read-only;
- `/prices/{symbol}` supports the accepted `5m` / `15m` / `1h` / `1d` read-only timeframes;
- session-specific execution rows load against real production read-side data;
- BUY/SELL execution markers align with intraday chart timestamps;
- Mission Control rejects POST/PUT/PATCH/DELETE writes;
- account/position/price views remain truthful and do not present historical daily close as current quote;
- live price and previous close remain explicitly distinct;
- Today Sessions / `AUTO / PRIMARY` behavior loads against real production read-side data;
- selected/pinned historical run context cannot be silently replaced by later polling;
- Candidate / Chart / Decision Room remain synchronized to the selected run;
- persisted execution-skip reason/detail survive backend → API → frontend;
- Journal decision ledger/history remains discoverable and exact-run navigation works;
- stale/degraded read-side data is explicitly identified rather than silently shown as current;
- Telegram output still sends through the accepted OneCLI path;
- existing timers remain active/correct;
- `dev` / `qamc` / `ubuntu` account boundaries remain intact;
- no secret material appears in logs or repository state;
- no trading-critical regression is evident.

Where private operator/browser access is available, perform a real desktop and iPad/tablet visual pass against production. Do not expose the preview or production publicly merely to enable browser testing.

### 4. Governance closeout

After successful production convergence:

- update `docs/STATE.md` with the actual deployed SHA and verified production state;
- update `docs/WORK.md` to remove completed convergence work and leave only genuine remaining work;
- use the normal branch/PR workflow for documentation changes;
- do not create additional status/handoff documents.

### 5. Continue natural trading validation

Do not stop or manufacture the Alpaca Paper soak for the dashboard deployment.

Natural sessions still need to demonstrate:

- worthwhile opportunities survive discovery and reach PM/Risk;
- eligible trades can reach funded broker submission;
- supported bearish opportunities can be expressed through approved inverse ETFs;
- no-trade decisions remain possible and explainable;
- positions and exits behave coherently after entry;
- funding/execution failures are visible rather than mistaken for investment judgment;
- outcomes and missed opportunities can be measured without hindsight tuning.

## Mission Control acceptance carried by `main`

The merged PR #60 establishes the accepted read-side product/correctness direction and PR #63 extends its chart/execution observability.

Accepted behavior includes:

- professional ordinary UI primitives via Tremor/TanStack;
- Lightweight Charts for market-price visualization and trade markers;
- Dockview for the desktop support workspace;
- custom visualization only where QAMC-specific decision topology justifies it;
- separate current-session quote vs historical daily-bar semantics;
- pinned session/run context that cannot be silently overwritten by stale polls;
- truthful `AUTO / PRIMARY` behavior;
- candidate-correct PM/Risk attribution;
- persisted execution-skip explanations and explicit reason-not-recorded states;
- Journal decision ledger and exact-run navigation;
- session-specific execution rows with click-to-chart behavior;
- `5m Today`, `15m`, `1h`, `1D` read-only chart timeframes with timestamp-preserving intraday OHLCV;
- no ArcGauge, handmade RunTimeline or old ECharts candidate funnel.

Do not reopen those resolved product decisions without new evidence.

## Verification already recorded for merged read-side work

PR #60 integration verification reported:

- backend/API correctness + GET-only suite: **149 passed**;
- frontend: **50 passed**;
- production TypeScript/Vite build: passed;
- read-only branch preview returned 200 for cockpit/assets and 405 for write methods.

PR #63 verification reported:

- backend: **2,030 passed**;
- frontend: **55 passed**;
- production frontend build: passed;
- desktop/iPad browser verification completed with zero console/page errors against the branch verification setup.

These results justify convergence review; they are not production-deployment proof.

## Remaining uncertainty

The trading recovery is strongly supported by production forensics and deterministic tests, but the complete opportunity→decision→execution→management chain still needs natural market validation after deployment.

Two lower-priority core issues remain outside the current gate unless they materially distort validation:

- news-narrative factual drift;
- `actual_provider` attribution oddity.

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
- Production deployment is a separate governed task; merging code is not deployment proof.
