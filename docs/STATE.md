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
- Smart Money v1 uses first-party SEC Form 4. The operator has authorized a
  fail-closed, run-scoped external-symbol admission lane for fresh material
  open-market purchases; it changes no permanent universe membership and
  preserves the complete accepted decision/risk/execution chain.

## Stabilization account model

The prior three-account daily workflow created excessive friction. During stabilization the accepted operating model is now:

- **`ubuntu` — engineering/operator account.** Codex development, Git/GitHub, development tooling, private DEV preview/browser work, testing, Docker/sudo tasks, and deployment orchestration happen here.
- **`qamc` — production runtime account only.** It owns the production checkout, runtime `.env`/OneCLI wiring, user services/timers, and QAMC execution.
- **`dev` — parked.** It remains present but is removed from the normal workflow. Do not expand its permissions or require the operator to use it during stabilization.

The retained isolation boundary is `ubuntu` engineering/operator vs `qamc` runtime. Codex must not run as `qamc`.

## Production position — current verified deployment

Production is deployed and verified at:

`9c24f78ec99dbcafa62413858aff7e735ae10dbd`

This is GitHub `main` after PR #87. It retains the mandatory paid-analysis cost
circuit, the PR #83 intraday suspension fix and PR #85 credentialless SEC Form
4 source/admission lane, and completes the Research Intelligence Desk against
its accepted editorial and responsive acceptance criteria. The recorded
rollback SHA is `d645ef2d61d8ba4c06dd18c40b0ae44334462cec`.

Production verification on 2026-08-25 established:

- `/health` returned the intentional `status=degraded`, DB reachable, broker
  reachable, `paper=true`, and `decision_path_status=paid_analysis_suspended`;
- the circuit seeded the existing ET-day agent ledger at **$4.211481**, latched
  the **$1.50** daily limit before any post-deployment provider request, and
  recorded successful Telegram alert delivery;
- the first post-deployment midday run reconciled all two broker-protected long
  positions before returning `paid_analysis_suspended`; agent-log ID **189**
  and ET-day spend remained unchanged;
- the 18:30 UTC natural intraday tick again reconciled both broker-protected
  longs and blocked before the Tech request with zero incremental spend. It
  exposed a context-manager observability bug that masked the structured
  suspension with `generator didn't stop after throw()`; PR #83 corrected that
  control flow, added exact propagation/release/no-agent-call regressions and
  was deployed without resetting the circuit;
- all seven existing timers are active; non-LLM safety, reconciliation, close,
  P&L and read-only API work remain available while model calls are blocked;
- the complete merged hermetic suite passed **2,127 tests**; the deployed
  Smart Money/PM/checkpoint/broker/API/stage suite passed **272 tests**;
- the source-only production preflight processed 25 official SEC filings,
  cached 13 exact Form 4 P/S observations, retained five material observations
  for SPIR/TISI and made zero LLM calls. No symbol cleared the higher external
  admission threshold, which is a valid quiet source result rather than an
  acceptance failure;
- `/health`
  remained intentionally degraded, SQLite `quick_check` remained `ok`, the
  circuit remained latched, agent-log ID remained **189** and incremental
  post-deployment spend remained **$0.00**;
- `/cockpit/` returned 200 and the accepted `5m Today`, `15m`, `1h`, and `1D`
  price requests were accepted (the 5m response was naturally empty before
  the Paper market session; the other three returned bars);
- `/research/daily/2026-08-25` returned canonical stored QAMC data, and the
  Research Desk rendered evidence-backed change, tension, why-now, specialist,
  run-local PM/Risk/gate/execution and truthful after-bell states on desktop and
  iPad without console errors, request failures, micro-text or horizontal
  overflow;
- the Desk production asset is `index-DBp3ajHn.js`; Dockview maximize state
  survived a real page reload, and the purpose-built iPad brief, signals,
  decision and review routes passed rendered inspection;
- 71 frontend tests, six focused Research API contract tests, the production
  build and all 16 fixture visual-acceptance scenarios passed;
- POST, PUT, PATCH and DELETE remained rejected with 405;
- OneCLI and Mission Control remained private and reachable;
- the existing private/read-only Mission Control and Research Desk contracts
  remain intact.

The Smart Money seat is enabled on first-party SEC Form 4. Its deterministic
pre-market refresh is credentialless, bounded, cached and available while the
paid circuit is suspended. External symbols can be admitted for one run only
after fresh material open-market purchase evidence plus broker common-stock,
supported-exchange, $5 price, 20-session history, $10M average dollar-volume
and known-sector checks. The run cap is three. Admission bypasses only permanent
universe membership and the Technical prefilter; current Technical analysis,
PM grounding, AI Risk, deterministic risk/funding, broker protection and
Alpaca Paper remain mandatory. The configured 77-stock universe is unchanged.

The production checkout retains exactly one intended tracked local configuration delta:

- `config/settings.yaml`: `intraday_scan.enabled: true`

GitHub `main` may advance beyond this production SHA. Production changes only through the governed engineering workflow below.

## Mandatory paid-analysis cost circuit

