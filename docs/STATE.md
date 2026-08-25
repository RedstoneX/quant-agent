# QAMC Current State

Updated: 2026-08-25

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

- **`ubuntu` — engineering/operator account.** Codex development, Git/GitHub, development tooling, private DEV preview/browser work, testing, Docker/sudo tasks, and approved deployment orchestration happen here.
- **`qamc` — production runtime account only.** It owns the production checkout, runtime `.env`/OneCLI wiring, user services/timers, and QAMC execution.
- **`dev` — parked.** It remains present but is removed from the normal workflow. Do not expand its permissions or require the operator to use it during stabilization.

The retained isolation boundary is `ubuntu` engineering/operator vs `qamc` runtime. Codex must not run as `qamc`.

## Production position — current verified deployment

Production is deployed and verified at:

`16c52715b3ee05ec9e38c12958a14ee77a6d38d7`

This is GitHub `main` after PRs #74, #75, #76 and #77. It deploys the backend
recovery and the final Mission Control utility tranche. The recorded rollback
SHA is `a6758f935910c5cf380cc6a7acedc5f3b78f6366`.

Production verification on 2026-08-25 established:

- `/health` returned `status=ok`, DB reachable, broker reachable and
  `paper=true`;
- OpenRouter and the configured per-seat policy worked through OneCLI,
  including a real completion served by Portfolio Manager model
  `openai/gpt-5.5`;
- `/cockpit/` returned 200 and the accepted `5m Today`, `15m`, `1h`, and `1D`
  price requests were accepted (the 5m response was naturally empty before
  the Paper market session; the other three returned bars);
- POST, PUT, PATCH and DELETE remained rejected with 405;
- OneCLI and Mission Control remained private and reachable;
- Telegram credentials and the bot API path validated without sending a
  message;
- only `quant-agent-api.service` was restarted;
- all seven existing `quant-agent-*.timer` units remained enabled and listed;
- no broker order was submitted, cancelled or modified.

The production checkout retains exactly one intended tracked local configuration delta:

- `config/settings.yaml`: `intraday_scan.enabled: true`

GitHub `main` may advance beyond this production SHA. **Production never automatically follows `main`.**

## Promotion authority — accepted rule

The standing workflow is:

**`ubuntu` engineering implementation/preview/tests → push branch/PR → external review → explicit merge approval → STOP → explicit production approval → governed `ubuntu` → `qamc` deploy/verify.**

Important consequences:

- Codex may work autonomously from `ubuntu` on already-authorized engineering tasks.
- Codex must not merge its own implementation PR.
- Merge authorization and production-deployment authorization are separate gates.
- Generic instructions such as “proceed”, “continue”, “fix it” or “finish this” never escalate work into the next environment by inference.
- A merged PR, green tests, or an available deploy script is not production authorization.
- Before explicit production approval, `ubuntu` privilege must not be used to mutate `/home/qamc/quant-agent`, `qamc` services/timers, runtime credentials, or production configuration.
- After explicit production approval, `ubuntu` performs the necessary privileged operation directly; the operator should not be bounced between accounts.

## Mission Control accepted state

Accepted behavior includes:

- professional cockpit composition using Tremor/TanStack for ordinary UI primitives;
- TradingView Lightweight Charts for price candles/volume and BUY/SELL markers;
- Dockview for the desktop workspace;
- QAMC-specific decision-chain topology as the justified custom visualization;
- current-session quote context exposed separately from historical bars and broker position marks through GET `/quotes`;
- historical daily candles are not presented as a live/current quote;
- automatic mode follows the best primary run and is labeled `AUTO / PRIMARY`;
- selected-session execution rows can drive chart context;
- accepted chart timeframes are `5m Today`, `15m`, `1h`, and `1D` using read-only market data;
- intraday OHLCV timestamps are preserved so execution markers can align to relevant candles;
- stale/degraded read-side data must be identified rather than silently represented as current.

The chart live-price/current-price truth issue is **already resolved**. Commit `796558f184f8dd800c7e1cbb57f11173ad3d6f6b` (`fix(qamc): show session fills and live chart price`, 2026-08-21) introduced the genuinely live `/quotes` path and separated live/current price from historical bars. Current `PriceChartPanel` also hides the historical series' default last-value line and renders explicit `LIVE` and `PREV CLOSE` lines. This is accepted behavior and is not an outstanding task.

## Trading-utility recovery — deployed

Production forensics for sessions 2026-08-18 through 2026-08-24 disproved the
prior claim that all mechanical blockers between opportunity discovery and
execution were corrected. Verified current-code gaps included batch snapshot
failure from Alpaca-incompatible class-share symbols, unmarketable BUY limits
coupled to a 15-second cancel guard, Risk parse failures labeled as rejection,
zero-order runs labeled executed, and SGOV funding sized before entry viability
with whole-order drops on partial funding.

Priority 1 is merged through PR #74 and deployed.

The final backend tranche adds fail-closed, machine-checkable PM specialist
provenance and holding validation; records PM model/parse/grounding failures as
agent failures; and extends the existing `specialist_evidence`/`trades` state so
the complete candidate/order/protection/outcome chain and deterministically
derivable realized P&L are queryable through the existing API. Production-scale
measurement supports changing only the PM seat to `openai/gpt-5.5`; Risk
routing and deterministic Python/broker authority remain unchanged. This final
tranche is merged through PR #75 and deployed.

The final Mission Control utility tranche is merged through PRs #76 and #77
and deployed. It
removes ECharts/handmade allocation visuals, moves ordinary tables to TanStack,
keeps price history on Lightweight Charts, makes SGOV unambiguously cash
parking outside directional-risk/P&L emphasis, consumes PR #75 lifecycle facts,
and turns the desktop Candidates/Chart/Decision Room working surface into a
persisted Dockview workspace while retaining simple iPad/mobile tabs. The
Mission Control finish line and backend recovery promotion are complete.

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
- Current engineering routing uses `openai/gpt-5.5` for Portfolio Manager,
  `qwen/qwen3-235b-a22b-2507` for Risk Manager, and
  `google/gemini-2.5-flash-lite` for the remaining seats, according to the
  measured per-seat policy. Production remains on its separately promoted config.

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
2. **Natural Alpaca Paper acceptance:** collect the remaining substantive
   evidence without forcing activity or weakening safety: opportunity →
   evaluation → defensible decision → eligible execution → management/exit →
   measured outcome.
3. **Evidence-driven follow-up only:** do not reopen dashboard or
   trading-critical feed defects from historical notes alone; require current
   evidence.

See `docs/WORK.md` for the active natural-acceptance contract.
