# Agent Role Audit

**Date:** 2026-08-27
**Method:** each agent's prompt, its `build_user_message`, and its feeding data modules were read directly, then compared against what a professional in that role actually uses.
**Why:** the owner had assumed each agent did what its job title implies. Mostly they do a fraction of it.

> **The recurring pattern is not missing data.** In case after case the pipeline
> already computes or fetches the information and then withholds it from the
> agent that needs it. The 20-bar chart, the unused correlation matrix and the
> intraday macro blindfold are three instances of one habit.

---

## Severity 1 — safety and sizing

### 1.1 A hard risk rule has no code behind it

`config/prompts/risk_manager.md` (checklist item 7) instructs the Risk Manager to halve BUY sizes during a drawdown, and states plainly that **"no deterministic code enforces this rule... if you don't check it, nothing does."**

A safety rule that depends on a language model remembering to apply it is not a rule. `in_drawdown` is already computed and passed; the gate is arithmetic.

**Fix:** move it into `src/risk/rules.py` as a hard gate. Cheap, deterministic.

**FIXED (2026-08-27, commit `c89e957`, branch `feat/risk-metrics-and-pm-correlation`).** `src/risk/rules.py::apply_drawdown_scale` halves every BUY's allocation before the hard filter runs, so the cash budget, sector accumulation, RM and execution all see the halved size; `drawdown_buy_cap` joins `HARD_BLOCK_RULES` (`src/pipeline.py`) as a fail-closed backstop for any BUY that reaches the engine unscaled. The PM prompt's own `drawdown = 0.5` multiplier was deleted in the same change — two independent halvings would quarter the position, so exactly one layer owns it.

### 1.2 The Portfolio Manager receives no correlation data

`config/prompts/portfolio_manager.md` (Step 6) tells the PM to "avoid stacking highly correlated positions (e.g. NVDA + AMD + SMH)". `grep -i correlation src/agents/portfolio_manager.py` returns **zero hits**.

`src/data/correlation.py` builds a full matrix **every run** (`src/pipeline_stages.py:1065`) and passes it only to the deterministic cluster check in `src/risk/rules.py:200` — which fires *after* the PM has already chosen. The PM is instructed to act on information it is never given.

**Fix:** pass the already-computed matrix into the PM's context. Low effort, high value.

**FIXED (2026-08-27, commit `c89e957`, branch `feat/risk-metrics-and-pm-correlation`).** The matrix is now built on the DecisionStage side (`TradingPipeline._ensure_correlation_matrix`, memoized on `RunContext`) before PM decides, rather than only in RiskStage after. `correlation_clusters()` (`src/data/correlation.py`, new — groups transitively, so a theme is one bet even where its outer pair falls under 0.7) renders the measured clusters into PM's Quantitative Facts. RiskStage reuses the same memoized matrix instead of building a second one, so the deterministic cluster check judges PM against the exact numbers PM was shown.

### 1.3 Portfolio heat is not computed anywhere

Total capital at risk if every open stop were hit — the number the owner-ratified 25% ceiling is defined against — does not exist in any agent or in the deterministic engine.

**Fix:** `sum(position_size × distance_to_stop)`. Pure Python.

