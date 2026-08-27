# QAMC Current State

Updated: 2026-08-27

This file records what is accepted and true **now**. Git history preserves implementation detail; do not turn this file into a changelog.

## Accepted product / architecture

- **QAMC / Mission Control** is the whole product/system. **Dashboard** is the browser/iPad read-side UI.
- QAMC is an autonomous AI-assisted Alpaca trading system whose **currently authorized execution environment is Alpaca Paper**. Live-broker order submission is not authorized.
- Paper vs live is an execution-environment boundary, not a separate trading architecture.
- Decision chain remains: **Specialists → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution → broker**.
- **FIXED 2026-08-27 (PR #108) — the exit path now follows this chain.**
  `run_position_review` (`src/pipeline.py`, midday and close) previously called
  `position_reviewer` and executed directly: no Portfolio Manager, no AI Risk
  Manager, so sells were taken by a single model call with no second opinion
  while buys passed three layers. `_risk_review_exits` now puts every
  SELL/REDUCE in front of the AI Risk Manager and drops what it rejects.
  **The failure posture is deliberately asymmetric with entries and this is
  not an oversight:** `RiskStage` fails CLOSED with zero orders when the Risk
  Manager is unparseable, but the exit path fails OPEN. Failing closed on an
  entry means not buying, which costs nothing; failing closed on an exit means
  a thesis-invalidated position cannot be closed because a language model is
  unavailable, with the loss then bounded only by the broker stop. The
  deterministic gates (named-trigger requirement, metric-contradiction veto,
  ATR noise band) run first and are the real protection. Every fail-open path
  logs at ERROR.
- Deterministic Python and broker protections remain final safety authority; uncertainty fails closed.
- Mission Control/API/journal/search/UI remain private, read-only and non-critical to trading.
- OneCLI remains the accepted credential-delivery layer. No public listener is authorized.
- Private operator access uses Tailscale. Canonical VPS FQDN: `ovh-vps.wallaby-bowfin.ts.net`.
- Smart Money v1 uses first-party SEC Form 4. The operator has authorized a
  fail-closed, run-scoped external-symbol admission lane for fresh material
  open-market purchases; it changes no permanent universe membership and
  preserves the complete accepted decision/risk/execution chain.
- **`QAMC_REMEDIATION_SPEC.md` Phase 1 (Tech Analyst structural levels) is
  implemented and merged to `main`** (PR #102). `TechAnalysisResult`
  (`src/models.py`) now requires `support_levels`, `resistance_levels`, `setup_type`
  (`"range"` / `"breakout"`), `expected_horizon_sessions` and `reference_target` for
  every actionable rating, on top of the existing `entry_price` / `stop_loss`; a
  candidate missing any of them fails validation. A new deterministic module,
  `src/data/levels.py`, computes support/resistance from the full OHLCV history
  (swing-pivot detection, zone clustering, distance/recency-weighted ranking) and
  the Tech Analyst prompt now includes a formatted levels block computed over
  `trading.lookback_days: 1800` (~5 years, up from 320) rather than the 20-40 bar
  window it previously reasoned over. `PortfolioConstructor` no longer synthesizes
  a stop (`entry − 2×ATR`, then `entry × 0.95`) or a target
  (`entry × (1 + 2×stop_gap_pct)`) when the analyst omits one — those fallbacks and
  their config fields (`default_stop_atr_multiple`, `fallback_stop_pct`) are deleted.
  **Behavioral consequence: the desk now declines a trade outright — no BUY or SELL
  is constructed — whenever the Tech Analyst does not supply a structural stop and
  target for a symbol**, rather than trading it against an invented number. On top
  of this, a new deterministic module `src/data/context.py` computes market context
  per symbol from the same bars — relative strength vs a same-batch benchmark (SPY,
  else QQQ, else IWM), returns across 1w/1m/3m/6m/12m, 52-week range position, ATR
  as a percentage of price with its 1-year percentile and a volatility state,
  moving-average slopes, consolidation detection (requires both a narrow range and
  small net drift), average dollar volume, up/down volume ratio, and unfilled gaps
  — rendered into the Tech Analyst prompt via `format_context_block()`
  (`src/agents/tech_analyst.py`). `TechAnalystAgent.build_user_message` also accepts
  an optional `days_to_earnings` kwarg, and `MarketDataProvider.get_next_earnings_date()`
  (`src/data/market.py`, new) can supply an approximate trading-session count to a
  symbol's next scheduled earnings — but **nothing in the pipeline calls it or
  passes `days_to_earnings` today**; it is available but unwired.
- **Phase 2a of the remediation spec is committed, not yet merged** — `c89e957`
  on branch `feat/risk-metrics-and-pm-correlation`. It folds in four
  `AGENT_ROLE_AUDIT.md` audit findings that preceded Phase 2's own sizing work:
  the drawdown-halve is now deterministic (`src/risk/rules.py::apply_drawdown_scale`
  + `drawdown_buy_cap` hard block, PM prompt's own halving deleted), the
  correlation matrix is built before the Portfolio Manager decides and shown to
  it as measured clusters (`src/data/correlation.py::correlation_clusters`),
  portfolio heat / budget risk / open risk exist (`src/risk/metrics.py`) and
  render to PM and RM, and R-multiple reaches the Position Reviewer. A new
  `risk.max_portfolio_risk_pct` config field (25%) was reporting-only at this
  point — no gate consumed it yet. Phase 2's own items (§2.1 risk-based
  sizing, §2.2 correlation-aware budget ceiling, §2.4 retiring the fixed
  position count) landed afterward as Phase 2b — see below.
- **Phase 3.1 and 3.2 of the remediation spec are committed, not yet
  merged** — `aea82ee` on branch `feat/exit-rework-pace-and-memory`. §3.1: the
  `pace` metric no longer feeds back on the desk's own realized-trade
  calibration — `expected_horizon_sessions` and `setup_type` are pinned to the
  `trades` row at BUY time and never recomputed, the calibration query is
  deleted from the review path, and `pace_status` (`measured` / `too_early` /
  `n/a_breakout` / `unavailable_no_pinned_horizon`) replaces a fabricated
  number with a labeled absence where pace cannot be measured. §3.2 (closes
  `AGENT_ROLE_AUDIT.md` §1.5): the Position Reviewer now has memory of its own
  prior review — each review snapshots its per-position metrics
  (`db.save_position_review_metrics`), the next review receives the deltas,
  and new `src/risk/exit_guard.py` deterministically vetoes a SELL/REDUCE
  whose stated reason is a deterioration claim when every metric that moved
  since the prior review improved. Exits on new information (news, earnings,
  regime shift, correlation breach, a triggered `thesis_invalid_if`) are never
  vetoed. §3.3 is committed separately — see below. §3.4–§3.7 (routing exits
  through AI Risk, upgrading the reviewer's model off `gemini-2.5-flash-lite`,
  ATR noise band, broker-resident trailing stops) and Phases 4–8
  (evidence/feed repair, short selling, cost/transparency, measurement,
  further documentation correction) remain pending — see
  `docs/QAMC_REMEDIATION_SPEC.md`.
- **Phase 3.3 of the remediation spec is committed, not yet merged** —
  `2f177e33` on branch `feat/exit-gate-and-risk-routing`. The hard-trigger
  phrase gate on exits previously applied only to a symbol already trimmed
  that day, so a position's first sale of the day executed on soft reasoning
  unchecked — almost every sale. Every SELL and REDUCE now requires the
  reason to name a recognised trigger (`src/pipeline.py`,
  `_reason_cites_hard_trigger` against `_HARD_TRIGGER_KEYWORDS`, widened to
  cover macro regime shift, sector shock, adverse/material news, earnings
  miss and guidance cut); a non-matching reason is dropped and logged as
  `exit_blocked_no_named_trigger`, and the position is held, protected by its
  broker-resident stop.
- **Phase 3 of the remediation spec is COMPLETE and DEPLOYED** at `058273f1`
  (rollback `9f77b03e`), live on the paper account since ~09:20 ET
  2026-08-27. §3.1 the `pace` feedback loop is cut — the horizon is pinned to
  the trade row at entry from the analyst's thesis and `avg_hold_days` is gone
  from the review path, so the system's own selling behaviour no longer sets
  the bar every surviving position is measured against. §3.2 the reviewer
  snapshots its metrics and a deterioration verdict that contradicts its own
  improving numbers is vetoed (`src/risk/exit_guard.py`). §3.3/§3.4 as above.
  §3.6 an adverse move inside 1.0x ATR of entry cannot trigger a price-derived
  exit; external-information triggers bypass it. §3.7 trailing stops are
  computed deterministically in `src/risk/trailing.py` before the LLM is asked
  — range setups do not trail until the target is exceeded, breakouts trail
  under confirmed higher lows with a 3x ATR chandelier fallback, ratchet
  upward only. §3.5 was resolved as an owner decision rather than
  implemented: the "weakest model" premise is contradicted by committed
  benchmark data (see `docs/WORK.md`). §3.8 unchanged.
- **Phase 2b (risk-based sizing) is committed, not yet merged or deployed** —
  `75c0233` on branch `feat/pm-flex-routing`. Production is still bounded by
  the constructor's `risk_budget_pct = 0.5` default until this merges. The
  branch replaces `target_weight_pct` with `risk_allocation_pct` as the live
  sizing field and turns `max_portfolio_risk_pct` from a reported figure into
  an enforced gate. **Owner-ratified risk envelope, per `config/settings.yaml`
  `risk:`:** 5% of equity per trade ceiling (`max_position_risk_pct`), 0.5%
  floor below which a request is denied rather than shrunk
  (`min_position_risk_pct`), 25% of equity total at-risk ceiling across the
  book (`max_portfolio_risk_pct`, previously reporting-only), and no single
  correlated cluster (measured return correlation, `src/data/correlation.py`)
  may consume more than 40% of that total (`max_cluster_risk_share_pct`) —
  `src/risk/budget.py::allocate_risk_budget`, largest-request-first with an
  alphabetical tie-break. The gate engages only when the caller supplies the
  book's current risk and clusters (`pipeline_stages._book_risk_inputs`); an
  unmeasurable book falls back to per-position sizing and the 5% single-name
  cap only. `target_weight_pct` stays Optional so stored decisions still
  replay through `src/replay.py` and the Mission Control API. **Caution for
  the next session:** `/home/ubuntu/projects/quant-agent-worktrees/phase2b`
  (branch `feat/risk-based-sizing`) still holds an independent, uncommitted,
  in-progress attempt at this same phase (dirty `src/models.py`, untracked
  `src/data/company.py`) that predates and duplicates `75c0233`. It was not
  touched by this documentation pass; reconcile or discard it before doing
  further Phase 2b work so two implementations don't collide.
  **Two follow-on fixes landed the same day, same branch, also not merged or
  deployed:** `b712f4c` clamps the constructor to the 20% single-name ceiling
  the risk engine actually enforces (`max_position_pct` is a HARD BLOCK, not
  a trim — without this, the sizing above computed orders the engine would
  silently drop). `3dff940` widens an entry stop to a minimum ATR distance
  (`risk.min_stop_atr_multiple`, base 3.0, scaled by setup type and macro
  regime) when the analyst placed it inside ordinary volatility, and rejects
  the trade if the widened stop drops reward:risk below
  `risk.min_reward_risk_after_widening` (1.5) — measured, the book's stops
  were sitting a median 1.7 ATRs from entry, which both fired exits inside
  noise and forced the 20% clamp above to bind at nearly every conviction
  level. `config/prompts/portfolio_manager.md`'s conviction bands and
  `config/prompts/tech_analyst.md`'s stop guidance were recalibrated to match.
  2487 tests pass. Production still runs the pre-clamp, pre-widening
  constructor — true from `058273f1` / `e6ada88` and still true at current
  production HEAD `32c174b` (PR #114 added only the deploy-drift alarm, not
  this fix; see "Production position" below).
- **The Portfolio Manager now routes through OpenRouter's `openai/flex`
  endpoint** (`16f6535`, same branch) — the identical `openai/gpt-5.5-20260423`
  weights at half price ($2.50/$15 vs $5/$30 per M tokens), an endpoint choice
  rather than a model choice. `llm.portfolio_manager_provider_order:
  ["openai/flex"]`; fallbacks stay enabled since the fallback endpoint serves
  the same weights. OpenRouter calls now request `usage: {include: true}` and
  the daily cost circuit spends against the provider-reported figure, not the
  pinned per-model estimate, because one model id now prices two ways.
- **`intra_check` is no longer blindfolded** (`fb88e08`, same branch). The
  intraday PM previously received a technical-only evidence registry even
  though the morning's macro and news were already on disk; both are now
  carried forward, date-scoped and re-validated, and labelled
  `carried_from_morning` in `data_status` rather than `not_run_intraday`.
  Earnings stay excluded — an intraday filing genuinely has not been read
  this tick. Nothing is re-fetched.
- **`AgentLogItem` now surfaces `input_message` / `full_response`**
  (`6b7af86`, same branch), a frontend-only fix — the backend has populated
  and served both fields all along; only the TypeScript interface omitted
  them. See `docs/architecture/MISSION_CONTROL_API.md`.
- **Sector-stance vocabulary is unified** (`cdb387b`, same branch).
  `SECTOR_STANCE_TO_DIRECTION` / `SECTOR_DIRECTIONS` / `normalize_sector_stance`
  now live once in `src/models.py`; `macro_store` imports them instead of
  keeping its own copy. This also fixed a live crash where carrying stored
  macro forward into the intraday PM (`fb88e08`, above) raised `TypeError:
  string indices must be integers` because `build_user_message` indexed the
  persisted `{sector: direction}` dict shape as if it were the live agent's
  list-of-mappings shape.

## Stabilization account model

The prior three-account daily workflow created excessive friction. During stabilization the accepted operating model is now:

- **`ubuntu` — engineering/operator account.** Codex development, Git/GitHub, development tooling, private DEV preview/browser work, testing, Docker/sudo tasks, and deployment orchestration happen here.
- **`qamc` — production runtime account only.** It owns the production checkout, runtime `.env`/OneCLI wiring, user services/timers, and QAMC execution.
- **`dev` — parked.** It remains present but is removed from the normal workflow. Do not expand its permissions or require the operator to use it during stabilization.

The retained isolation boundary is `ubuntu` engineering/operator vs `qamc` runtime. Codex must not run as `qamc`.

## Production position — current pointer and history

**Current production HEAD is `32c174b`** (merge of PR #114, the deploy-drift
alarm — see below), deployed 2026-08-27 evening ET. It descends from `e6ada88`
(PR #112) and `058273f1` (Phase 3, PR #109/#110), so everything recorded
against those SHAs elsewhere in this file still holds. It does **not**
include Phase 2b risk-based sizing (`75c0233`) or the OpenRouter flex-routing
change (`16f6535`) — both are on `feat/pm-flex-routing` (PR #113), still open
and unmerged. See `docs/WORK.md` "Session start" for the full not-deployed
list.

The `a25a723f70a4e0f1548b3389c93c96d9b5ced6d7` / PR #93 SHA recorded below is
**historical** — it was production on 2026-08-26 and has since been
superseded twice over (PR #109/#110, then PR #111/#112, then PR #114). It is
kept only because the forensic narrative under it (the recovery/retry-limit
defect, the Smart Money/earnings data-shaping fixes) is not duplicated
elsewhere. Its recorded rollback SHA was `7fe6e4babbf3cf0209d8f93536f8150de70fea37`;
that rollback target is likewise stale and must not be used against current
production without re-verifying the gap it would reopen.

Production verification on 2026-08-26 established (historical, PR #93 state):

- the prior ET-day quota hold rearmed automatically after the date advanced;
  no spend was erased and no manual reset was used;
- the first natural morning run exposed a mismatch between Tech's per-batch
  recovery and the session-wide retry limit. It admitted RSG, AMR and PAM but
  stopped safely before PM, Risk or broker submission;
- PR #92 now analyzes every primary Tech batch before one bounded consolidated
  recovery, retains successful results, prioritizes run-scoped admissions,
  narrows the deterministic prefilter and compacts Smart Money input. The
  audited operator rerun completed all 54 selected Technical analyses, compared
  with 93 loosely selected symbols before the fix;
- that rerun found seven directional PM candidates but exposed two independent
  data-shaping defects before Risk: a valid fenced Smart Money result was
  reduced to its inner list, and absent queued earnings analysis became the
  false stance `none`;
- PR #93 fixes both contracts at their source. The exact stored Smart Money
  response replays as the full eight-finding object, missing earnings is omitted
  from PM's authoritative registry, and unsupported evidence is still rejected;
- the complete hermetic suite passed **2,181 tests**;
  - *Correction (2026-08-27):* this figure was accurate locally but had never been
    reproduced in CI. GitHub Actions had never executed a single run on this fork,
    so all 94 merges to that date were ungated. The first CI run failed: `fastapi`
    was declared only in the optional `[api]` extra while the workflow installed
    `.[dev]`, so all 8 API test modules failed at collection and pytest exited
    before running anything. Every run would have failed identically. Fixed in #96;
    CI now reproduces 2,181 passing tests;
- the operator-rerun switch may bypass only a same-day morning marker and
  requires a reason. It does not bypass the ET window, weekday check, session
  lock, paid-session/cost circuit, Paper configuration, PM, AI Risk,
  deterministic risk or broker protections. Its use is audit-logged;
- `/health` reports DB and broker reachable, `paper=true`, no active session
  lock and no global circuit suspension. Its degraded label reflects the
  historical session-scoped retry hold from the earlier failed run, not a
  block on later independent sessions;
- exact settled ET-day paid-analysis cost is **$0.5279159 / $1.50**. Both
  permitted paid morning sessions were used, so no third run was attempted;
- Alpaca still reports EPD 12 shares and SGOV 89 shares. EPD remains fully
  covered by a broker-resident 12-share stop-limit order at stop $38.00 and
  limit $36.86. No order was forced, submitted, cancelled or modified;
- Mission Control remains private/read-only and the intended production-only
  `intraday_scan.enabled: true` setting was preserved.

The Smart Money seat is enabled on first-party SEC Form 4. Its deterministic
pre-market refresh is credentialless, bounded, cached and available while the
paid circuit is suspended. External symbols can be admitted for one run only
after fresh material open-market purchase evidence plus broker common-stock,
supported-exchange, $5 price, 20-session history, $10M average dollar-volume
and known-sector checks. The run cap is three. Admission bypasses only permanent
universe membership and the Technical prefilter; current Technical analysis,
PM grounding, AI Risk, deterministic risk/funding, broker protection and
Alpaca Paper remain mandatory. The configured 101-stock universe is unchanged.

The production checkout retains exactly one intended tracked local configuration delta:

- `config/settings.yaml`: `intraday_scan.enabled: true`

GitHub `main` may advance beyond the current production SHA. Production changes only through the governed engineering workflow below.

**Deploy-drift alarm (2026-08-27, PR #114).** This "may advance beyond" gap is
now monitored instead of silently trusted: `scripts/check_deploy_drift.py`
compares the deployed HEAD on the qamc box against `origin/main` and alerts
over Telegram when they diverge, run by `quant-agent-drift-check.timer`
Mon-Fri 08:45 ET. It exists because PR #111 sat merged-but-undeployed for
eight hours with nothing catching it (`docs/WORK.md` records that incident).
Merged and deployed as part of `32c174b`; verified firing.

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
- **CI is now a blocking gate.** As of 2026-08-27 `main` is protected: the `pytest`
  check is required, strict mode is on, and `enforce_admins` is true, so a failing
  suite blocks merges for everyone including administrators. Verified by merging a
  deliberately failing test (refused: `Required status check "pytest" is failing`).
- Changes to `OUTCOME.md`, `STATE.md` and `WORK.md` require **owner ratification**
  — agents propose, the owner accepts by merging. See `AGENTS.md`.
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

Bearish expression currently runs through approved inverse ETFs. **Direct stock
shorting and margin are authorized as of 2026-08-27** (owner ratification; see
`docs/OUTCOME.md`) and are pending implementation — the broker layer contains no
short-selling capability at all today. The prior exclusion was an engineering scope
decision recorded here as an accepted constraint; it was never an owner requirement.
Options and theta strategies remain outside the accepted architecture.

Note also that nothing in the codebase currently tells the Portfolio Manager that
`SH`, `SDS`, `PSQ` and `SQQQ` are bearish instruments, so even the sanctioned
bearish expression is not actually wired up.

## Model / provider policy

- OpenRouter remains the model-provider path.
- Current engineering routing uses `openai/gpt-5.5` for Portfolio Manager,
  `qwen/qwen3-235b-a22b-2507` for Risk Manager, and
  `google/gemini-2.5-flash-lite` for the remaining seats, according to the
  measured per-seat policy. Production remains on its separately promoted config.
- **Committed, not yet merged or deployed** (`16f6535`, branch
  `feat/pm-flex-routing`): the Portfolio Manager's `openai/gpt-5.5` calls now
  prefer OpenRouter's `openai/flex` endpoint over `openai`/`azure` — the same
  model weights at half the per-token price. This is an endpoint preference
  (`llm.<agent>_provider_order`), not a routing change, and it is rejected at
  config load on any seat not on OpenRouter. See `docs/architecture/MODEL_ROUTING_POLICY.md`.

## Market-data feed finding — resolved, not an active defect

The previously flagged concern that `src/execution/broker.py::get_latest_price` omits an explicit Alpaca feed is **not an established defect**. Alpaca's current latest-trade/latest-quote behavior defaults to the best feed available to the subscription; for this account that is IEX. Independent probes confirmed current IEX latest trade/quote data succeeds while explicitly requesting SIP is rejected as unsubscribed, which is expected.

**ACTIVE DEFECT — research feeds are degraded (observed 2026-08-26).** Production
logs show the Reuters Business feed returning HTTP 404 and AP Business returning
HTTP 403 (16 occurrences each), repeated FRED macro API timeouts (13), 28 incomplete
Tech batches, and 11 `Portfolio decision failed deterministic grounding` errors. The
news and macro seats are therefore frequently operating with no data, and nothing
surfaces that fact to the operator. Repair is `docs/QAMC_REMEDIATION_SPEC.md`
Phase 4.2, which also adds a feed-health signal to the alerts and dashboard.

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

1. **Natural Alpaca Paper acceptance:** the Aug 26 rerun proved source,
   admission, full Technical coverage and PM candidate generation. The first
   post-PR #93 eligible session must still prove PM → AI Risk → deterministic
   gate → execution when eligible, followed by management/exit and measurement.
2. **No artificial activity:** do not exceed paid-session limits, force a trade
   or weaken evidence/risk thresholds. A no-trade result must be specific and
   defensible rather than caused by a pipeline defect.
3. **Evidence-driven follow-up only:** do not reopen resolved dashboard or
   trading-critical feed defects from historical notes alone; require current
   evidence.

See `docs/WORK.md` for the active contract and exact Research Intelligence acceptance criteria.
