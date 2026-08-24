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

## Production position

The last governed production record pins production at:

`d14e28dfc63ca6e4da920229b0ab5ba0f33b93df`

This is the deployed PR #56 trading-utility recovery, accepted as machinery after the governed Gate A→E rollout on 2026-08-20.

That governed production checkout carries one intended local configuration delta:

- `config/settings.yaml`: `intraday_scan.enabled: true`

That intraday enablement uses the existing `quant-agent-intra_check.timer` / service; no new daemon or timer was introduced.

**Deployment preflight must verify the actual VPS checkout, working tree, services and timers before relying on this recorded pin.** GitHub governance records the last accepted production state; it does not substitute for observing the current VPS.

## Accepted merged read-side code

`main` includes three accepted read-side/observability improvements that are **merged but not yet recorded as deployed to production**:

1. `e113f5c6255925f1a93f0f8c242dcd5facbaf41a` — enriched Telegram trader decision feed.
2. PR #60 — integrated Mission Control professional cockpit + data-truth/explainability work, whose code merge commit is `c0edf5f24ee5771d37587aa9188f28857c6fee57`.
3. PR #63 — session-specific executions plus live/intraday chart functionality, merged at `2b3faaf69c0b842a08f991a9ca517a3989bdaf93`.

PR #63 adds read-only session execution rows, BUY/SELL chart markers, explicit live-price versus previous-close presentation, `5m Today` / `15m` / `1h` / `1D` chart timeframes, and timestamp-preserving intraday OHLCV reads through the existing Alpaca market-data client. It does not change trading decisions, risk semantics, order submission/cancellation, timers, credentials or live-trading authorization.

The deployment target must always be resolved explicitly from current `main` at deployment time rather than inferred from this file, because subsequent governance/documentation commits can advance `main` without changing the accepted code payload.

Production must not be described as already running PR #60, PR #63, `/quotes`, intraday chart timeframes, or the enriched Telegram feed until governed production convergence records and verifies a new production pin.

## Mission Control integrated state — merged, deployment pending

PR #60 established the accepted professional/read-side correctness baseline and PR #63 extends it with session execution/chart capability.

Accepted behavior includes:

- professional cockpit composition using Tremor/TanStack where ordinary UI primitives are needed;
- TradingView Lightweight Charts for price candles/volume and BUY/SELL markers;
- Dockview retained for the desktop support workspace;
- QAMC-specific decision-chain topology retained as the justified custom visualization;
- current-session quote context exposed separately from historical daily bars and broker position marks through GET `/quotes`;
- historical daily candles are never presented as a live/current quote;
- day/session selection can pin an earlier run without a later poll silently replacing it;
- automatic mode truthfully follows the best primary run and is labeled `AUTO / PRIMARY`, not literal latest;
- Candidate / Decision Room / chart context remains synchronized to the same run;
- candidate outcomes surface stopping stage, persisted execution-skip reason when present, and explicit “reason not recorded” wording when candidate-specific evidence is absent;
- run-scoped PM/Risk evidence is not attributed to a candidate that never reached that stage;
- Journal presents a decision ledger and exact-run candidate navigation;
- selected-session execution rows can drive chart context;
- chart controls support `5m Today`, `15m`, `1h` and `1D` using read-only market data;
- intraday OHLCV timestamps are preserved so execution markers align to the relevant chart candle;
- live price, previous close and historical bars remain semantically distinct;
- `ArcGauge`, the handmade `RunTimeline`, and the old ECharts candidate funnel are removed.

PR #60 integration verification reported:

- backend/API correctness + GET-only suite: **149 passed**;
- frontend: **50 passed**;
- production TypeScript/Vite build: passed;
- branch preview served cockpit/assets successfully and rejected write methods with 405.

PR #63 verification reported:

- backend: **2,030 passed**;
- frontend: **55 passed**;
- production frontend build: passed;
- desktop and iPad browser verification completed with zero console/page errors against the branch verification setup.

These are pre-deployment verification results, not production-deployment proof.

## Trading-utility recovery — natural validation still required

PR #56 fixed the evidenced mechanical blockers between opportunity discovery and execution and is deployed/verified as machinery according to the last governed production record.

That does **not** yet prove the recovery works as a complete trading system. Acceptance still requires natural Alpaca Paper sessions demonstrating the real chain:

**opportunity discovered → evaluated → defensible bullish/bearish/neutral decision → executed when eligible → managed/exited → measured**.

No trade may be forced or manufactured to create evidence.

## Intraday opportunity discovery

The last governed production record has intraday discovery enabled by the one governed local config override.

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

Two workstreams continue without being conflated:

1. **Core recovery:** continue natural Alpaca Paper validation; do not force activity.
2. **Merged read-side improvements:** the accepted Telegram enrichment, PR #60 Mission Control/data-correctness work and PR #63 session-execution/intraday-chart work are ready for a single governed production-convergence task.

That production-convergence task must:

- first verify the actual VPS production SHA, working tree, services, timers and intentional local configuration;
- resolve the exact current `main` deployment target and inspect the full delta from actual production;
- preserve `intraday_scan.enabled: true`, existing account isolation, OneCLI credential handling, current timers and read-only Mission Control semantics;
- deploy only after the candidate target is understood and preflight passes;
- verify production behavior after cutover; and
- update `docs/STATE.md` / `docs/WORK.md` with the actual deployed result.

See `docs/WORK.md` for the current authorized next work.
