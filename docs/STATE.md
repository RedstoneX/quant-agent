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

## Production position — current verified deployment

Production has been reported and verified at:

`a6758f935910c5cf380cc6a7acedc5f3b78f6366`

That deployment includes PR #69, which restores intraday chart bars by explicitly requesting Alpaca IEX data for the accepted 5m/15m/1h read-only chart path.

Production verification reported:

- non-empty SPY/AAPL intraday bars for 5m, 15m and 1h;
- visible and usable `5m Today`, `15m`, `1h`, and `1D` chart controls;
- `/health` healthy with DB and broker reachable;
- `paper=true`;
- only `quant-agent-api.service` restarted;
- all seven existing `quant-agent-*.timer` units preserved;
- Mission Control remained private/read-only;
- existing Telegram/OneCLI path preserved;
- no broker order submitted, cancelled or modified as part of the read-side deployment.

The production checkout retains exactly one intended tracked local configuration delta:

- `config/settings.yaml`: `intraday_scan.enabled: true`

GitHub `main` may advance beyond this production SHA for documentation or later accepted work. **Production never automatically follows `main`.**

## Promotion authority — accepted rule

The accepted standing workflow is now:

**DEV implementation/preview/tests → push branch/PR → external review → explicit merge approval → STOP → explicit production approval → governed `ubuntu` deploy/verify.**

Important consequences:

- Claude/Codex may work autonomously inside DEV for already-authorized tasks, including private Vite/browser verification.
- Claude must not merge its own implementation PR.
- Merge authorization and production-deployment authorization are separate gates.
- Generic instructions such as “proceed”, “continue”, “fix it” or “finish this” never escalate work into the next environment by inference.
- A merged PR, green tests, or an available deploy script is not production authorization.
- Production checkout/service changes require explicit operator authorization for that promotion step.
- Existing `dev` / `qamc` / `ubuntu` and OneCLI boundaries remain; the friction reduction comes from keeping routine work in DEV and bundling privileged production actions into one operator-approved step, not from weakening those boundaries.

## Mission Control accepted state

Accepted behavior includes:

- professional cockpit composition using Tremor/TanStack for ordinary UI primitives;
- TradingView Lightweight Charts for price candles/volume and BUY/SELL markers;
- Dockview for the desktop support workspace;
- QAMC-specific decision-chain topology as the justified custom visualization;
- current-session quote context exposed separately from historical bars and broker position marks through GET `/quotes`;
- historical daily candles are not presented as a live/current quote;
- automatic mode follows the best primary run and is labeled `AUTO / PRIMARY`;
- selected-session execution rows can drive chart context;
- accepted chart timeframes are `5m Today`, `15m`, `1h`, and `1D` using read-only market data;
- intraday OHLCV timestamps are preserved so execution markers can align to relevant candles;
- stale/degraded read-side data must be identified rather than silently represented as current.

PR #69 solved the intraday IEX entitlement/data-path defect. It did **not** modify `PriceChartPanel`, so the previously operator-observed live-price versus chart-right-edge mismatch is not considered resolved solely by that deployment. If still reproducible, it remains a separate DEV-first dashboard defect subject to the promotion gates above.

## Trading-utility recovery — natural validation still required

The evidenced mechanical blockers between opportunity discovery and execution have been corrected and the accepted machinery is present in production.

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

## Known separate issue requiring explicit authorization

`src/execution/broker.py::get_latest_price` builds latest-trade/latest-quote requests without an explicit Alpaca feed and silently degrades to `None` on failure. Because that path is trading-critical, it requires a separate operator/ChatGPT-authorized production investigation before any code change.

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
- Automatic production deployment merely because code is merged or verified in DEV.

## Handoff

Current bounded activities:

1. **Workflow repair:** restore the operator-controlled merge/production gates while keeping autonomous DEV preview/testing and minimal-sufficient execution.
2. **Dashboard:** if the live-price/chart-right-edge mismatch remains reproducible, fix and verify it in DEV only, then stop at the external gate.
3. **Core recovery:** continue natural Alpaca Paper validation without forcing activity or weakening safety.

See `docs/WORK.md` for the active execution contract.