Paid model requests share one persistent SQLite authority across systemd
processes. It fails closed before provider I/O on missing/corrupt accounting,
unknown or stale pricing/cost, unresolved attempted requests, excessive provider
attempts, retries, repeated paid sessions, projected exposure, session spend or
ET-day spend. Current limits are
**$0.90 per session**, **$1.50 per ET day**, two provider attempts per logical
call, two retry/repair attempts per session and two paid sessions per mode/day.

Expected budget exhaustion creates the narrowest applicable quota hold: current
run for session spend/retry exposure, current mode/day for paid-session count,
or the current ET day for aggregate spend. Session holds do not block later
independent runs. Day and mode/day holds rearm only after the ET date advances,
the new ledger seeds exactly, accounting invariants pass and no prior-day
attempted reservation remains unresolved. Trip and successful rearm each send
one deduplicated Telegram alert. Missing/corrupt or inexact accounting, unknown
pricing/cost, unresolved attempted requests, provider-attempt exhaustion and any
unrecognized trigger remain a hard global latch requiring an auditable operator
reset. Reset never erases settled spend and cannot bypass a quota hold.

Broker-resident stops, deterministic loss protection, order/fill reconciliation,
close/P&L jobs and the read-only API remain active under every hold/latch.

## Paper-beta engineering authority — accepted rule

While execution remains **Alpaca Paper**, already-authorized engineering work may run end-to-end without a human code-review, merge or deployment gate:

**`ubuntu` engineering → tests/inspection → dedicated PR → merge → governed `ubuntu` → `qamc` deploy → production verification → rollback if needed.**

Important consequences:

- Codex may complete implementation, self-review, merge and Paper-production deployment autonomously.
- Independent review is optional evidence, not permission and not a blocking gate.
- Version control, the dedicated PR and known-good production state provide traceability and rollback.
- A failed production verification stops further mutation and triggers preservation/restoration of the last known-good state.
- This fast lane does **not** authorize live capital, paid dependencies, secrets/credential redesign, destructive infrastructure replacement or material architecture outside current authority.

## Parallel engineering policy — accepted rule

Parallelism and subagents are available and should be used when they reduce wall-clock time without creating coordination waste.

- Parallelize genuinely independent investigation, implementation surfaces, tests, browser/visual verification and evidence gathering.
- The lead agent owns integration and resolves conflicting findings.
- Do not spawn multiple workers to rediscover the same facts or overlap edits without a reason.
- Use strong reasoning models for architecture, trading logic, safety-sensitive changes, complex debugging, hard review and ambiguous product/UX judgment.
- Use cheaper/faster workers for bounded tests, searches, inventories, logs and mechanical evidence collection.
- Escalate a cheap worker when the task becomes reasoning-heavy.

This is an efficiency policy, not an agent-count target.

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
and deployed. It removes ECharts/handmade allocation visuals, moves ordinary tables to TanStack,
keeps price history on Lightweight Charts, makes SGOV unambiguously cash
parking outside directional-risk/P&L emphasis, consumes PR #75 lifecycle facts,
and turns the desktop Candidates/Chart/Decision Room working surface into a
persisted Dockview workspace while retaining simple iPad/mobile tabs. The
Mission Control finish line and backend recovery promotion are complete.

The Research Intelligence Desk and Smart Money seat are merged through PRs #79,
#85 and #87 and deployed. The read-only daily projection, edited daily story,
evidence-backed change/tension/why-now treatment, restrained semantic language,
technical setup context, run-local PM/Risk/gate/execution deltas, persistent
desktop Dockview workspace, purpose-built iPad reading flow and truthful
sparse/degraded/after-bell states are production-verified against stored QAMC
data. The first-party SEC source is commissioned and preserves transaction
versus disclosure time, suppresses stale/lone noise, exposes deterministic
run-scoped admissions and cannot bypass the accepted decision chain. Paid Smart
Money synthesis remains suspended by the intentional cost circuit; that does
not block the completed read-side experience or deterministic source facts.

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
- Deterministic risk/execution semantic redesign outside accepted work.
- Paper-specific trading shortcuts that would create a second trading architecture.
- Broker-write Mission Control controls.
- Telegram command/control.
- Public exposure of QAMC or OneCLI.
- Turning `qamc` into a development/operator account.
- Expanding `dev` permissions or reintroducing it into the normal workflow during stabilization without explicit authorization.
- Forcing/manufacturing trades for validation.
- Live-capital promotion without separate explicit authorization.

## Handoff

Current bounded activities:

1. **Circuit rollover acceptance:** paid analysis is quota-held for the current
   ET day. Do not reset or erase the unchanged $4.211481 ledger. Verify the
   first next-day activation creates an exact new ledger, releases the old-day
   hold once, sends one recovery alert and never inherits prior-day spend.
2. **Smart Money natural evidence:** after automatic ET-day rearm, observe
   model synthesis and any naturally qualifying transient candidate through
   the full accepted chain. Do not force a candidate or weaken thresholds.
3. **Natural Alpaca Paper acceptance:** allow the existing schedule to resume
   paid research only after rollover checks pass; continue non-LLM safety
   observation now.
4. **Evidence-driven follow-up only:** do not reopen resolved dashboard or trading-critical feed defects from historical notes alone; require current evidence.

See `docs/WORK.md` for the active contract and exact Research Intelligence acceptance criteria.