**FIXED (2026-08-27, commit `c89e957`, branch `feat/risk-metrics-and-pm-correlation`).** `src/risk/metrics.py` (new) computes budget risk (vs. entry, released once a trailing stop reaches entry) and open risk (from today's price), rolled up into `PortfolioHeat` and rendered via `format_heat_block()` to both PM and RM with headroom under `risk.max_portfolio_risk_pct` (25%, `config/settings.yaml`). A position with no stop is charged at full notional, not zero — scoring it zero would rank the riskiest book as the safest. **The 25% ceiling is now an enforced gate** (spec Phase 2b, commit `75c0233`, branch `feat/pm-flex-routing`, not yet merged) — `src/risk/budget.py::allocate_risk_budget` rations every risk-based target against it, alongside risk-based sizing (§2.1), before Phase 2b it was reporting-only.

### 1.4 R-multiple is absent

Profit measured against initial risk is the standard trader metric for whether a position is working. No agent computes it. `thesis_progress_pct` measures distance to target, which is a different quantity and does not normalise for how much was risked.

**Fix:** `(current_price − entry) / (entry − initial_stop)`. Pure Python.

**FIXED (2026-08-27, commit `c89e957`, branch `feat/risk-metrics-and-pm-correlation`).** `src/risk/metrics.py::r_multiple` computes it against the stop the position was OPENED with, never the trailed stop — the denominator is the bet that was actually made. `_build_position_facts` (`src/pipeline.py`) renders it into the Position Reviewer's per-position metrics line, ahead of `thesis_progress_pct`.

### 1.5 The Position Reviewer has no memory of its own prior review

`_build_own_recent_decisions` (`src/pipeline.py:6369-6409`) replays past *actions* and explicitly drops HOLDs. No prior metric values are carried forward, so the reviewer rebuilds its view from scratch twice a day.

This is the second independent confirmation of the EPD defect: on 2026-08-26 it sold a position for "not progressing" when progress had risen 16% → 20% and distance-to-stop had improved since its own midday read.

**Fix:** persist each review's metrics; pass the previous snapshot; reject a deterioration verdict when the deltas are positive.

**FIXED (2026-08-27, commit `aea82ee`, branch `feat/exit-rework-pace-and-memory`, not yet merged).** Each review now snapshots its per-position metrics to `specialist_evidence` (`agent_name="position_reviewer"`, `kind="review_metrics"`) via new `db.save_position_review_metrics`; the next review reads them back with `db.get_prior_position_review_metrics` and the pipeline's new `_build_review_metric_deltas` / `_persist_review_metrics` (`src/pipeline.py`). New `src/risk/exit_guard.py` (`MetricDeltas`, `compute_deltas`, `is_deterioration_claim`, `veto_contradicted_exit`) then vetoes a SELL/REDUCE whose stated reason is a deterioration claim ("stalling", "not progressing", "momentum fading", etc.) when every metric that moved since the prior review improved — logged as `exit_vetoed_contradicts_own_metrics`. Exits on new information (news, earnings, regime shift, correlation breach, a triggered `thesis_invalid_if`) are never vetoed, and a mixed picture is deliberately not vetoed — that stays the reviewer's judgment call. Landed alongside Phase 3.1 (the pace feedback loop, `QAMC_REMEDIATION_SPEC.md` §3.1); see that document for the pace fix.

---

## Severity 2 — analyst coverage

### 2.1 Tech Analyst

Largely addressed by Phase 1 / 1b. Before that work it received twenty daily bars — one month — and was asked to identify structural levels, with no relative strength, no range position, no volatility regime, no MA direction, no consolidation measure and no liquidity figure.

**Closed 2026-08-31:** `MarketDataProvider.get_next_earnings_date()` now has callers — `src/data/event_calendar.py::fetch_earnings_proximity` sweeps it for every symbol the Risk Manager is judging. **Still open for the Tech Analyst specifically:** nothing passes `days_to_earnings` into `TechAnalystAgent.build_user_message`.

### 2.2 News Analyst

| Gap | Detail |
|---|---|
| ~~Two of nine feeds dead~~ **PARTLY FIXED** | Investigated live 2026-08-28. The UA hypothesis (403 = blocked User-Agent) was WRONG for both: Reuters killed public RSS in June 2020 and reutersagency.com is now a paid-licensing marketing page with no feed link left at all; AP's own feed (`apnews.com/index.rss`) answers 401 "Invalid client credentials" (paid Content API, OAuth2), and the third-party proxy previously used sits behind a Cloudflare JS challenge no UA can pass. Neither is fixable for free — both entries removed from `RSS_FEEDS` rather than left permanently red, and Yahoo Finance News (verified live, free, publisher-hosted) added as a partial substitute. Dedicated Reuters/AP wire access remains an owner decision (paid) — see `docs/WORK.md`. |
| Never reads an article | RSS summary only, truncated to 300 characters (`src/data/news.py:127`). |
| No source credibility weighting | A Federal Reserve press release and a MarketWatch aggregator are equal-weight text blocks. |
| ~~Weak deduplication~~ **FIXED** | Was word-Jaccard > 0.7 on titles, which measurement confirmed cannot separate at any threshold. Replaced by a term-frequency cosine stage (`src/data/news_dedup.py`) calibrated on 589 real archived headlines, with collapsed counts preserved and rendered to the analyst as syndication breadth. |
| No novelty assessment | Cannot distinguish new information from recycled commentary — see `RESEARCH_FINDINGS.md`, where novelty is the property that actually predicts returns. |
| ~~Degradation invisible downstream~~ **FIXED** | The PM could not tell "the wires were down" from "a quiet news day" — a dead feed logged a warning and `data_status["news"]` still read "ok" as long as the LLM call parsed. `NewsCoverage` (`src/data/news.py`) now tracks configured/succeeded/failed feeds per run, threaded into the analyst's own prompt ("News Coverage" section) and into `data_status["news"]` (`ok`/`partial`/`failed`, coverage-failure dominates a parsed-but-empty report) — which is what `trader_feed.py` / `notifier.py` already render as `⚠️ Data degraded: news`. |

### 2.3 Macro Analyst

Every value is presented as a bare current level with, at best, a 30-day delta. There is no historical percentile context anywhere — "VIX 19.5" rather than "19.5, the 40th percentile of the last two years".

~~**Missing series, all free on FRED:** real yields (`DFII10`), inflation breakevens (`T10YIE`), the 3M/10Y curve (`DGS3MO`), the dollar index (`DTWEXBGS`), investment-grade spreads (`BAMLC0A0CM`).~~ **FIXED (2026-08-30, PR #162, commit `92689e6`).** All five are now fetched and rendered to the macro analyst, plus a sixth (`ICSA`, weekly initial jobless claims) added for the same reason. All six were verified live against FRED before being wired in. See `config/prompts/macro_analyst.md` for how the analyst is taught to read them.

~~**Also:** FRED calls time out repeatedly in production, so the agent frequently runs on `None`.~~ **FIXED (2026-08-30, PR #162).** The single-retry/flat-backoff policy that let a three-minute outage on 2026-08-26 fail all nine series at once has been replaced with config-driven retries, exponential backoff with jitter, and a real wall-clock ceiling (`total_fetch_deadline_s`, 90s default) the old policy never had. When series still fail, coverage is no longer silent: it now feeds `data_status["macro"]` through the same ok/partial/failed path the news feed already used, and the macro analyst is shown its own coverage directly.

**Still true, unrelated to this fix:** no historical percentile context exists for any of these series (the paragraph above). The macro event calendar was built on 2026-08-31 (`src/data/event_calendar.py`) — `event_risk` is now answered from FRED's free release-dates schedule plus a per-symbol earnings sweep, with every gap named rather than inferred. FOMC meeting dates followed the same day, from the Federal Reserve's own free calendar (structured JSON feed, rendered calendar page as fallback) — they are fetched, not declared uncovered.

### 2.4 Earnings Analyst

- Compares against **one** prior filing (`src/agents/earnings_analyst.py:163-180`), so no multi-quarter revenue/margin/FCF trend and no guidance-versus-prior-guidance diff.
- No consensus estimates, so it cannot distinguish "beat expectations" from "beat last year" — and only the former moves a stock.
- Share count and buyback execution are not extracted, though they are present in filings already downloaded.

**Not a gap:** valuation is deliberately deferred to the Tech Analyst because price is stale on a cached filing. That design is correct.

### 2.5 Smart Money Analyst

Correctly filters to transaction codes P/S, non-derivative rows only, with a $100k floor and a 14-day cluster window requiring two distinct owners.

- **Routine-versus-opportunistic classification — finished, PR opened, not
  yet merged.** `f3aeba4` + `866e423` plus a 2026-08-28 finishing pass
  (config-driven thresholds, per-code and fail-closed tests) on branch
  `feat/insider-signal-filter`, `src/data/insider_signal.py`. See
  `RESEARCH_FINDINGS.md` §1 and `docs/WORK.md` "Landed" for the mechanism
  and the re-measured 57.3% routine split (with the caveat that the
  calendar-month test has not yet contributed to that number — only the
  proportional-sale rules have).
- Post-transaction share count is captured but never converted into a purchase-as-percentage-of-holdings ratio.
- `actor_roles` is captured but unweighted; CFO purchases are more informative than CEO purchases.
- No market-cap tilt, though the documented effect concentrates in small and mid caps.

---

## Severity 3 — worth knowing, not urgent

- **PM has no expectancy feedback by conviction level.** Aggregate 30-day win rate exists, but nothing tests whether "high conviction" has historically outperformed "medium". Until measured, conviction-weighted sizing is an assumption.
- **No opportunity-cost framing at candidate selection.** Rotation logic only fires when cash is short, so a better candidate never displaces a mediocre holding while cash is ample.
- **Sector tags only, no theme/factor exposure.** A 75% per-side sector cap (spec §12.3; 40% before 2026-09-01) does not catch a cross-sector "AI capex" concentration.
- **`thesis_invalid_if` is free text**, evaluated by a model reading it. Conditions like "closes below MA50" are structured enough to check in code.

---

## Is the LLM Risk Manager earning its seat?

**Yes, but narrowly.** `src/risk/rules.py` already enforces position caps, total exposure, daily loss, stop presence, correlation clustering, cash-only and sector caps. The Risk Manager adds narrative coherence auditing and PM-versus-Tech signal-fidelity checking, which rules cannot do.

The uncomfortable part: its most safety-critical contribution is **covering for a missing deterministic control** (§1.1), not adding judgment. Once the drawdown gate is real code, this seat should be re-examined honestly.

**Status (2026-08-27):** the drawdown gate is real code (§1.1, above). That re-examination has not happened yet.

---

## Recommended sequencing

| When | Items |
|---|---|
| **Phase 2** (sizing and risk) | §1.1 drawdown gate · §1.2 correlation to PM · §1.3 portfolio heat · §1.4 R-multiple — **landed as Phase 2a, commit `c89e957`** |
| **Phase 3** (exits) | §1.5 reviewer memory — **landed, commit `aea82ee` on branch `feat/exit-rework-pace-and-memory` (not yet merged)** |
| **Right after Phase 2** | §2.5 routine/opportunistic filter — **finished, PR #133 opened against `main`, commit `f3aeba4`/`866e423` + 2026-08-28 finishing pass on branch `feat/insider-signal-filter` (not yet merged)** |
| **Separate pass, needs owner decisions** | §2.2 news cascade · ~~§2.3 macro series~~ (fixed 2026-08-30, PR #162 — six series added, FRED resilience rebuilt; the macro event calendar followed on 2026-08-31, `src/data/event_calendar.py`; percentile context remains unbuilt) · §2.4 earnings trends — several need new data sources |
