# QAMC Remediation Specification

**Author:** Claude (Opus 5), with Rex Redstone
**Date:** 2026-08-27
**Status:** Owner-ratified. This document supersedes conflicting statements in `docs/OUTCOME.md`, `docs/STATE.md`, and `docs/WORK.md`.

---

## 0. The Mandate (corrected)

QAMC exists to **make money**. It is not a research experiment into whether cheap models add trading value. `docs/OUTCOME.md` currently states the latter; that framing was authored by coding agents and is wrong.

**The edge, stated so it is testable:**

> **Breadth × consistency × asymmetry.** Underwrite ~100 liquid names daily across technicals, fundamentals, news, macro and insider flow — coverage no individual sustains. Act identically every time, long or short. Risk a bounded fraction per idea, cut losers at pre-defined structure, and let winners run. Be right ~51% of the time and make roughly twice as much when right as is lost when wrong.

**Operating parameters:**

| Parameter | Value |
|---|---|
| Horizon | Swing — days to weeks |
| Direction | Long **and** short (real shorting, Alpaca margin) |
| Autonomy | Fully hands-off. The system never asks for trade approval. |
| Capital deployment | **Fully deployed.** No idle T-bill parking unless nothing is worth owning. |
| Max risk per trade | **5% of equity** (ceiling, not target) |
| Min risk per trade | **0.5%** — below this, don't trade |
| Max total at risk | **25% of equity**, correlation-aware |
| Environment | Alpaca **Paper** until explicitly authorized otherwise |

**Governing principle: agents decide, deterministic code bounds.**
The desk chooses what to buy, how much conviction it carries, where the stop belongs, when to exit. Deterministic Python enforces only the outer envelope — the limits that must survive a model having a bad day.

---

## Phase 0 — Gates (blocking; nothing else merges until done)

Nothing below should be merged until there is a working test gate. 94 PRs have already merged ungated; adding trading-logic changes to that pile would be reckless.

**0.1 — Enable CI.**
`.github/workflows/test.yml` exists but has **never executed**. Root cause is almost certainly that **GitHub disables Actions by default on forked repositories**.
- Enable Actions: repo Settings → Actions → General → Allow all actions. *(Owner action.)*
- Update the workflow to reflect current test/build requirements.
- Prove it runs green on `main`.
- **Deliberately push a failing test and confirm the merge is blocked.** This is the only step that constitutes proof. Do not skip it.
- Add branch protection on `main` requiring the check to pass. A gate that reports but does not block is not a gate.

**0.2 — Ratification rule.**
Root cause of QAMC's drift: coding agents write the documents that define "accepted truth," and `STATE.md` declares independent review *"optional evidence, not permission and not a blocking gate."* Scope decisions made for engineering convenience (e.g. dropping short selling) laundered into requirements that every later agent treated as owner intent.

- Amend `AGENTS.md`: agents may **propose** changes to `STATE.md` / `WORK.md` / `OUTCOME.md`; only the owner may **accept** them. Proposals live in the PR description until ratified.
- Any existing constraint in those documents is **unverified** until the owner confirms it.

**0.3 — Commit the production config drift.**
Live `config/settings.yaml` has uncommitted edits. `STATE.md` claims exactly one intended delta. Reconcile and commit, so what runs is what is in git.

---

## Phase 1 — Tech Analyst returns real structure — IMPLEMENTED (branch `feat/tech-analyst-structural-levels`)

Everything downstream depends on this. Stops, targets, thesis progress and pace are all currently derived from *invented* numbers because the analyst supplies no levels.

**Status note (2026-08-27):** the substance of 1.1–1.4 is implemented, with two departures from this text worth recording:

- The spec names a new `suggested_stop_price` field on the Tech Analyst's output. The implementation reuses the existing `stop_loss` field on `TechAnalysisResult` (`src/models.py`) instead of adding a differently-named one; `suggested_stop_price` remains a distinct field elsewhere, on the Portfolio Manager's `TargetPosition`, which `PortfolioConstructor._resolve_stop` still checks first.
- `reference_target` was made **mandatory** for every actionable rating (not "only where structure supports one" as originally written) — the validator in `src/models.py` requires it, `setup_type`, `expected_horizon_sessions`, and at least one support/resistance level together, or the result is rejected. For a `"breakout"` setup, `reference_target` is documented (`config/prompts/tech_analyst.md`) as a measured-move projection rather than a defended level.
- Two things were added beyond this section's original text: a standalone deterministic levels module (`src/data/levels.py`, new) that finds support/resistance from OHLCV bars via bad-print filtering, swing-pivot detection, zone clustering, distance/recency-weighted ranking; and raising `trading.lookback_days` from 320 to 1800 (~5 years, `config/settings.yaml`) so those levels are computed over real multi-year history rather than the ~1-year window the 320-day figure implied.

**1.1 — Require structural levels. DONE.**
The Tech Analyst must return, per candidate:
- `support_levels[]`, `resistance_levels[]` — actual prior consolidation / swing structure. **Done** — computed by `src/data/levels.py` over the full fetched history and required by the validator for actionable ratings.
- `suggested_stop_price` — derived from structure, never from a volatility multiple. **Done, via the existing `stop_loss` field** rather than a new field of this name (see status note above).
- `setup_type` — see 1.2. **Done** — `Literal["range", "breakout"]` on `TechAnalysisResult`, required for actionable ratings.
- `reference_target` — only where structure supports one. **Done, but made unconditionally required** for actionable ratings rather than conditional (see status note above).
- `expected_horizon_sessions` — the analyst's own estimate of time-to-resolution. **Done**, required for actionable ratings.

**1.2 — Classify the setup.** Two types, managed differently. **`setup_type` field is implemented and required; the differentiated exit-management behavior in the table below (fixed target vs. trailing-only, progress/pace enable/disable) is NOT implemented — that is Phase 3 work and remains pending.**

| Type | Definition | Stop | Exit management |
|---|---|---|---|
| **A — Range / level** | Clear overhead resistance exists | Below structural support | Fixed target; progress & pace metrics valid |
| **B — Breakout / trend** | No overhead resistance (all-time highs, clean break) | Below the breakout level or prior consolidation high | **Trailing only. No fixed target. Progress and pace metrics DISABLED.** |

**1.3 — No levels, no trade. DONE.** If the analyst cannot identify structure, the candidate is rejected: the model validator requires at least one support/resistance level for any actionable rating, and `PortfolioConstructor._resolve_stop` rejects (returns `None`) a BUY with no structural stop. The `2 × ATR` and `5%` fallbacks are deleted from `src/portfolio_constructor.py` (and the `default_stop_atr_multiple` / `fallback_stop_pct` config fields removed with them). `atr_14` is retained on `TechAnalysisResult` for a future noise-band use (Phase 3); it is no longer read as a stop source anywhere in `portfolio_constructor.py`.

**1.4 — Delete the invented target. DONE.** `entry × (1 + 2 × stop_gap_pct)` is removed from `portfolio_constructor.py`; a BUY with no `reference_target` from the analyst is now rejected rather than assigned a synthesized one, so a Type B (breakout) position without a real reference is a declined trade today rather than "no target, handled as a first-class state" — the latter is Phase 3 exit-rework territory (disabling progress/pace for breakouts) and remains pending.

**Phase 1b — market context. DONE, not in the original spec text.** Structural levels alone left the Tech Analyst without most of what a technical analyst actually reasons about — it had moving averages, RSI, MACD, Bollinger bands, ATR and a single volume-change percentage, and nothing else. `src/data/context.py` (new) computes, deterministically from bars already fetched, and renders via `format_context_block()` ahead of the levels block in the prompt:

- Relative strength vs a benchmark drawn from the same batch (SPY, else QQQ, else IWM — no extra fetch), over 1m and 3m.
- Returns over 1w / 1m / 3m / 6m / 12m.
- Position within the trailing 52-week range.
- ATR as a percentage of price, its percentile against its own trailing year, and an `expanding` / `contracting` / `stable` volatility state.
- MA20 / MA50 / MA200 slopes over a 10-session lookback — trend direction of the averages, not merely price's position against them.
- Consolidation detection: a candidate window must be **both** narrow (≤8% high/low spread over 15 sessions) **and** low-drift (net move ≤50% of the range) to be flagged — a narrow window alone does not distinguish a base from a slow steady trend.
- 20-day average dollar volume and 20-day up/down volume ratio (accumulation vs. distribution).
- Unfilled price gaps (≥2%, up to 3 most recent).

`src/agents/tech_analyst.py` wires this in per symbol and accepts an optional `days_to_earnings` kwarg to render an earnings-proximity warning line. **Not wired end to end**: `src/data/market.py` adds `MarketDataProvider.get_next_earnings_date()`, which estimates trading sessions to the next scheduled earnings report (approximate — calendar days × 5/7, not a precise trading-calendar count) — the first place in the system that has ever known a *future* earnings date, as distinct from `src/data/earnings.py`, which is retrospective (it finds filings already on EDGAR). As of 2026-08-31 that method **is called** — by `src/data/event_calendar.py::fetch_earnings_proximity`, which sweeps it for every symbol the Risk Manager is judging and renders a session count or a NAMED absence into RM's Event Risk block. **Still true:** nothing passes `days_to_earnings` to the Tech Analyst — that kwarg is accepted and handled but always empty in practice today. `tests/test_context.py` (new, 27 tests) covers the module.

**Status of everything below (2026-08-27, corrected 2026-08-28).** This line
was previously a blanket "none of this work has been implemented", which
stopped being true the moment Phase 2a landed and is exactly the kind of
stale absolute that misleads the next session. The status below is itself
dated and will go stale the same way — it is not a live pointer. For what is
actually deployed right now: `sudo -n -u qamc git -C /home/qamc/quant-agent
log --oneline -1`, cross-referenced against `docs/STATE.md` → "Production
position" and `docs/WORK.md`'s ordered backlog. As of the dates given below:

