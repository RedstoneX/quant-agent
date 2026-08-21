# QAMC Current State

Updated: 2026-08-21

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

Production remains pinned at:

`d14e28dfc63ca6e4da920229b0ab5ba0f33b93df`

This is the deployed PR #56 trading-utility recovery, accepted as machinery after the governed Gate A→E rollout on 2026-08-20.

The production checkout is intentionally detached at that SHA and carries one intended local configuration delta:

- `config/settings.yaml`: `intraday_scan.enabled: true`

That intraday enablement uses the existing `quant-agent-intra_check.timer` / service; no new daemon or timer was introduced.

## Current `main`

Current `main` is:

`c0edf5f24ee5771d37587aa9188f28857c6fee57`

This is ahead of production and now contains two accepted read-side/observability improvements that are **merged but not yet deployed to production**:

1. `e113f5c6255925f1a93f0f8c242dcd5facbaf41a` — enriched Telegram trader decision feed.
2. PR #60 — integrated Mission Control professional cockpit + data-truth/explainability work, merged at `c0edf5f24ee5771d37587aa9188f28857c6fee57`.

Production therefore must not be described as already running the PR #60 cockpit or `/quotes` endpoint until a governed deployment records a new production pin.

## Mission Control integrated state — merged, deployment pending

PR #60 combines the previously separate professional UI and data-correctness streams into one accepted read-only implementation.

Accepted behavior includes:

- professional cockpit composition using Tremor/TanStack where ordinary UI primitives are needed;
- TradingView Lightweight Charts retained for price candles/volume and BUY/SELL markers;
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
- `ArcGauge`, the handmade `RunTimeline`, and the old ECharts candidate funnel are removed.

Integration verification reported:

- backend/API correctness + GET-only suite: **149 passed**;
- frontend: **50 passed**;
- production TypeScript/Vite build: passed;
- branch preview served cockpit/assets successfully and rejected write methods with 405.

A fresh browser-automation pass of the combined branch was not available because the browser environment could not reach the private Tailscale preview. The accepted UI baseline has repository screenshot evidence; no combined screenshot was fabricated.

## Trading-utility recovery — natural validation still required

PR #56 fixed the evidenced mechanical blockers between opportunity discovery and execution and is deployed/verified as machinery.

That does **not** yet prove the recovery works as a complete trading system. Acceptance still requires natural Alpaca Paper sessions demonstrating the real chain:

**opportunity discovered → evaluated → defensible bullish/bearish/neutral decision → executed when eligible → managed/exited → measured**.

No trade may be forced or manufactured to create evidence.

## Intraday opportunity discovery

Production intraday discovery remains enabled by the one governed local config override.

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
2. **Merged read-side improvements:** current `main` at `c0edf5f` is ready for a separate governed production-convergence task covering the accepted Telegram enrichment and PR #60 Mission Control changes. Deployment must preserve account isolation, the intraday enablement override, existing timers, read-only Mission Control semantics and the exact reviewed target SHA.

See `docs/WORK.md` for the current authorized next work.
