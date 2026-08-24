# QAMC Current State

Updated: 2026-08-24

This file records what is accepted and true **now**. Git history preserves implementation detail; do not turn this file into a changelog.

## Accepted product / architecture

- **QAMC / Mission Control** is the whole product/system. **Dashboard** is the browser/iPad read-side UI.
- QAMC is an autonomous AI-assisted Alpaca trading system whose **currently authorized execution environment is Alpaca Paper**. Live-broker order submission is not authorized.
- Paper vs live is an execution-environment boundary, not a separate trading architecture. Trading-critical reasoning, risk, execution, position management and journaling remain environment-neutral; genuine broker differences stay at the broker/configuration boundary.
- Decision chain remains: **Specialists → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution → broker**.
- Deterministic Python and broker protections remain final safety authority; uncertainty fails closed.
- Mission Control/API/journal/search/UI remain private, read-only and non-critical to trading.
- Production runtime is owned by `qamc`; administration/recovery by `ubuntu`; development by `dev`. These account boundaries remain hard.
- OneCLI remains the accepted credential-delivery layer. No public listener is authorized.
- Private operator access uses Tailscale. Canonical VPS FQDN: `ovh-vps.wallaby-bowfin.ts.net`.

## Production position — verified 2026-08-24

Observed VPS production checkout:

`2b3faaf69c0b842a08f991a9ca517a3989bdaf93`

This is the PR #63 merge commit and contains the accepted runtime payload through:

- PR #56 trading-utility recovery;
- enriched Telegram trader decision feed (`e113f5c`);
- PR #60 professional Mission Control + data-truth/run-history/explainability work;
- PR #63 session-specific executions and intraday chart functionality.

The production checkout is owned by `qamc` and carries exactly one intended tracked local configuration delta:

- `config/settings.yaml`: `intraday_scan.enabled: true`

A governed preflight on 2026-08-24 verified:

- `ubuntu` sudo authority works;
- `qamc` / `dev` / `ubuntu` account separation remains intact;
- production tracked files are clean except the governed `config/settings.yaml` override;
- all seven expected qamc timers remain enabled;
- `quant-agent-api.service` is active;
- `/health` reports healthy, broker reachable and `paper=true`;
- OneCLI remains private on `127.0.0.1:10254` / `127.0.0.1:10255`;
- the actual production SHA is `2b3faaf...`.

Comparison against the then-current GitHub `main` showed **no runtime/code/config delta**: only `docs/STATE.md` and `docs/WORK.md` differed. Therefore no application deployment was required to achieve convergence; production was already running the accepted PR #63 runtime payload.

## Production acceptance — verified 2026-08-24

A final read-only acceptance pass against actual production completed with **0 failures**:

- `/cockpit` final HTTP 200;
- `/ui` final HTTP 200;
- `/quotes?symbols=SPY` works;
- `/prices/SPY` works for `5m`, `15m`, `1h`, `1d`;
- session execution read path works;
- POST / PUT / PATCH / DELETE are rejected by Mission Control;
- Telegram credentials are present, unmuted and the notifier is enabled under the production `qamc` environment (`--dry-run`, so no test message was sent).

This closes the read-side / Telegram production-convergence task.

## Current operator-observed dashboard discrepancy — 2026-08-24

The operator reports that the actual production price chart currently exposes only the day / `1D` view; minute/hour timeframe controls are not visible or usable in the dashboard.

This conflicts with two verified facts:

- current accepted frontend source defines `5m Today`, `15m`, `1h`, and `1D` controls in `PriceChartPanel`;
- production API acceptance verified working `5m`, `15m`, `1h`, and `1d` price endpoints.

Treat this as a **bounded production UI/runtime regression or asset/rendering discrepancy**, not as authorization to redesign chart architecture or create another chart implementation. The accepted product expectation remains four usable timeframes. Until actual production browser verification proves otherwise, do not describe the multi-timeframe controls as production-visible.

## Mission Control integrated production state

Accepted behavior includes:

