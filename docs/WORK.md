# QAMC Current Work

Status: **CORE RECOVERY IN NATURAL PAPER VALIDATION | MISSION CONTROL + TELEGRAM PRODUCTION CONVERGENCE PENDING**

## Current integration truth

- Trading-utility recovery PR #56 is deployed to production at `d14e28dfc63ca6e4da920229b0ab5ba0f33b93df` and accepted as machinery.
- Production remains intentionally detached at that exact SHA with one governed local delta: `intraday_scan.enabled: true`.
- `main` now includes:
  - `e113f5c` — enriched Telegram trader decision feed;
  - PR #60 — integrated professional Mission Control cockpit plus data truth / run-history / explainability, code-merged at `c0edf5f24ee5771d37587aa9188f28857c6fee57`;
  - subsequent documentation-only reconciliation commits.
- PR #59 is superseded by PR #60 and must not be deployed separately.
- The read-side improvements are merged but **not yet deployed to production**.

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

### 1. Resolve the exact deployment target

Before any production cutover, read current `main` and record the exact target SHA. Do not hard-code a self-referential “current main” SHA into governance docs because documentation merges themselves advance `main`.

The deployment target must contain the already-accepted:

- Telegram enrichment (`e113f5c`);
- PR #60 Mission Control integration (`c0edf5f` code merge);
- only intentional subsequent documentation changes.

Verify the exact production delta from `d14e28d` to that target before cutover.

### 2. Governed production convergence

Deploy the exact reviewed target through the existing governed rollout path.

Preserve the authorized local production override:

`intraday_scan.enabled: true`

Do not change the seven existing `qamc` user timers.

Confirm no trading-critical semantics, account boundaries or credential architecture changed beyond already accepted code.

### 3. Post-deploy acceptance

After deployment, verify at minimum:

- production pin equals the exact reviewed target SHA;
- `/health` is healthy and reports `paper=true`;
- `/cockpit` and `/ui` return 200;
- `/quotes` is available and read-only;
- Mission Control rejects POST/PUT/PATCH/DELETE writes;
- account/position/price views remain truthful and do not present historical daily close as current quote;
- Today Sessions / `AUTO / PRIMARY` behavior loads against real production read-side data;
- Telegram output still sends through the accepted OneCLI path;
- existing timers remain active/unchanged;
- `dev` / `qamc` / `ubuntu` account boundaries remain intact;
- no secret material appears in logs or repository state.

If the private browser is available to the operator after deployment, perform the missing combined visual pass at desktop and iPad widths. This is a product verification item, not permission to expose the preview publicly.

### 4. Continue natural trading validation

Do not stop or manufacture the Alpaca Paper soak for the dashboard deployment.

Natural sessions still need to demonstrate:

- worthwhile opportunities survive discovery and reach PM/Risk;
- eligible trades can reach funded broker submission;
- supported bearish opportunities can be expressed through approved inverse ETFs;
- no-trade decisions remain possible and explainable;
- positions and exits behave coherently after entry;
- funding/execution failures are visible rather than mistaken for investment judgment;
- outcomes and missed opportunities can be measured without hindsight tuning.

## Mission Control acceptance now carried by `main`

The merged PR #60 establishes the accepted read-side product direction:

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
- no ArcGauge, handmade RunTimeline or old ECharts candidate funnel.

Do not reopen those resolved product decisions without new evidence.

## Remaining uncertainty

The trading recovery is strongly supported by production forensics and deterministic tests, but the complete opportunity→decision→execution→management chain still needs natural market validation after deployment.

Two lower-priority core issues remain outside the current gate unless they materially distort validation:

- news-narrative factual drift;
- `actual_provider` attribution oddity.

The combined Mission Control branch lacked a fresh automated browser pass because the available browser environment could not reach the private Tailscale preview. Code/API/unit/build verification passed; post-deploy operator-side browser inspection should close that visual evidence gap.

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
