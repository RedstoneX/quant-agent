# QAMC Current State

Updated: 2026-08-24

This file records what is accepted and true **now**. Git history preserves implementation detail; do not turn this file into a changelog.

## Accepted product / architecture

- **QAMC / Mission Control** is the whole product/system. **Dashboard** is the browser/iPad read-side UI.
- QAMC is an autonomous AI-assisted Alpaca trading system whose **currently authorized execution environment is Alpaca Paper**. Live-broker order submission is not authorized.
- Paper vs live is an execution-environment boundary, not a separate trading architecture.
- Decision chain remains: **Specialists → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution → broker**.
- Deterministic Python and broker protections remain final safety authority; uncertainty fails closed.
- Mission Control/API/journal/search/UI remain private, read-only and non-critical to trading.
- OneCLI remains the accepted credential-delivery layer. No public listener is authorized.
- Private operator access uses Tailscale. Canonical VPS FQDN: `ovh-vps.wallaby-bowfin.ts.net`.

## Stabilization account model

The prior three-account daily workflow created excessive friction. During stabilization the accepted operating model is now:

- **`ubuntu` — engineering/operator account.** Claude/Codex development, Git/GitHub, development tooling, private DEV preview/browser work, testing, Docker/sudo tasks, and approved deployment orchestration happen here.
- **`qamc` — production runtime account only.** It owns the production checkout, runtime `.env`/OneCLI wiring, user services/timers, and QAMC execution.
- **`dev` — parked.** It remains present but is removed from the normal workflow. Do not expand its permissions or require the operator to use it during stabilization.

The retained isolation boundary is `ubuntu` engineering/operator vs `qamc` runtime. Claude/Codex must not run as `qamc`.

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

GitHub `main` may advance beyond this production SHA. **Production never automatically follows `main`.**

## Promotion authority — accepted rule

The standing workflow is:

**`ubuntu` engineering implementation/preview/tests → push branch/PR → external review → explicit merge approval → STOP → explicit production approval → governed `ubuntu` → `qamc` deploy/verify.**

Important consequences:

- Claude/Codex may work autonomously from `ubuntu` on already-authorized engineering tasks.
- Claude must not merge its own implementation PR.
- Merge authorization and production-deployment authorization are separate gates.
- Generic instructions such as “proceed”, “continue”, “fix it” or “finish this” never escalate work into the next environment by inference.
- A merged PR, green tests, or an available deploy script is not production authorization.
- Before explicit production approval, `ubuntu` privilege must not be used to mutate `/home/qamc/quant-agent`, `qamc` services/timers, runtime credentials, or production configuration.
- After explicit production approval, `ubuntu` performs the necessary privileged operation directly; the operator should not be bounced between accounts.

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

The chart live-price/current-price truth issue is **already resolved**. Commit `796558f184f8dd800c7e1cbb57f11173ad3d6f6b` (`fix(qamc): show session fills and live chart price`, 2026-08-21) introduced the genuinely live `/quotes` path and separated live/current price from historical bars. Current `PriceChartPanel` also hides the historical series' default last-value line and renders explicit `LIVE` and `PREV CLOSE` lines. This is accepted behavior and is not an outstanding task.

## Trading-utility recovery — mechanical recovery awaiting promotion

Production forensics for sessions 2026-08-18 through 2026-08-24 disproved the
prior claim that all mechanical blockers between opportunity discovery and
execution were corrected. Verified current-code gaps included batch snapshot
failure from Alpaca-incompatible class-share symbols, unmarketable BUY limits
coupled to a 15-second cancel guard, Risk parse failures labeled as rejection,
zero-order runs labeled executed, and SGOV funding sized before entry viability
with whole-order drops on partial funding.

The Priority 1 engineering tranche corrects those mechanics on a dedicated
review branch. It is not accepted in production until its PR is externally
reviewed, explicitly merged, and separately authorized for deployment.

After promotion, acceptance still requires natural Alpaca Paper sessions
demonstrating the real chain:

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

## Market-data feed finding — resolved, not an active defect

The previously flagged concern that `src/execution/broker.py::get_latest_price` omits an explicit Alpaca feed is **not an established defect**. Alpaca's current latest-trade/latest-quote behavior defaults to the best feed available to the subscription; for this account that is IEX. Independent probes confirmed current IEX latest trade/quote data succeeds while explicitly requesting SIP is rejected as unsubscribed, which is expected.

`get_latest_price` returning `None` on a genuine market-data exception is an intentional fail-closed/degradation contract and is covered by existing tests. Do not change this trading-critical method merely because `feed` is omitted. Reopen it only on concrete production evidence of incorrect behavior.

## Not authorized

- Live-broker order submission or live-capital activation.
- Direct stock shorting, options/theta strategies, or margin.
- New timers, daemons, databases, proxies, credential systems or durable infrastructure without explicit architectural approval.
- Deterministic risk/execution semantic redesign.
- Paper-specific trading shortcuts.
- Broker-write Mission Control controls.
- Telegram command/control.
- Public exposure of QAMC or OneCLI.
- Turning `qamc` into a development/operator account.
- Expanding `dev` permissions or reintroducing it into the normal workflow during stabilization without explicit authorization.
- Forcing/manufacturing trades for validation.
- Automatic production deployment merely because code is merged or verified in engineering.

## Handoff

Current bounded activities:

1. **Stabilization workflow:** use the merged two-account model (`ubuntu` engineering/operator, `qamc` runtime-only, `dev` parked) and preserve explicit merge/production gates.
2. **Core recovery:** continue natural Alpaca Paper validation without forcing activity or weakening safety.
3. **Mechanical recovery:** review and promote the Priority 1 backend recovery before treating opportunity→execution mechanics as corrected.
4. **Evidence-driven follow-up only:** do not reopen dashboard or trading-critical feed defects from historical notes alone; require current evidence.

Priority 2 PM truthfulness/model-suitability work and Priority 3 stage
observability remain after this mechanical tranche. See `docs/WORK.md` for the
active execution contract.