- **Phase 2a** — merged and deployed (PR #106). The four audit-finding fold-ins: deterministic
  drawdown gate, correlation to the PM, portfolio heat, R-multiple.
- **Phase 2b** (§2.1 risk-based sizing, §2.2 correlation-aware budgeting,
  §2.4 retire the position-count concept) — merged and deployed 2026-08-27
  evening (commit `75c0233`, branch `feat/pm-flex-routing`, merged as
  PR #113). `max_portfolio_risk_pct` is
  now an enforced gate, not a reported figure. Two follow-on fixes landed the
  same day, same branch, also merged and deployed: the constructor now clamps to
  the 20% single-name ceiling instead of proposing orders the engine
  hard-blocks (`b712f4c`), and entry stops sitting inside ordinary volatility
  are widened to a regime/setup-scaled ATR floor before that clamp is even
  computed (`3dff940`) — see the Phase 2/Phase 3 sections below.
- **Phase 3 (all of 3.1–3.4, 3.6, 3.7)** — merged and deployed 2026-08-27
  ~09:20 ET to the live
  paper account at `058273f1`. §3.5 resolved as an owner decision rather than
  implemented (see the correction note under §3.5); §3.8 unchanged by design.
- **Phase 4.1** — merged and deployed 2026-08-27 evening (commit `fb88e08`,
  branch `feat/pm-flex-routing`, merged as PR #113): the intraday PM is no
  longer blindfolded to macro/news.
  **Phase 4.2** (feed repair) — not started as of 2026-08-27.
- **Phase 5** (short selling) — now a three-stage plan, not the single "roughly
  a day" task this document originally scoped it as. Stage 1 (make shorts
  countable) merged into `main` as PR #116; stages 2 and 3 not started. See
  `docs/WORK.md`'s Phase 5 backlog entry for current status — deploy status
  changes daily and is not tracked here.
- **Phases 6–7** — not started as of 2026-08-27.

The owner reordered Phase 3 ahead of the rest of Phase 2 on 2026-08-27; the
evidence and the decision are recorded in `docs/WORK.md`. The execution-order
diagram at the foot of this document reflects the ORIGINAL dependency analysis
and has not been redrawn.

---

## Phase 2 — Risk-based sizing and correlation-aware budgeting

**Status note (2026-08-27, commit `c89e957`, branch `feat/risk-metrics-and-pm-correlation`, merged and deployed):** landed as "Phase 2a" is the deterministic risk arithmetic this phase depends on — it closes four `AGENT_ROLE_AUDIT.md` audit findings (§1.1–§1.4), not this section's own numbered items:

- The drawdown-halve (audit §1.1) moved from a PM-prompt instruction into `src/risk/rules.py::apply_drawdown_scale`, with `drawdown_buy_cap` as a hard-block backstop. The PM prompt no longer pre-applies it.
- The correlation matrix (audit §1.2) is now built before PM decides (`TradingPipeline._ensure_correlation_matrix`) and PM's Quantitative Facts render the measured clusters (`src/data/correlation.py::correlation_clusters`).
- Portfolio heat (audit §1.3) — budget risk and open risk — is computed in `src/risk/metrics.py` and rendered to PM and RM with headroom under a new `risk.max_portfolio_risk_pct` config field (default 25). **That ceiling is reporting-only** — nothing gates on it yet.
- R-multiple (audit §1.4) is computed against the entry stop and rendered to the Position Reviewer.
- `src/risk/metrics.py`'s budget-risk arithmetic already implements §2.3's release condition below (a stop at or above entry zeroes that position's budget contribution) and the module docstring cites §2.3 directly — but this is exposed only as a reported figure. No gate consumes it, so the book does not yet actually expand/contract on it.

**Phase 2b — COMPLETE (2026-08-27).**

- **§2.1 landed.** `TargetPosition.risk_allocation_pct` (0.5–5.0, owner-ratified envelope) replaces `target_weight_pct` as the live sizing field, and `PortfolioConstructor._plan_risk_targets` derives the weight as `risk_pct x entry / (entry - stop)` — the §2.1 formula with the equity term cancelled, so it needs the stop distance and nothing else. `target_weight_pct` is retained as Optional purely so historical `agent_logs` / `specialist_evidence` rows still parse through `src/replay.py` and the Mission Control API; a target carrying neither field is rejected at the schema. `config/prompts/portfolio_manager.md` now emits risk, and its conviction bands have been through two revisions since: originally 2.0–4.0 / 1.0–2.5 / 0.5–1.0, then recalibrated (2026-08-27, `3dff940` + doc-sync, merged and deployed as PR #113) to 1.5–3.0 / 1.0–2.0 / 0.5–1.0 once the stop-width fix below changed what the 20% single-name ceiling actually leaves — see that entry for why. The formula still reads `risk = min(raw, queued_cap, 5.0)`, pinned by `tests/test_prompts_anchors.py`.
- **§2.2 landed** as `src/risk/budget.py::allocate_risk_budget`, a pure function the constructor calls before sizing anything. Total ceiling 25%, per-cluster cap 40% of it (10% of equity), clusters from measured return correlation rather than a hand-maintained sector table. Rationing is largest-request-first with an alphabetical tie-break, so the outcome never depends on the order the PM listed its targets; a request rationed below the 0.5% floor is denied outright rather than shrunk to a token position. Every cut carries a note into the order's reasoning, because on 2026-08-20 an unexplained deterministic cut read to the AI Risk Manager as "plan inconsistency" and drew a full-plan veto.
- **§2.4 verified already satisfied, and now genuinely so.** A grep of `config/prompts/` and `src/` confirms no fixed position-count target exists anywhere: `PMFacts.position_count` is a reported figure only. That was true before this tranche too — but §2.4 asks for the count to be *determined* by the budget, and until §2.2 was a gate rather than a report, nothing determined it at all. It now falls out of the risk budget: the book expands as stops reach entry and release budget (§2.3, already computed in `src/risk/metrics.py`) and contracts as they do not.

**Two fixes on top of §2.1, found the same day, both merged and deployed 2026-08-27 evening as PR #113:**

- **The 20% single-name ceiling was reachable but unenforced by the constructor (`b712f4c`).** `risk.max_position_pct` sits in the risk engine's `HARD_BLOCK_RULES` — it drops an oversized order outright, it does not trim it. Nothing connected §2.1's sizing to that ceiling, so at this book's real stop distances (median 4.3%) any conviction above roughly 1% risk computed a notional weight the engine would refuse, and the failure mode was a silently empty book, not an error. One combination (4% risk, 3% stop) computed a 133% allocation and raised an uncaught `ValidationError` inside `construct_orders`, taking down order construction for the whole session. The constructor now clamps to the same ceiling the engine enforces (wired from `risk.max_position_pct`, same setting) and states in the order's reasoning that the position therefore risks LESS than the PM allocated — silently under-delivering risk is exactly the gap this closes, and an unexplained deterministic cut is what drew the 2026-08-20 veto.
- **The stops feeding that formula were themselves too tight (`3dff940`).** See the root-cause note under §3.6 — the same 1.7-ATR median stop distance that fires exits inside ordinary volatility also, mechanically, forces the 20% clamp above to bind at nearly every conviction level. `PortfolioConstructor` now widens an entry stop placed inside `risk.min_stop_atr_multiple` ATRs (base 3.0, scaled 0.85x/1.15x by breakout/range setup type and 0.95x/1.10x/1.20x by risk-on/transitional/risk-off macro regime) and rejects the trade if the resulting reward:risk drops below `risk.min_reward_risk_after_widening` (1.5). Measured effect: MSFT 2.4% → 7.0%, VLO 4.5% → 9.2%, OKLO 7.7% → 24.7%, and 0.5/1.0/1.5% conviction now produces 7.1/14.2/20.0% positions instead of clamping all three to 20% — conviction changes size again, the entire premise of §2.1. 2487 tests pass. **This paragraph does not track live deploy status** — see `docs/STATE.md` → "Production position" for how to check what production is actually running.

**The one thing §2.2 does NOT do:** the gate is enforced only when the caller supplies `existing_risk_pct` and `clusters`. `pipeline_stages._book_risk_inputs` supplies both from `ctx.facts` — the same numbers the PM was shown before it decided — and returns `(None, None)` when the facts are unavailable, which leaves the portfolio ceilings unenforced. That is deliberate: enforcing a 25% ceiling against a book that cannot be measured would either block every trade or wave everything through. Per-position sizing and the 5% single-name cap still apply in that state.

**2.1 — Conviction is expressed as risk, not notional.**
The PM currently emits "BUY OKLO 3%" — percent of portfolio, which is risk-blind. A 3% position with a 10% stop risks 0.3% of equity; the same position with a 2% stop risks 0.06%.

Replace with a risk allocation in `[0.5%, 5.0%]` of equity. Share count is then derived:

```
shares = (equity × risk_pct) ÷ |entry_price − stop_price|
```

A wider stop yields a smaller position rather than a rejected trade. This eliminates the entire "stops too tight" failure class — risk is never controlled by squeezing the stop.

**2.2 — Correlation-aware portfolio budget.**
Total at-risk ceiling is **25% of equity**, but correlated names consume **one bet's** budget, not several.
- Cluster by sector *and* thematic exposure. `OKLO / CEG / VST / CCJ` is one nuclear bet. `NVDA / AMD / AVGO / MU / TSM / SMH / SOXX` is one semiconductor bet.
- Budget consumed by a cluster = the **sum** of its members' risk, and a single cluster may not exceed a defined share of the total (suggest 40%).
- Rewards genuine diversification; refuses to pay for fake diversification.

**2.3 — Risk is released, not static.**
Once a position's trailing stop sits **at or above entry**, its risk contribution drops to zero (or negative) and it stops consuming budget. The book therefore expands when trades are working and contracts when they are not — automatically, with nobody choosing a position count.

**2.4 — Retire the fixed position-count concept.** There is no target number of positions. The correlation-aware budget determines it dynamically.

---

## Phase 3 — Exit rework — **COMPLETE, DEPLOYED 2026-08-27**

**Status.** §3.1 and §3.2 landed in PR #107; §3.3 and §3.4 in PR #108; §3.6 and
§3.7 in PR #109. All are deployed to the live paper account at `058273f1`.
§3.5 was **resolved as an owner decision rather than implemented**: its
"weakest model in the stack" premise is contradicted by
`ops/model_policy/results/merged.json`, where `google/gemini-2.5-flash-lite`
scores `quality_min 1.0` at `midday_exit` tied with four other models
including `openai/gpt-5.5` (which costs ~84x more per review), and scores 1.0
on every scenario it was measured on. The `midday_exit` scenario ties five of
twelve candidates at the ceiling and therefore does not discriminate; the
agreed path is to build a scenario that does. §3.8 is unchanged — the reviewer
retains full authority to exit on new information.


This is where the money has been going.

**Superseded by the Status block above.** This note originally tracked 3.1–3.3
as the only landed items with 3.4–3.7 "NOT started"; all of Phase 3 except the
owner-resolved §3.5 has since shipped and deployed (`058273f1`). Left here only
so the per-item detail below still reads as a record of what was fixed and why,
not as a live status line.

**3.1 — Kill the pace feedback loop. (Highest priority defect in the system.) DONE.**
`pipeline.py:6318` computed `pace = progress ÷ (days_held ÷ avg_hold_days)` where `avg_hold_days` was drawn from the system's **own rolling 30-day realized-trade calibration** (currently ≈2.0 days). Selling quickly shrank that figure, which made every position look stalled, which drove more selling. It was a self-tightening noose.

- Replace `avg_hold_days` with the **`expected_horizon_sessions` pinned at entry** from the Tech Analyst (Phase 1.1). **Done** — `expected_horizon_sessions` and `setup_type` are persisted on the `trades` row at BUY time (new columns via `_ensure_column`, `src/storage/db.py`; `insert_trade` takes both as new kwargs); `ExecutionStage` (`src/pipeline_stages.py`) looks them up from `ctx.analyses` to supply them.
- Never derive a trade's expected horizon from the system's own past behaviour. **Done** — the calibration query is deleted from `run_position_review`, and `avg_hold_days` is removed from `_build_position_facts`'s signature entirely (`src/pipeline.py`), so it cannot be reconnected by accident.
- Do not evaluate pace until at least **one third** of the pinned horizon has elapsed. Before that the metric is mathematically meaningless. **Done** — `pace_status="too_early"` below that threshold; no figure is produced.
- Disable progress and pace entirely for Type B (trend) positions. **Done** — `pace_status="n/a_breakout"`.

A fourth state exists beyond this section's original three: `pace_status="unavailable_no_pinned_horizon"`, for positions opened before this landed and carrying no pinned horizon — they get no figure rather than an inferred one. `pace_status="measured"` is the normal case. `config/prompts/position_reviewer.md` renders why pace is absent in each of the three non-measured cases, because a missing number that reads as "nothing to see" is how a day-2 position gets called stalled.

**3.2 — Give the reviewer memory. DONE.**
`_build_own_recent_decisions` (`pipeline.py:6369-6409`) discards HOLDs and never passes prior metric values. The reviewer sold EPD for "not progressing" when progress had risen 16% → 20% and distance-to-stop had *improved*.
- Persist each review's metrics per position. **Done** — `db.save_position_review_metrics` snapshots per-position metrics to `specialist_evidence` (`agent_name="position_reviewer"`, `kind="review_metrics"`).
- Pass the previous snapshot into the next review. **Done** — `db.get_prior_position_review_metrics`, plus new pipeline methods `_build_review_metric_deltas` and `_persist_review_metrics` (`src/pipeline.py`).
- Deterministically reject a deterioration verdict when the deltas are positive. A model may not claim a position is stalling while its own numbers improve. **Done** — new `src/risk/exit_guard.py` (`MetricDeltas`, `compute_deltas`, `is_deterioration_claim`, `veto_contradicted_exit`). `_midday_execute_llm_actions` now takes `metric_deltas` and drops a SELL/REDUCE whose stated reason is a deterioration claim when every metric that moved since the prior review improved, recording `exit_vetoed_contradicts_own_metrics`. Exits on new information (news, earnings, regime shift, correlation breach, a triggered `thesis_invalid_if`) are never vetoed. A mixed picture is deliberately not vetoed either — that stays the reviewer's judgment call.

**3.3 — Close the first-sale-of-the-day loophole. DONE.**
The hard-trigger gate previously applied only to symbols already trimmed that day, so a position's *first* sale executed on soft reasoning unchecked — which is almost every sale. **Done** — commit `2f177e33` (branch `feat/exit-gate-and-risk-routing`): `src/pipeline.py` now runs `_reason_cites_hard_trigger` against every `SELL`/`REDUCE`, not just a symbol's second sell-side action of the day; a non-matching reason is dropped and logged as `exit_blocked_no_named_trigger`, and the position holds, protected by its broker-resident stop. The `_HARD_TRIGGER_KEYWORDS` vocabulary was widened first — macro regime shift ("regime shift"/"regime flip"/"risk-off"), "sector shock", "adverse news", "material news", "earnings miss", "guidance cut" — all sanctioned by §3.8 and previously unrepresented, so gating every exit against the narrower list would have blocked legitimate ones. Concentration and drift were considered and deliberately NOT added: "Concentration drift; valuation stretched" is the verbatim shape of the reason behind the 2026-05-04 AMZN double-trim, and drift trims belong to the Portfolio Manager's rule-priority rows 4-5, not this seat. `config/prompts/position_reviewer.md` states the new scope and full trigger list. 2324 tests pass (2323 before).

**3.4 — Route exits through AI Risk. DONE (PR #108).**
`run_position_review` (`pipeline.py:6412`) called only `position_reviewer`, then executed — violating the `AGENTS.md` contract: `Specialists → Portfolio Manager → AI Risk → deterministic Python → broker`. `_risk_review_exits` now puts every SELL/REDUCE in front of the AI Risk Manager and drops what it rejects. The failure posture is deliberately asymmetric with entries: `RiskStage` fails CLOSED on an unparseable Risk Manager response, but the exit path fails OPEN — failing closed on an exit would leave a thesis-invalidated position unable to close because a language model is unavailable. The deterministic gates (named-trigger requirement, metric-contradiction veto, ATR noise band) are the real protection; every fail-open path logs at ERROR. See `docs/STATE.md` for the full rationale.

**3.5 — Upgrade the reviewer's model.**
`position_reviewer` runs on `google/gemini-2.5-flash-lite` — the weakest model in the stack — while the PM runs GPT-5.5. The consequential, loss-generating decision is on the cheap seat with no second opinion. Move it to a strong model; the cost is negligible against the losses.

> **Correction (2026-08-27, documentation-sync pass).** The "weakest model in the stack" claim above is not supported by the committed benchmark data and must not be read as fact. `ops/model_policy/results/merged.json`, at `position_reviewer`'s own measured scenario (`midday_exit`), scores `google/gemini-2.5-flash-lite` at `quality_min: 1.0` / `quality_mean: 1.0` — tied at the ceiling with four other candidates: `openai/gpt-5.5` (the PM's own model), `deepseek/deepseek-v4-pro-0813`, `qwen/qwen3.7-flash` and `qwen/qwen3-235b-a22b-2507`. It is not a low scorer at this scenario by any measured axis, and it scored 1.0/1.0 on every scenario it was measured on across the whole sweep (`tech_batch`, `macro_stress`, `news_intel`, `pm_constrained`, `risk_rr_breach`, `midday_exit`, `tech_batch_full`). This is the same error `DECISION_CHAIN_AUDIT.md` (F5) caught and corrected for `risk_manager`: reading the model's price, or its place in the routing policy's cost narrative, as a quality finding, when quality was never measured that way. `midday_exit` ties five of the twelve measured candidates at 1.0 and therefore does not discriminate between them on quality — a model could still be moved off this seat for other reasons (independence from the shared specialist model, the discretion this seat carries, or a scenario suite that plainly doesn't discriminate at the ceiling), but not for the reason stated above. §3.5 stays open and undecided; only its stated premise is corrected here.

**3.6 — ATR noise band on exits. DONE (PR #109).**
An adverse move inside ~1 × ATR of entry is noise, not thesis failure. OKLO was sold at 0.67 × ATR. An adverse move inside 1.0× ATR of entry can no longer trigger a price-derived exit; external-information triggers (news, earnings, regime shift, correlation breach, thesis invalidation) bypass the band.

> **Root cause found later, merged and deployed 2026-08-27 evening (`3dff940`, PR #113).** §3.6 gates the *exit* side of the noise band — it stops a stop-hit-adjacent price move from firing a discretionary SELL. It does not touch where the *stop itself* sits. Measured against the book: entry stops were sitting a median 4.3% below entry against a median ATR of 2.56% of price — about 1.7 ATRs, barely past §3.6's own 1.25 ATR "ordinary day's range" threshold, on positions meant to be held for ten sessions. That is the same failure §3.6 was built to catch, one layer earlier: a stop that tight gets tripped by ordinary volatility before §3.6's exit-side gate is ever relevant, because the *broker-resident hard stop* (§3.7) fires first and unconditionally — it has no noise band. It is also, independently, why Phase 2b's single-name clamp (below) was binding on nearly every conviction level: `notional = risk_pct x entry/(entry - stop)` makes a tight stop demand an oversized position for any real risk. `src/portfolio_constructor.py` now pushes an entry stop out to `risk.min_stop_atr_multiple` (base 3.0 ATRs, scaled by setup type and macro regime) when structure placed it inside that floor — never tightening a wide stop — and rejects the trade outright if the resulting reward:risk falls under `risk.min_reward_risk_after_widening` (1.5). Structure still decides the level. **This paragraph does not track live deploy status** — see `docs/STATE.md` → "Production position" for how to check what production is actually running.

**3.7 — Trailing stops, broker-resident. DONE (PR #109, `src/risk/trailing.py`).**
Trailing is arithmetic and belongs in deterministic code, not in an LLM's discretion:
- **Type A:** trail only after the target is exceeded.
- **Type B:** trail from entry — under each successive higher low (structural), with a chandelier stop as fallback where structure is unclear.
- Ratchet upward only, never down. Keep the existing ≥2% ratchet threshold and cooldown.

**3.8 — Exits the reviewer may still make on judgment.** The reviewer retains full authority to exit on **new information**: adverse news, earnings miss, macro regime shift, sector shock, correlation breach, thesis invalidation. Price movement alone is not new information.

---

## Phase 4 — Evidence symmetry and feed repair

**4.1 — Unblindfold the intraday buy path. DONE (2026-08-27, commit `fb88e08`, branch `feat/pm-flex-routing`, merged and deployed as PR #113).**
`portfolio_manager.py` previously hard-returned a technical-only evidence registry whenever `session_type == "intra_check"`, even though the morning's macro was already loaded in memory. The 13:04 reviewer saw macro the 11:02 buyer was denied — which is precisely how OKLO was bought and killed within two hours on evidence that existed at purchase time. Measured over the 10 days to 2026-08-27, this session cost $0.222/run — essentially the same as a full `morning` run ($0.221), ~99% of it the PM call — while deciding on a fraction of the evidence: 33% of all LLM spend on blindfolded scanning.
- **Done, with the remedy narrowed from the original design.** Earnings stay excluded — an intraday filing genuinely has not been read this tick — but today's macro and news are now carried forward from the store (`TradingPipeline._carry_forward_macro` / `_carry_forward_news`) rather than passed by reference from memory. `_carry_forward_macro` refuses anything not dated today (`load_last_state` is not date-scoped, so that check is this method's job); `_carry_forward_news` re-validates the stored dump against the `NewsIntelligenceReport` schema. Both degrade to `None` on any failure — a carry-forward problem costs the tick its context, never the deterministic loss protection that already ran.
- `data_status` now reads `carried_from_morning` for macro/news instead of `not_run_intraday`, so RiskStage's existing 2+-degraded-sources advisory still fires honestly: a 09:30 regime call IS weaker evidence at 14:00, it is simply not NO evidence. `session_type` was removed from `build_evidence_registry` and `validate_grounding` entirely — rather than left inert — so no future reader assumes an intraday tick is still filtered somewhere.
- **Nothing is re-fetched.** The research stack still does not rerun this tick, which is the saving this scan exists to preserve; only what was already on disk is now shown to the PM.
- Fixed a latent crash this carry-forward surfaced: `sector_guidance` has two shapes — the live macro agent emits `[{sector, stance, reason}, ...]`, `MacroStore` persists the normalized `{sector: direction}` form — and the registry iterated the dict shape as a list, so `row.get` raised `AttributeError`. Carrying stored macro forward is what first put that shape in front of the registry; without the fix, every intraday tick would have degraded to no scan at all. Both shapes now resolve; the vocabulary asymmetry they expose (`overweight` vs `bullish` for one macro view) is pinned as a known property, not fixed here.

**4.2 — Repair the data feeds. DONE (2026-08-30, commit `92689e6`, branch `fix/fred-feed-repair`, merged and deployed as PR #162 — news half done earlier via PR #148/#157).**
Production logs show Reuters Business 404, AP Business 403, repeated FRED timeouts, 28 incomplete tech batches, and 11 `Portfolio decision failed deterministic grounding` errors (the PM inventing holdings).
- Replace or re-point the dead news feeds. **Done** — see `docs/AGENT_ROLE_AUDIT.md` §2.2 and `docs/phases.yaml`'s `phase_4` entry.
- Add retry/backoff for FRED. **Done, and rebuilt from what the spec's own text assumed already existed.** The single-retry/flat-2s-backoff policy live at the time this line was written was not enough — it let a three-minute outage on 2026-08-26 fail all nine series in one run. It is now config-driven retries, exponential backoff with jitter, per-request timeouts, and a real wall-clock ceiling (`total_fetch_deadline_s`, 90s default) the old policy never had.
- **Add a feed-health gate:** if macro or news coverage is unavailable, the desk must know it is operating degraded, and that fact must appear in the Telegram alert and the dashboard. **Done for both.** News coverage was wired into `data_status["news"]` earlier; macro coverage now feeds `data_status["macro"]` through the same ok/partial/failed path, and the macro analyst is shown its own coverage directly.
- **Not part of this item's original text, but a real gap surfaced by this work:** there was no macro event calendar (Fed decision / inflation release dates) — the desk answered "is one coming up" from the model's own memory. **Built 2026-08-31** in `src/data/event_calendar.py`, from FRED's free release-dates API: CPI, Employment Situation (NFP), PPI, PCE, GDP, retail sales and jobless claims, threaded into the macro analyst and the Risk Manager with a `MacroCoverage`-shaped coverage line and the same wall-clock-ceiling discipline as the FRED series feed. FOMC meeting dates followed on the same day: FRED cannot supply them (release 101 is a daily release carrying no meeting schedule), so they come from the Federal Reserve's own free calendar — `federalreserve.gov/json/calendar.json` as the structured primary, the rendered `fomccalendars.htm` page as a fallback used only when that feed fails or its schedule stops before the horizon. Disk-cached, with a stale cache served labelled rather than silently, and with "no meeting in this window" printable only when the published schedule spans the window.

---

## Phase 5 — Short selling

Owner-authorized. Currently **absent** from `src/execution/broker.py` — not disabled, not implemented. `allow_margin: false` is unrelated (it governs buying beyond cash).

Now a three-stage plan — see `docs/WORK.md` Phase 5 backlog entry for current
status (merge/deploy status is not tracked here — it changes daily).

- **No owner action outstanding.** The account was verified on 2026-08-28 as
  margin-enabled: `shorting_enabled: True`, `multiplier: 4`, equity
  $9,871.87. This corrects the line previously here asking the owner to
  switch the account to margin.
- Implement short entry/exit in the broker layer.
- Add borrow-availability and hard-to-borrow checks before submitting.
- **New risk math:** a short's loss is unbounded. Deterministic risk must treat shorts differently from longs — a stop is mandatory, gap risk is materially worse, and the position must be force-covered on breach.
- Teach the PM that the inverse ETFs already in the universe (`SH`, `SDS`, `PSQ`, `SQQQ`) are bearish instruments. Nothing in the codebase currently conveys this, so even the previously "sanctioned" bearish mechanism was never wired up.
- Short candidates flow through the same Specialists → PM → AI Risk → deterministic chain.

---

## Phase 6 — Cost circuit and transparency

**6.1 — Dollar-based budget with an afternoon reserve.**
`max_paid_sessions_per_mode_per_day: 2` tripped at 12:00 ET with **$0.59 of the $1.50 daily budget unused**, leaving four scheduled afternoon slots structurally dead.
- Cap on **dollars**, not session count.
- Reserve capacity for the afternoon so a quiet morning cannot consume the day.
- Suppress repeat suspension alerts — one notice, then silence until state changes.

*Note: prompt caching is deliberately disabled for a legitimate documented reason (it would make the circuit's pre-call reservations incomparable to actual spend) and would save essentially nothing here — one PM call per session, sessions 30 minutes apart, caches live ~5–10 minutes. Leave it off.*

**6.2 — Make the dashboard answer "why."**
The reasoning is already persisted; it is simply never shown.
- Add a `position_id` to `trades`, assigned when a BUY opens a flat position and inherited by every subsequent SELL / REDUCE / TRAIL_STOP against it until flat. Currently nothing links a buy to the sell that closed it.
- Add `GET /positions/{position_id}/history` returning the ordered chain: entry thesis, stop, each interim review decision, exit reason, realized P&L, hold duration.
- Render `reasoning` in the trades table (it is fetched and discarded today).
- Fix `PositionsPanel` so clicking a position opens its **entry** thesis, not today's re-evaluation.
- Add an `exit_reason_category` so exits are classifiable rather than free text.

**6.3 — Report the real number.** Telegram showed −$1.22 at 15:00 ET on 2026-08-26; the day actually closed **−$46.58**. The alert stream must carry a true end-of-day figure, and P&L should be expressed against risk capital as well as total equity.

---

## Phase 7 — Measurement

**Status note (2026-08-30):** §7.1 (backtester) and §7.3 (edge numbers) are DONE. §7.2 has two distinct halves: (a) the **logging** half (pin each trade's allocated risk, conviction, model at entry; label exit decision_id status) is DONE as of PR #159, and (b) the **analysis** half (grade whether stated conviction predicts outcomes) is NOT DONE and is data-constrained, not code-constrained — only 8 closed round trips exist in all live history, far too few for statistical claims.

**7.1 — Build a backtester.** `src/replay.py` replays past decisions; it is not a strategy backtest. Without one, every change is a guess evaluated against one noisy live day, and a bad change is indistinguishable from a bad week. 419 commits in 17 days were merged into that blindness. **DONE** — `src/backtest/engine.py` simulates the deterministic layer against real history, entering on next day's open, producing deterministic reproducible results.

**7.2 — Calibrate conviction.** Log each trade's allocated risk against its realized outcome. If the desk's conviction predicts results, conviction-weighted sizing amplifies the edge. If it does not, flat sizing is superior — and that must be discovered from data, not assumed. **Logging half DONE (PR #159)** — each trade carries conviction, requested_risk_pct, allocated_risk_pct, decision_model at entry; exits label whether they link to an originating decision. **Analysis half NOT DONE** — only 8 closed round trips exist in production history, insufficient for statistical work.

**7.3 — Track the edge directly.** Surface win rate, average win ÷ average loss, expectancy per trade, and average hold duration. The edge claim in §0 is a hypothesis until these numbers confirm it. **DONE** — `compute_trade_calibration` surfaces win rate, avg_win_loss_ratio, expectancy_pct, avg_hold_days for both long and short sides separately.

---

## Phase 8 — Documentation correction

- Rewrite `docs/OUTCOME.md` to the mandate in §0. The current "research experiment" framing is the root cause of the 99%-cash, 3%-starter-position behaviour — the system was built correctly for the wrong brief.
- Amend `docs/STATE.md`: remove the claim that the full chain applies to exits (it does not, until Phase 3.4), remove health claims contradicted by the failing feeds, and correct the "exactly one config delta" statement.
- Amend `AGENTS.md` with the Phase 0.2 ratification rule.
- Repo hygiene, low priority: delete the 83 merged-but-undeleted branches; triage the 26 orphans, rescuing the two abandoned VPS-security branches.

---

## Phase 9 — The research desk actually deliberates

**Status note (2026-08-31):** §9.1/§9.2 (any seat may nominate, Technical becomes a responder) are DONE and deployed (PR #153). §9.3 (disagreement adjudication) and §9.4 (sizing by agreement count) are NOW DONE and deployed (PR #160, merged during this audit window). §9.5 (conviction ledger per candidate) is **owner-ratified scheduled work, PARTIALLY BUILT** — its recording and scoring layer landed on `feat/conviction-ledger-recording`; its operator-facing view has not been built. See §9.5 below for exactly which parts exist.

*An earlier version of this note recorded §9.5 as "deliberately not built" with no attribution and no stated reason — an engineering scope decision written as though it were settled truth, the exact pattern AGENTS.md's Governance ratification section exists to prevent. The owner reopened it on 2026-08-31 and ratified the full specification below. Corrected here rather than carried forward.*

**Owner-directed, 2026-08-27.** Rex, verbatim: *"We have agents doing research
and analysis. If something has high conviction or strong candidacy it should
be debated amongst all the agents. We're trying to create a trading desk with
synergy, not a technical analysis trade bot."*

### What the system does today

Evidence from the 2026-08-27 morning run. Ten Portfolio Manager targets,
every one of them carrying `technical=buy/supports` as its **only** supporting
source. News contributed to exactly one target, as neutral context. Smart
Money contributed to none. Four of the ten were bought while
`macro=underweight/conflicts` — the conflict was recorded and then ignored.

The cause is structural, not a tuning problem. `validate_grounding`
(`src/agents/portfolio_manager.py`) rejects any increase where
`symbol not in {analysis.symbol for analysis in analyses}` — **a current-run
Technical analysis is a hard gate on candidacy.** The funnel is
`universe → prefilter → Technical → PM`, so News, Earnings, Macro and Smart
Money are all *filters applied to a list Technical already chose*. A name with
a blowout earnings beat and cluster insider buying cannot be bought unless
Technical happened to analyse and rate it. The other seats cannot bring
anything to the table; they can only nod at what is already on it.

That is a technical-analysis bot with commentary attached, which is precisely
what the owner does not want.

### 9.1 — Any seat may NOMINATE a candidate

Each research seat gains a bounded nomination output: symbol, conviction, and
the specific observation behind it. News nominates on a genuine catalyst;
Earnings on a filing that materially changes the picture; Smart Money on
clustered insider buying (this lane half-exists already and is capped at three
per run — generalise that pattern rather than inventing a new one); Macro
nominates sector leaders when a regime turns.

Cap nominations per seat per run. The cap is what keeps this bounded and
affordable, and the existing SEC Form 4 admission lane is the working
precedent for how to do it safely.

### 9.2 — Technical becomes a RESPONDER, not the gatekeeper

Invert the funnel. A nomination triggers an on-demand Technical analysis of
that symbol. Technical stays **mandatory** — Phase 1's contract is
non-negotiable, no trade without structural levels, a stop and a target — but
it stops being the thing that *originates* every idea. It answers "here is a
name the desk is interested in; is there a tradeable setup, and where are the
levels?"

This is the whole architectural change. Everything else follows from it.

Cost is bounded and modest: seats already do the analysis that produces a
nomination, so nomination itself is free — it is structured output from work
already paid for. Only the responding Technical call is new spend, and it is
capped by 9.1.

### 9.3 — Disagreement must be adjudicated, not merely logged

Today a conflicting macro stance is recorded in `provenance` as
`conflicts` and the trade proceeds unchanged. That is a record of a
disagreement, not a resolution of one.

Where a material conflict exists between seats, the PM must address that
specific conflict in `signal_conflicts` and name why it is overridden. A
target carrying a `conflicts` provenance entry with no corresponding
adjudication should fail grounding, the same way an unsourced claim does.

### 9.4 — Conviction is agreement, not a technical rating

The mandate is **breadth × consistency × asymmetry**. Sizing should follow the
number of *independent* sources that agree, not the strength of one seat's
rating. A name confirmed by Technical, Earnings and Smart Money is a different
bet from a name only Technical likes, and today they size identically at 3-5%.

This lands naturally on top of Phase 2b, which replaces notional conviction
sizing with risk allocation: multi-source agreement earns a larger share of
the risk budget.

### 9.5 — A conviction ledger per candidate

**Status (owner-ratified 2026-08-31): specified, scheduled work. PARTIALLY
BUILT — the recording and scoring data layer landed on
`feat/conviction-ledger-recording` (PR #196), and the read side (one read-only
API route plus the Mission Control panel) landed on
`feat/analyst-scorecard-panel`.** Items 2, 4, 5 and 7 remain unbuilt. Two
owner amendments are recorded inline on 2026-08-31: **item 3a** (credit is raw
signed R — the conviction weight in item 3's original wording was removed, and
a per-declared-confidence breakdown replaced it) and **item 3b** (shorts are
chained, scored, signed and worded identically to longs). The
per-item implementation status is item 11 at the end of this section, and item
10's "missing" list has been corrected to match. Everything between here and
item 10 is the specification as ratified, unchanged.

`docs/phases.yaml` had recorded this as "deliberately NOT built," with no
attribution and no stated reason — the exact pattern `AGENTS.md`'s
"Governance ratification" section exists to prevent: an engineering scope cut
recorded as though it were settled, with nothing to show it was ever a
decision rather than an omission. The owner reopened it on 2026-08-31. What
follows is the specification; nothing below is built yet.

Note the name collision before anything else: `src/storage/db.py` already
has a `_CONVICTION_OUTCOME_MIN_N` constant and a `trades.conviction` /
`requested_risk_pct` / `allocated_risk_pct` / `decision_model` column set,
both labelled "conviction ledger" in their own comments — but that is
**§7.2's** ledger, a single PM-assigned conviction value per *trade*, graded
against that trade's own outcome. **This section is a different thing**: a
per-*seat*, per-*idea* record, tracking every seat that took a position on a
symbol, not just the one conviction value the PM finally acted on. The two
should not be conflated; §9.5 does not replace or modify §7.2's ledger, and
item 8 below states exactly where the two touch.

**1. What is recorded.**

Per idea (symbol), per session, one row per seat that took a position on it:
which seat, its stance (long/short/pass) and declared conviction
(low/medium/high — `Nomination.conviction`'s existing scale), its stated
reason, whether it originated the idea or is responding to a seat that
already tabled it, and the session date.

The original wording for this section said the ledger tracks "what changed
since the previous session." That line is easy to read past, but it is the
point: this is **not a per-run snapshot**. A seat's stance on a symbol it
already has a position on can strengthen, soften, or reverse from one session
to the next, and that trajectory — not any single session's snapshot — is
what makes a seat's judgement legible. The ledger is longitudinal per idea by
design, not a table refreshed and discarded every morning.

**2. Two skills, scored separately.**

*Origination* — the seat that first tabled the symbol — and *judgement* —
any seat (including the originator, on a later session) taking a position on
an idea someone else already put on the table — are scored independently. A
seat can be a sharp originator and a weak judge of others' ideas, or the
reverse; collapsing the two into one number hides which skill is which.

**3. Scoring. (Amended by the owner, 2026-08-31 — see item 3a.)**

On position close, the realized outcome in R
(`src/risk/metrics.py::r_multiple`) is credited backwards to every seat that
took a stance on that idea: seats whose stance aligned with the direction
actually taken score **+R**, seats opposed score **−R**. A seat that argued
against a trade that went on to lose therefore gains from having been right
to dissent — the ledger scores the call, not the trade.

*The original wording of this item ended: "Each seat's score is weighted by
its own declared conviction on that call, so a loud wrong call costs more than
a quiet one, and a loud right call earns more than a quiet one." The owner
reversed that on 2026-08-31. Struck here rather than deleted, because item 3a
is only legible against what it replaced.*

**3a. No conviction weighting. Credit is raw signed R. (Owner decision,
2026-08-31 — reverses item 3's original last sentence.)**

Credit is `+R` for a supporter and `−R` for an opposer, and nothing multiplies
it. The declared conviction is still recorded on every credit row; it simply
scales nothing.

*Rationale (owner), both halves of which are recorded in
`src/conviction_ledger.py` so nobody reinstates the weight from this document
alone:*

1. **It is circular.** The ledger exists to discover whether an analyst's
   declared confidence predicts anything. Multiplying its credit by its own
   confidence assumes that answer and bakes it into the measurement. This desk
   has already observed high-conviction trades underperforming low-conviction
   ones at small sample (see `_CONVICTION_OUTCOME_MIN_N` in
   `src/storage/db.py` and §7.2); under weighting, that finding would have
   been hidden inside the score.
2. **It double-counts.** A confident call already earns a larger position
   through the §9.4 agreement ceiling, and a larger position already produces
   a proportionally larger R. Weighting the credit again charges confidence a
   second time for the same fact.

*What replaces it.* Each analyst's record is broken down **by the confidence
it declared**: resolved calls, calls right, average win, average loss and
cumulative total, separately for each level that analyst used
(`SeatRecord.by_confidence`, `AnalystScorecardItem.by_confidence`, and the
"When it said it was sure, and when it hedged" section of the panel). "Does
this analyst's high confidence earn more?" becomes something a reader can see
rather than something the code asserts.

*Reinstating a weight later.* A confidence weight could legitimately be
introduced, but **only one derived from an analyst's own measured history —
never one chosen up front**. Deriving it requires this breakdown to exist and
to have accumulated a real sample first, which is precisely why the breakdown
is the replacement rather than a smaller multiplier.

*Already-written rows.* Credit rows persisted before 2026-08-31 stored a
weighted `credit`. Nothing migrates them. Both read paths
(`Database.get_conviction_credits` and `db_reads.get_conviction_ledger`)
recompute `credit` from the stored `r_multiple` and `side`, which is exact
because `r_multiple` was always persisted unweighted — so old and new rows
mean the same thing and no series ever mixes two scales. The stored `credit`
and the historical `weight` key are ignored on read; `weight` is no longer
written, and is gone from `SeatCredit` and from the API response.

**3b. Shorts are scored identically to longs. (Owner decision, 2026-08-31.)**

A trade that made money is a **win** and a **positive** number; one that lost
money is a **loss** and a **negative** number — whichever direction it was
taken in. An analyst that argued for a profitable short is credited; one that
argued against it is charged. Exactly as for a long.

**Nothing is inverted, negated or specially-cased for direction anywhere a
human reads it.** Direction affects only how profit is computed from prices —
`src/risk/metrics.py::r_multiple` takes the side from a signed `qty`, so a
short arrives at the ledger already carrying the right sign — and never the
sign convention, the wording, the colour, or which side of zero a figure lands
on. The panel describes both directions in one sentence whose only difference
is the verb ("the desk bet it would rise" / "…fall").

*What actually had to change.* `_assign_position_ids` (§6.2a,
`src/storage/db.py`) opened a chain only on a `BUY`, so no `SHORT` ever
received a `position_id` and no short round trip existed for this ledger to
score. A `SHORT` now mints a chain and the `COVER` family
(`COVER`/`PARTIAL_COVER*`/`EMERGENCY_COVER`) retires it, mirroring
`BUY`/`SELL`; stops, trails, take-profits and de-levers retire either side. A
long-side exit against an open short chain (or the reverse) is left
unattached rather than allowed to close the wrong position, and an entry on
the opposite side of an already-open chain is passed through untouched — both
are malformed histories this desk cannot produce, and neither is guessed at.

**This changed no trading behaviour.** `position_id` is a forensic column: no
module outside `src/storage/db.py` and the read-only `src/api/` package names
it, and nothing in the decision chain reads it, the ledger, or any credit row.
`tests/test_conviction_ledger.py::test_short_chaining_touches_no_trading_decision`
runs `DecisionStage` against a database that does and does not already hold a
scored short round trip and requires byte-identical constructed orders.

**4. Unconverted nominations are not scored, but tracked.**

An idea that never became a trade has no outcome and gets no verdict — there
is nothing in R to credit. It is not silently dropped, though: conversion
rate (nominations that became a trade ÷ nominations made) is tracked and
shown per seat, alongside the scored record. A seat that nominates constantly
but rarely survives to a trade is a different seat from one that nominates
rarely but almost always converts, and the ledger should show that
difference even though neither number is a win/loss score.

**5. Shadowed objections. (Owner decision, 2026-08-31.)**

When a seat's objection blocks a trade outright, there is no real market
outcome to grade the objection against. The trade is *shadowed* instead:
tracked as if it had been taken, at the price and structural levels the
desk would have used, so the blocking seat's objection is scored against
what actually happened to the position it prevented.

*Rationale (owner):* without this, the cheapest way for any seat to look
good on this ledger is to block everything. A seat that only ever votes no
would otherwise be unscoreable — never wrong, because never in a trade — and
that is a worse incentive than the one the ledger is trying to fix.

**6. Advisory only. (Owner decision, 2026-08-31.)**

No score produced by this ledger feeds back into position sizing, in this
build, under any circumstance.

*Rationale (owner):* a leaderboard that reallocates risk starts grading
itself on trades it caused. A seat upweighted because it scored well starts
influencing the very trades that determine whether it keeps scoring well —
the measurement poisons itself the moment it has power over the thing it is
measuring. This ledger stays read-only until that circularity has a real
answer, which is not attempted here.

**7. Decay. (Owner decision, 2026-08-31.)**

Scores are computed over a rolling window, not a lifetime record, so a seat
is judged on the regime it is trading now rather than carrying a call from
eight months ago at equal weight to one from last week.

*Tension to flag explicitly, per the owner:* decay too aggressively and no
seat ever accumulates enough resolved calls inside the window to be
statistically readable — the same small-n problem §7.2 already hit at 8
closed round trips (see the `_CONVICTION_OUTCOME_MIN_N` comment in
`src/storage/db.py`). The window length is not fixed by this spec; whoever
implements this has to pick a value that survives that tension, not assume
one exists by default.

**8. No arbitrary sample gate. (Owner decision, 2026-08-31 — reverses earlier
design.)**

An earlier version of this plan gated any displayed score behind 20 resolved
calls per seat — the threshold `src/storage/db.py::_CONVICTION_OUTCOME_MIN_N`
already uses to gate §7.2's `by_conviction` / `by_allocated_risk` outcome
buckets. The owner rejected the gate as arbitrary: a reader can tell the
difference between "3 of 3" and "25 of 30" without being told which one is
allowed to be shown. **Show the raw counts (n resolved, n scored) alongside
every figure; never hide a score behind a threshold.**

This is a deliberate, narrow reversal, and it does not touch
`_CONVICTION_OUTCOME_MIN_N` itself. That constant keeps its existing job
exactly as-is: gating whether §7.2's `compute_trade_calibration` states a
win rate / average return for a *conviction bucket across the whole book* (a
claim strong enough to justify changing how much risk the desk allocates, if
it were ever wired to sizing — which per item 6 above, this ledger's
per-seat score will not be). The two ledgers are answering different
questions at different resolutions, and only one of them is gated. Anyone
extending §7.2's aggregate claims should keep using the floor there; this
section's per-seat, raw-count display is the only thing exempted.

**9. Presentation — a recommendation, not a decision.**

Five encodings were prototyped for showing one seat's record at a glance and
rejected. Recording why, so nobody re-discovers the same dead ends:

- **(a) A diverging bar-per-call strip** (one bar per resolved call, signed
  by outcome) — shows the sequence of calls but hides the total at a glance.
- **(b) A stacked distribution / dot plot** — shows the spread of outcomes
  but hides which order they happened in.
- **(c) A hard-edged confidence-interval bar** — Correll & Gleicher (IEEE
  TVCG 2014,
  https://graphics.cs.wisc.edu/Papers/2014/CG14/Preprint.pdf) show that a
  hard interval edge causes readers to systematically over-trust values
  just inside the bar ("within-the-bar" overconfidence) — the wrong failure
  mode for a metric meant to be read skeptically.
- **(d) A variable-width "tug of war" bar** (width/area encoding conviction
  or magnitude) — width and area are weak perceptual channels next to
  position and length (Cleveland & McGill's ranking of perceptual
  accuracy), which is why differences in block size did not actually read
  to a viewer.
- **(e) Any single mark trying to combine frequency and magnitude into one
  number** — collapsing "how often" and "by how much" loses exactly the
  distinction item 2 above depends on (origination vs. judgement are
  different skills; a single score cannot show both were measured
  separately).

**Recommendation:** a **bullet graph** (Stephen Few — designed for exactly
this: a compact, dashboard-row-sized metric) carrying magnitude as
**length from a baseline** (cumulative R, or average R per call), paired
with a **separate small glyph** for hit rate (calls scored positive ÷ calls
scored) rather than folding the two together.

*Justification:* this is a convergent convention, not a personal
preference. Trading journals separate win% from average-win-vs-average-loss.
Sports tipster tables separate strike rate from ROI. Forecasting scorers
(Brier score) separate calibration from sharpness. Three independent
domains that all face this exact "frequency vs. magnitude" problem
independently refuse to collapse it into one number — that convergence is
the strongest evidence available that two channels, not one, is correct.

*Binding accessibility constraint:* the owner has red-green color
blindness. Meaning must never rest on hue alone anywhere in this
presentation — encode sign and category with position, length, shape, or an
explicit +/− glyph, and use color only as reinforcement on top of one of
those, never as the sole carrier.

This section is a recommendation for whoever builds the Mission Control view,
not a locked decision — unlike items 5–8, it carries no owner ratification,
because no design has been shown to the owner yet.

**10. What already exists versus what must be built.**

Verified by reading the current code, not assumed:

*Already exists:*

- **Nomination capture.** Every seat's raw nomination (symbol, conviction,
  observation) is already captured, per seat, before capping/dedup
  (`src/nominations.py::_collect_seat_nominations` is called from
  `src/pipeline_stages.py::_run_nomination_responder_pass`), and is already
  **persisted** — one `specialist_evidence` row per nomination, kind
  `pipeline_event`, `outcome="nominated"`, carrying `seat`, `conviction` and
  `observation` in its JSON payload, scoped to that symbol
  (`src/pipeline_stages.py::_record_pipeline_event` /
  `Database.insert_specialist_evidence`). It is even already surfaced
  read-only through Mission Control's research/evidence API
  (`src/api/routes_evidence.py`, `src/api/routes_research.py`) as a per-run
  event timeline. **This corrects the premise that nominator identity is
  computed and then discarded — it reaches `src/storage/db.py` today.** What
  it is NOT, today, is a ledger: it is one forensic log line per event, per
  run, with no aggregation, no cross-session view, and (see below) no link
  to what the trade did.
- **Per-seat, per-symbol stance for a session.** Each seat's own structured
  analysis for a symbol is independently persisted
  (`specialist_evidence`, kind `analysis`) and is the exact registry
  `src/risk/rules.py::count_aligned_sources` reads to compute how many seats
  agree. This is effectively the raw material for "who confirmed, who
  dissented" — but nothing marks an analysis row as a response *to a
  specific nomination*, so that relationship has to be reconstructed, not
  read.
- **Seat alignment counting.** `count_aligned_sources` and
  `agreement_ceiling_for_count` (`src/risk/rules.py`) already turn per-seat
  stances into a deterministic agreement count and a sizing ceiling (§9.4,
  shipped in PR #160).
- **R computation.** `src/risk/metrics.py::r_multiple` already turns
  current price, entry and initial stop into a signed R-multiple for either
  side (long or short), which is exactly the scoring unit item 3 needs.
- **Per-trade risk fields.** The `trades` table already carries `conviction`,
  `requested_risk_pct`, `allocated_risk_pct` and `decision_model` pinned at
  entry, and exits are already labelled `linked` /
  `no_originating_decision` against their originating decision
  (`src/storage/db.py`, §7.2). That is the single-conviction-per-trade
  ledger, distinct from this one (see the note above item 1), but its
  `decision_id` linking pattern is the precedent to reuse.

*Missing — has to be built:*

*(Corrected against the tree after `feat/conviction-ledger-recording`. The
first two bullets below were written before that branch and are now done;
they are kept, struck through in prose, because the diagnosis they record is
what the fix was built from.)*

- ~~**Persistence of nominator/dissenter identity as a structured, joinable
  record**~~ — **DONE.** The diagnosis stands and was verified against the
  code: nomination `pipeline_event` rows were written with
  `decision_id=None` because `RunContext.decision_id`
  (`src/pipeline_context.py`) is not assigned until `DecisionStage`
  (`src/pipeline_stages.py`), which runs *after*
  `_run_nomination_responder_pass` has already written those rows.
  `Database.link_nominations_to_decision` now back-fills the id onto them
  once `DecisionStage` mints it, and `Database.record_seat_stances` writes
  one `seat_stance` row per (idea, seat) — dissent as well as support.
- ~~**The join from ledger to trade outcome.**~~ — **DONE.**
  `Database.resolve_conviction_ledger` walks each closed position chain to
  its realized R (`r_multiple`) and writes a scored verdict back per seat.
- **The longitudinal per-idea view.** Still missing. Everything persisted is
  scoped to one run/decision. Nothing aggregates a symbol's
  nomination/stance history *across* sessions, which is what item 1's "what
  changed since the previous session" requires.
- **Origination vs. judgement scoring** (item 2), **the conversion-rate
  metric** (item 4), **the shadowed-objection tracking** (item 5), **the
  decay window** (item 7), and **the presentation layer** (item 9). Still
  missing. One qualification on item 2: whether a seat originated an idea or
  responded to one is now recorded on every stance and credit row
  (`nominated`), so the split is derivable — but nothing computes the two
  scores separately, and `aggregate_seat_records` returns one record per
  seat.

**11. Implementation status, per item (as of `feat/conviction-ledger-recording`).**

The scope of that branch was deliberately the data layer only: no UI, no API
route, no frontend, and no change to sizing or to what gets traded.

| Item | Status | Where |
|---|---|---|
| 1 — what is recorded | **Partial** — seat, stance, declared conviction, originated-vs-responding, stated reason and session date are recorded per idea per decision. The **longitudinal** half (cross-session trajectory) is not built. | `Database.record_seat_stances`, `seat_stance` evidence rows |
| 2 — origination vs judgement scored separately | **Not built.** The `nominated` flag is persisted on every stance and credit row so the two can be separated later; nothing scores them apart today. | — |
| 3 — scoring on close | **Built.** +R aligned / −R opposed via the existing `r_multiple` against the stop the position was OPENED with. Idempotent; a chain with no entry stop is counted and skipped, never scored against a fabricated denominator. | `Database.resolve_conviction_ledger`, `src/conviction_ledger.py::score_position` |
| 3a — no conviction weighting | **Built (2026-08-31).** Credit is raw signed R; `conviction_weight()` and `CONVICTION_WEIGHT` are gone and `SeatCredit.weight` with them. Pre-2026-08-31 weighted rows are read back unweighted, recomputed from the stored `r_multiple` and `side` rather than migrated. | `src/conviction_ledger.py`, `Database.get_conviction_credits`, `src/api/db_reads.py::get_conviction_ledger` |
| 3a — per-confidence breakdown | **Built (2026-08-31).** Each analyst's resolved calls, calls right, average win, average loss and cumulative total, split by the confidence it declared. Sums back to its own totals; only levels it actually used appear. | `SeatRecord.by_confidence`, `AnalystScorecardItem.by_confidence`, `AnalystDetail.tsx` |
| 3b — shorts scored like longs | **Built (2026-08-31).** A SHORT mints a position chain and the COVER family retires it, mirroring BUY/SELL. A profitable short is a positive number and a win for its backers; nothing is inverted for direction anywhere a human reads it. | `_assign_position_ids` / `_exit_action_side` (`src/storage/db.py`) |
| 4 — unconverted nominations tracked, conversion rate | **Not built.** The join now makes conversion rate computable (a nomination row either does or does not have a `trades` row on its `decision_id` + symbol); no function computes it. | — |
| 5 — shadowed objections | **Not built.** | — |
| 6 — advisory only | **Built and enforced.** Nothing in the trading chain reads any ledger row; every write is best-effort. `tests/test_conviction_ledger.py::test_ledger_recording_does_not_change_a_single_trading_decision` runs `DecisionStage` with recording live and disabled and requires the constructed orders to serialize byte-identically. | `src/pipeline_stages.py::_link_nominations_to_decision` / `_record_seat_stances` |
| 7 — decay / rolling window | **Not built.** The record is lifetime. | — |
| 8 — no arbitrary sample gate | **Built.** `aggregate_seat_records` returns raw `resolved_calls` and `calls_right` with no threshold anywhere, and so does every per-confidence row. | `src/conviction_ledger.py` |
| 9 — presentation | **Built**, as a Mission Control view rather than the bullet graph item 9 recommended — see item 12. Raw counts always accompany any percentage, and no meaning rests on hue alone. | `GET /analysts/scorecard`, `frontend/src/components/scorecard/` |
| 10 — the join | **Built.** See the corrected list above. | `Database.link_nominations_to_decision` |

Additionally built, as the read side items 2/4/9 will need: a per-analyst
aggregate — `src/conviction_ledger.py::aggregate_seat_records` returns, per
seat, resolved calls, calls right, average win, average loss, the cumulative
profit series over time, and current drawdown from that seat's own peak. Pure
logic, no I/O, same posture as `src/nominations.py`.

**12. The read side (`feat/analyst-scorecard-panel`).**

One read-only endpoint and one Mission Control view. No write path, no change
to sizing, no change to what gets traded — item 6 holds unchanged.

- **`GET /analysts/scorecard`** (`src/api/routes_scorecard.py`,
  `AnalystScorecardResponse` in `src/api/schemas.py`). Reads the persisted
  `conviction_credit` and `seat_stance` evidence rows through
  `db_reads.get_conviction_ledger()` — an independent SQLite `mode=ro`
  connection, like every other Mission Control read. It **scores nothing**:
  no `r_multiple` is computed and no stance is re-classified as supporting or
  opposing. It sums into per-analyst totals, a running series, a peak, a
  distance below that peak, monthly buckets and a per-declared-confidence
  split. `credit` is the one value it derives — `±r_multiple` by `side` —
  which is what lets a pre-2026-08-31 weighted row read back on the same
  scale as a new one without a migration (item 3a).
- **It does not import `src.conviction_ledger`**, even though
  `aggregate_seat_records` produces the same per-analyst totals, because that
  module imports `src.risk.rules` and `tests/test_api_safety.py` forbids any
  `src.risk` import from `src/api/`. The aggregation is therefore mirrored in
  the route, and `tests/test_api_scorecard.py::test_projection_matches_the_ledgers_own_aggregate_when_it_is_available`
  compares the two implementations against identical input so they cannot
  drift apart. This duplication is a deliberate cost of the isolation
  contract, not an oversight.
- **The panel** (`frontend/src/components/scorecard/`, reachable from the
  cockpit's top nav as "Analyst Scorecard"). Four sections: two slope panels
  comparing every analyst at the earliest and latest month on accuracy and on
  money; a ranked table; one analyst opened (running-profit chart, a strip
  showing how far below its own best it has fallen and for how long, its
  record split by how confidently it spoke, and a month-by-month waterfall);
  and one closed idea traced back to everyone who took a side on it. Drawn with `lightweight-charts`, `recharts` used
  directly, plain SVG with `d3-scale`/`d3-shape`, and `@xyflow/react` — no new
  dependency, and deliberately not Tremor.
- **Item 9's bullet-graph recommendation was not followed**, and the
  recommendation itself said it was not a ratified decision. What it was
  protecting — never collapsing "how often right" and "by how much" into one
  mark — is preserved: the two are separate panels in section 1 and separate
  columns in the table. The binding accessibility constraint is met by
  position against a drawn zero line, explicit + / − signs, ▲ / ▼ glyphs and
  solid-versus-outlined shapes; colour only ever repeats one of those.
- **Plain language is enforced by test.** The panel defines every term on the
  page before using it, states the $100-at-risk convention, that nothing
  compounds and nothing expires, that how confidently an analyst spoke does
  not change what a call is worth, that a bet on a share falling counts
  exactly the same way as any other trade, that no score changes how much
  money any trade gets, and that no minimum call count hides anything.
  `frontend/src/components/scorecard/AnalystScorecard.test.tsx` asserts that
  "R-multiple", "seat", "payoff ratio", "expectancy", "drawdown" and
  "conviction-weighted" appear nowhere in what renders.
- **Example data, clearly labelled.** Until a position closes and is scored
  the ledger is empty, so the panel renders a committed fixture
  (`frontend/src/fixtures/analystScorecard.ts`) behind a permanent
  "Example data — not real" banner, and switches to the live record the moment
  the endpoint returns scored calls. The fixture is never merged into a live
  response, and the API never fabricates a row.

**That gap is now closed (2026-08-31).** Short round trips were previously
unscored because they carried no `position_id` to chain on —
`_assign_position_ids` (§6.2a) opened a chain only on a `BUY`. A `SHORT` now
mints a chain and the `COVER` family retires it, and the ledger's arithmetic
scored them unchanged the moment they had chains, exactly as this note
predicted. See item 3b above for the sign convention, which is the part that
mattered: a short is a win or a loss on the same side of zero as a long.

### Sequencing

Phase 9 depends on Phase 2b, because "agreement earns size" is meaningless
until size is expressed as risk. Build 2b first.

---

## Phase 10 — One voice must never veto the book (owner-ratified 2026-09-01)

**Status: ratified by Rex on 2026-09-01, not yet implemented.** Four decisions,
all made by the owner in conversation, all pointing at the same defect: the
pipeline repeatedly turns a judgement that should ADJUST a trade into a binary
that BLOCKS it.

**The evidence that produced them.** Morning run `run-64290730`, 2026-09-01.
59 symbols analysed, 38 actionable, **zero trades**. Reconstructed from the
run funnel:

- **30 of 38 actionable signals (79%) scored below the 1.5 R/R floor** and were
  untradeable before any human judgement applied — including the two
  highest-conviction calls of the day, SLB `strong_buy/high` at 1.28 and AGX
  `sell/high` at 0.84.
- Of the 8 that cleared the floor, **5 were shorts** (NKE 2.28, GEV 2.12, UNH
  1.90, NEE 1.84, FLNC 1.84). The PM proposed **none** of them and put up two
  longs instead. Macro had called the day `risk-on / bullish / high`. The market
  closed the session down. 15 bearish candidates reached
  `technical_analysis_validated` and every one died at
  `candidate_not_selected_for_target`.
- Risk rejected the whole plan naming **XLE only** (constructed R/R 1.18).
  **CHPX went down with it at R/R 3.03**, comfortably inside the floor, on an
  unrelated technical thesis in a different sector.

### 10.1 The risk verdict becomes per-trade, judged against the live portfolio

`RiskVerdict.approved` is a single `bool` for the entire plan.
`RiskModification` can retune a symbol's fields but cannot reject one symbol.
So one failing leg kills every other leg in the same run.

Change the verdict to carry a per-symbol outcome. A failing trade dies alone.

**Owner's framing, which is sharper than the original and governs the design:**
*the batch is arbitrary — it is whatever happened to be proposed in one run.
Judging a trade against its accidental co-passengers makes no sense. Judge it
against what the account actually holds.* Portfolio-level risk (correlation,
concentration, total exposure) is a property of the **live book**, not of the
run.

**Not acceptable as a fix:** detecting the rejection and re-running the plan
without the failing leg. That asks the wrong question twice instead of the
right question once, and burns a second paid session to hide a schema defect.

**Note for whoever implements this.** `docs/architecture/DECISION_CHAIN_AUDIT.md`
records F5's veto hierarchy as *intentionally retained* after external review on
2026-08-14. This is not a careless gate — it is a reviewed decision now being
revised on evidence that did not exist then. Read that audit before changing the
hierarchy, and record the supersession there.

### 10.2 Macro sets exposure. Macro does not select trades.

`config/prompts/portfolio_manager.md` orders the reasoning chain
`macro_filter · news_check · earnings_check · signal_conflicts`. Macro is first
and is named *filter*. For an LLM reading top-to-bottom that is an instruction
to eliminate before considering, and on 2026-09-01 it did exactly that.

- Technical, news, earnings and smart-money **select** what to trade.
- Macro **sizes**: it sets total and net exposure, nothing else.

A bullish macro call must not be able to suppress a qualified short. Under this
split there is no conflict to resolve: a bullish macro keeps the book net long
while an individual short lives inside that exposure. Macro was never asked
about the single name.

**Owner's correction, accepted:** *if the weighting is real, position in the
prompt is irrelevant.* Ordering only matters today because there is no
weighting — the PM is a language model reading prose. Compute the weighting
deterministically in Python and hand the PM a ranked, scored list; then no
analyst can dominate by position. This is the same principle as the
reward:risk fix below and as PR #202: **the number comes from code, the agent
brings judgement.**

Macro remains authoritative for its own question — index and sector direction.
It has little to say about a single name falling for months on company-specific
news, and must not be allowed to answer that question by default.

### 10.3 Concentration scales size. It does not veto.

Sector crowding currently blocks. It should reduce position size. A
high-conviction opportunity in an already-heavy sector should still be taken,
smaller. Same defect as 10.2: a dial wired as a gate.

### 10.4 The reward:risk gate is replaced, not relaxed

**Do NOT simply lower the 1.5 floor.** The floor is not the defect; its inputs
are.

The stop is already derived in code from measured volatility
(`min_stop_atr_multiple: 3.0`). The **target is the model's guess**. R/R is then
`(target − entry) / (entry − stop)` — arithmetic performed on an opinion. A
correctly-sized wide stop plus a modestly-guessed target rejects automatically,
which is precisely how a `strong_buy/high` breakout (SLB, 7.7% stop) became
untradeable.

**Derive the target in code**, from the structural levels the system already
computes from price history (see Phase 1, `feat/tech-analyst-structural-levels`)
and the distance the symbol actually travels over the intended holding period.
Then the ratio measures something real and the floor means what its name says.

**Rejected alternative, and why.** An expected-value gate (win-rate × payoff)
is the theoretically better answer and was proposed first. The owner rejected
it for now: it requires a body of closed trades this account does not yet have,
and it replaces one unmeasured number with another. Revisit under Phase 7 once
there is real outcome history.

### Ordering

10.1 and 10.3 are contained schema/sizing changes. 10.2 requires the
deterministic analyst-weighting layer and is the largest. 10.4 depends on
Phase 1's structural levels being trustworthy. **Nothing here relaxes a risk
limit** — every change converts a blunt refusal into a proportionate response,
and the portfolio-level ceilings are untouched.

---

## Phase 11 — Fractional sizing and bounded margin (owner-ratified 2026-09-01)

**Status: ratified by Rex on 2026-09-01. NOT implemented, NOT deployed.**
Two decisions taken together because they both change how a position is
sized, and shipping one without the other changes the risk profile in a way
neither decision intended.

### 11.1 Fractional shares are ON — the 2026-08-27 decision is reversed

**What the earlier decision said.** Fractional stays off because Alpaca
supports fractional for simple orders but not combined with bracket/OCO, and
QAMC attaches the protective stop as an OTO bracket at entry (invariant 3:
every position carries a broker-resident stop from the moment it opens).
Going fractional means placing the stop as a separate order after the fill —
a window where a position exists unprotected.

**Why it was wrong, in the owner's words: "if the gap is brief upon entry,
then it's irrelevant to eliminate that option."** He is right, and the
original reasoning conflated two different risks. A second or two of exposure
on a liquid name is negligible. The real risk is the stop order **never
landing at all** — an API error, a rate limit, the process dying between the
two steps — and that is not a brief gap, it is an indefinite one.

**That failure is already covered.** The intra-session stop-coverage
reconcile runs every 30 minutes and reports e.g. "all 5 long / 0 short
position(s) adequately stop-covered". So the true worst case is bounded at
one reconcile interval, and only when placement failed outright. The
machinery for attaching a stop to an already-landed fill also exists —
`place_entry_protection(superseded_filled_qty=...)`, built for the re-peg
case where a fill lands under a superseded order id.

**Verified live 2026-09-01, not inherited from the note:** the paper account
returns `fractionable=true` for MSFT, SPY and BRK.B.

**Required before this ships — all three, none optional:**
1. Stop placement retries immediately and hard on failure.
2. A stop that still fails alerts the OWNER, not a log line.
3. The 30-minute sweep gains an explicit check for positions with NO stop at
   all, distinct from the existing "stop is present but mis-sized" path.

**What it buys.** Exact sizing. Whole-share rounding is a silent, constant
tax: V wanted 6% of the book and got 3.84%.

**Also verified in the same check, unrelated but recorded here so it is not
lost again:** `BRK-B` returns `asset not found` at the broker on every run.
The correct symbol is `BRK.B`. The universe carries the wrong one.

### 11.2 Margin is ON, capped at 1.5x gross exposure

**Owner ratified 1.5x on 2026-09-01**, against my recommendation to defer.
My argument was that the desk is 78% cash and refusing to deploy, so leverage
raises the stakes on the few trades it does take rather than producing more
of them. He considered it and decided; recorded here so the disagreement is
visible rather than silently dropped, and so that if 11.2 is later reversed,
the reason it was tried is legible.

**The account already permits this.** Alpaca reports `multiplier: 4`,
`buying_power` ~$28.2k against ~$9.88k equity. Margin is not being switched
on — it is being BOUNDED for the first time.

**This is the part that matters: there is currently NO gross-exposure cap in
the codebase.** `max_portfolio_risk_pct: 25` bounds AT-RISK capital (the sum
of stop distances), not gross exposure. Nothing today stops the book reaching
4x; it sits at 21.7% invested purely by the PM's own choice. **So 11.2 adds a
ceiling where none existed. Implementing it is a tightening, not a
loosening** — and shipping fractional sizing (11.1) without it would remove
the whole-share friction that has been accidentally holding deployment down.

**The rule:**
- Gross exposure (long market value + absolute short market value) may not
  exceed **1.5x equity**. Enforced deterministically in Python at the sizing
  and execution gates, not by asking an agent to respect it.
- **The cash-park does not count as exposure.** SGOV is parked cash, not a
  position; counting it would consume the entire allowance doing nothing.
- Never draw on the 4x intraday allowance — it forces a flat close.

**Two exposures the current risk envelope does not model, and must before
this is trusted:**
1. **Overnight gap.** Stops protect intraday. They do not protect against an
   open 15% lower, and levered that gap comes out of a thinner cushion. A
   separate, tighter ceiling on overnight gross exposure is required.
2. **Forced liquidation.** Below maintenance margin the broker sells, at the
   worst moment, without asking. Nothing currently watches the distance to
   that threshold or alerts on it.

**Sequencing.** 11.2's gross cap and both gap/liquidation guards land BEFORE
or WITH 11.1. Fractional sizing plus no gross cap is the one ordering that is
worse than either change alone.

**Neither part ships without a rehearsal-rig run** — see the session-start
rule in `docs/WORK.md`. This changes sizing and execution, which is exactly
the class the rig exists for.

---

## Invariants (must hold at all times)

1. Alpaca **Paper** only. Live capital requires separate explicit authorization.
2. Deterministic Python risk and broker protections are **final authority and fail closed**.
3. Every position carries a **broker-resident stop** from the moment it is opened.
4. No single trade risks more than **5% of equity**; total at-risk never exceeds **25%**, correlation-adjusted.
5. `Specialists → Portfolio Manager → AI Risk → deterministic Python → broker` — for **exits as well as entries**.
6. No trade without analyst-supplied structural levels.
7. The system never blocks awaiting human approval.
8. Agents may propose changes to governance documents; only the owner accepts them.

---

## Execution order

```
Phase 0  ──▶  Phase 1  ──▶  Phase 2  ──▶  Phase 3
(gates)      (levels)     (sizing)      (exits)
                              │
                              ├──▶ Phase 4 (evidence + feeds)
                              ├──▶ Phase 5 (shorting)
                              ├──▶ Phase 6 (cost + transparency)
                              └──▶ Phase 7 (measurement)

Phase 8 (docs) — alongside, as each phase lands.
```

Phases 1 → 3 are the sequence that stops the bleeding. Phase 0 must precede all of it: none of this should be merged into a repository with no working test gate.