- professional cockpit composition using Tremor/TanStack where ordinary UI primitives are needed;
- TradingView Lightweight Charts for price candles/volume and BUY/SELL markers;
- Dockview for the desktop support workspace;
- QAMC-specific decision-chain topology as the justified custom visualization;
- current-session quote context exposed separately from historical bars and broker position marks through GET `/quotes`;
- historical daily candles are never presented as a live/current quote;
- day/session selection can pin an earlier run without a later poll silently replacing it;
- automatic mode truthfully follows the best primary run and is labeled `AUTO / PRIMARY`;
- Candidate / Decision Room / chart context remains synchronized to the same run;
- candidate outcomes surface stopping stage, persisted execution-skip reason when present, and explicit reason-not-recorded wording when candidate-specific evidence is absent;
- run-scoped PM/Risk evidence is not attributed to a candidate that never reached that stage;
- Journal presents a decision ledger and exact-run candidate navigation;
- selected-session execution rows can drive chart context;
- accepted chart design supports `5m Today`, `15m`, `1h`, `1D` using read-only market data, subject to the current production-visibility regression above;
- intraday OHLCV timestamps are preserved so execution markers align to the relevant chart candle;
- live price, previous close and historical bars remain semantically distinct;
- stale/degraded read-side data is identified rather than silently represented as current;
- `ArcGauge`, the handmade `RunTimeline`, and the old ECharts candidate funnel are removed.

PR #63 verification before merge reported:

- backend: **2,030 passed**;
- frontend: **55 passed**;
- production frontend build: passed;
- desktop and iPad browser verification completed with zero console/page errors against the branch verification setup.

The 2026-08-24 production acceptance separately verified the actual production read-side API surface, but it did not prove that every accepted frontend control was visibly rendered in the operator's production browser.

## Trading-utility recovery — natural validation still required

The evidenced mechanical blockers between opportunity discovery and execution have been corrected and the accepted machinery is present in production.

That does **not** yet prove the recovery works as a complete trading system. Acceptance still requires natural Alpaca Paper sessions demonstrating the real chain:

**opportunity discovered → evaluated → defensible bullish/bearish/neutral decision → executed when eligible → managed/exited → measured**.

No trade may be forced or manufactured to create evidence.

## Intraday opportunity discovery

Production intraday discovery is enabled by the one governed local config override.

Accepted live configuration:

- move threshold: **3.0%** absolute move from previous close;
- per-symbol cooldown: **3.0 hours**;
- maximum candidates per tick: **5**;
- approved bearish-expression ETFs: `SH`, `SDS`, `PSQ`, `SQQQ`.

Bearish expression remains through approved inverse ETFs. Direct stock shorts, options/theta strategies and margin remain outside the accepted architecture.

## Model / provider policy

- OpenRouter remains the model-provider path.
- Current accepted routing uses `google/gemini-2.5-flash-lite` and `qwen/qwen3-235b-a22b-2507` according to the per-seat policy.
- Cost-optimized routing and the accepted decision-chain audit remain in force.

## Not authorized

- Live-broker order submission or live-capital activation.
- Direct stock shorting, options/theta strategies, or margin.
- New timers, daemons, databases, proxies, credential systems or durable infrastructure without explicit architectural approval.
- Deterministic risk/execution semantic redesign.
- Paper-specific trading shortcuts.
- Broker-write Mission Control controls.
- Telegram command/control.
- Public exposure of QAMC or OneCLI.
- Collapsing `dev` / `qamc` / `ubuntu` account boundaries.
- Forcing/manufacturing trades for validation.

## Handoff

Two bounded activities may proceed without being conflated:

1. **Dashboard regression:** restore and verify the already-accepted `5m Today` / `15m` / `1h` / `1D` controls in the actual production UI using the smallest justified fix.
2. **Core recovery:** continue natural Alpaca Paper validation of the complete trading chain without forcing activity or weakening safety.

Two lower-priority issues remain outside the current gate unless they materially distort validation:

- news-narrative factual drift;
- `actual_provider` attribution oddity.

See `docs/WORK.md` for the current authorized next work.
