# Research Findings — What Actually Works

**Date:** 2026-08-27
**Purpose:** establish which signals have evidence behind them before building on them, rather than reinventing techniques the literature has already tested and, in several cases, already falsified.

> Read the caveats. Two of the most attractive ideas here are substantially
> weaker than their headline numbers suggest, and one widely-cited result
> appears to be an artefact of the model already knowing the answer.

---

## 1. Insider transactions (Form 4)

### What predicts returns

**Routine versus opportunistic is the whole ballgame.** Cohen, Malloy & Pomorski, *Decoding Inside Information* (Journal of Finance, 2012): more than half of insider trades are "routine" — the same insider trading in the same calendar month for three or more consecutive years — and these carry **zero predictive power**. Removing them leaves opportunistic trades generating roughly **82bps/month** value-weighted abnormal return.
<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1692517>

**Cluster buying replicates well.** Alldredge & Blank (Journal of Financial Research, 2019): purchases clustered within ~2 days of a colleague's trade earn ~2.1%/month, about 0.9pp above solitary buys. Kang, Kim & Wang: 3.8% versus 2.0% over 21 trading days, widening at 90 days.
<https://onlinelibrary.wiley.com/doi/10.1111/jfir.12172>

**Role matters, and not as expected.** CFO purchases outperform CEO purchases — CFOs see the numbers first. Trades by "star" CEOs carry no signal.

**Size relative to holdings, not absolute size.** On the sell side only large sales that are *also* large relative to the insider's total position predict negative returns; small proportional sales are liquidity and diversification noise.

**Decay.** Roughly 25% of the abnormal return accrues within five days, ~50% within a month, with some persistence to six months.

**Concentration.** The effect is strongest in small and micro caps, where analyst coverage is thin and price discovery is slower.

### The caveat that matters most

**Ozlen & Batumoglu, *The Death of Insider Trading Alpha* (December 2025)** find that **70–80% of the price move occurs between the transaction date and the filing becoming public.** Form 4s may lag two business days. A system acting on filings is collecting the tail.
<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5966834>

Also relevant: **10b5-1 plans are not a clean noise filter.** For high-value sales, planned and discretionary transactions show similar opportunism, and the 2022 SEC reform did not reduce abnormal returns on insider selling.

**Conclusion for QAMC:** treat insider flow as a **tilt, not a primary alpha engine**. `src/data/smart_money.py` already records `lag_days`, so this can be measured on our own data rather than taken on faith.

### Transaction codes

| Keep | Exclude |
|---|---|
| **P** — open-market purchase (highest signal) | **A** — grants and awards |
| **S** — open-market sale | **M** — option exercise (no new cash risked) |
| | **F** — shares withheld for tax (automatic) |
| | **G** — bona-fide gift (no directional signal) |
| | **D**, **X** — dispositions to issuer, expirations |

QAMC already filters to P/S and to non-derivative rows (`src/data/smart_money.py:390-398`). That part is correct.

### What to implement, ranked

1. **Routine versus opportunistic classification** — same insider, same calendar month, three consecutive years. Drop routine entirely. Pure Python; highest value per line of code in the whole system.
   **Built and finished** on branch `feat/insider-signal-filter` (`f3aeba4`
   + `866e423`, `src/data/insider_signal.py`, plus a 2026-08-28 finishing
   pass that moved every threshold into `SmartMoneyConfig`; PR opened
   against `main`, not yet merged or deployed — see `docs/WORK.md` "Landed"
   and `docs/STATE.md`). The 10b5-1 caveat below was followed rather than
   the common folk rule: the flag never marks a large sale routine on its
   own, only ever supports a routine label for a sale that is already
   proportionally small. Re-measured on the live cache 2026-08-28: 57.3% of
   open-market P/S rows routine (2,742 rows), consistent with the original
   56.2%-of-2,188 measurement a day earlier, but the calendar-month test
   again contributed zero of those matches — the multi-year history it
   needs still does not exist in production, so the measured split remains
   driven entirely by the proportional-sale rules, not the strongest,
   best-evidenced rule. Only 1 of 413 buy-side rows was routine at all
   (a $0-price transaction), which is why re-running `fetch()` before/after
   on the real cache showed zero symbols changing admission status today
   even though 94 symbols' entire dollar volume is now correctly
   down-weighted to $0 in the analyst's ranking sum — see `docs/WORK.md`
   "Landed" for the full before/after.
2. **Cluster confirmation** — already partly present; rank clusters above solitary buys rather than merely detecting them.
3. **Purchase as a percentage of the insider's existing holdings** — the raw fields are already captured, the ratio simply is not computed.
4. **Role weighting** — upweight CFO and non-celebrity officers.
5. **Small/mid-cap tilt.**
6. **Measure our own filing lag** before trusting any of the above.

---

## 2. News

### The headline result does not survive scrutiny

Lopez-Lira & Tang, *Can ChatGPT Forecast Stock Price Movements?* (2023) reported that LLM headline sentiment predicted next-day returns (~0.21% per sentiment unit, concentrated in small caps).
<https://arxiv.org/abs/2304.07619>

A 2025 replication, *Detecting Lookahead Bias in LLM Forecasts*, re-ran the test in a strict post-training-cutoff window and found the effect **largely explained by the model having memorised what happened.** Contamination, not forecasting skill.
<https://arxiv.org/pdf/2512.23847>

### What does survive: novelty

Across the more careful literature, **novelty predicts returns better than sentiment or polarity.** Recycled commentary carries little signal however strongly worded; a genuinely new fact moves prices. *Buy the Rumor, Sell the News: When Is News Priced In?* studies absorption rates across 1.68M events.
<https://arxiv.org/html/2608.14014>

This is a direct instruction for QAMC: score **"is this new?"** rather than **"is this positive?"** — and novelty is an embedding comparison against a rolling buffer, not a model's judgment.

### The volume problem: use a cascade

Reading every article is infeasible and unnecessary. The established pattern:

1. **Cheap deterministic stage** — entity-tag, embed, deduplicate and novelty-score against a rolling 48–72 hour per-ticker buffer. Near-duplicates (wire reprints, the same story across outlets) are merged or dropped before any model sees them.
2. **Expensive stage on the residual only** — typically a handful of genuinely novel, entity-relevant items per day. These justify fetching the full article and using a real model to extract **structured facts** (event type, direction, magnitude) rather than a sentiment scalar.

This is likely **cheaper than the current design**, which spends attention on ~50 truncated blurbs, most of them duplicates or irrelevant.

---

## 3. Macro — the LLM is not earning its seat

For regime classification from yield curve, VIX and credit spreads, the evidence does **not** support LLMs adding value over rules. *When Valid Signals Fail* found numeric macro features dominated, with LLM-only signals underperforming in high-volatility regimes — precisely when the classification matters most.
<https://arxiv.org/pdf/2604.10996>

**Where LLMs do have published support: Federal Reserve communication tone.** Hawkish/dovish scoring of FOMC statements and minutes correlates with fed-funds-futures moves and sometimes carries unpriced residual information.
<https://www.kansascityfed.org/documents/5642/rwp20-14dohsongyang.pdf>

**Recommendation:** make regime classification deterministic Python; point the model at FOMC text specifically. Narrower, better-evidenced, and cheaper than the current arrangement.

---

## 4. Earnings — a documented signal already sitting in the pipeline

**Cohen, Malloy & Nguyen, *Lazy Prices*** (NBER): year-over-year **changes in 10-K language** — particularly risk factors, litigation and executive-team sections — predict returns at roughly **188bps/month gross (~22%/year)**. Notably there is *no* announcement-day effect: the market is inattentive and the return accrues slowly, which suits a swing horizon.
<https://www.nber.org/system/files/working_papers/w25084/w25084.pdf>

**This requires no LLM at all.** It is a text similarity diff — cosine or Jaccard on filing sections.

**QAMC already downloads and stores full 10-K/10-Q text** (`src/data/earnings.py`). The raw material for one of the better-documented anomalies in the literature is already on disk and unused.

Where an LLM does beat dictionary methods: **hedging and evasiveness detection in earnings-call Q&A** (not prepared remarks). QAMC has no transcript source today.

---

## 5. Failure modes to design against

**Training-data contamination is the dominant risk in this field.** The model has seen both the original news and the retrospective commentary explaining why the stock moved. Any backtest of an LLM signal inside its training window is close to meaningless.

**Consequence for the backtester (spec Phase 7):** it must enforce strict post-training-cutoff evaluation windows and record which model and cutoff each test used. Without that, it will produce beautiful results that mean nothing — which is worse than having no backtester, because it manufactures false confidence.

Also: **anomaly decay after publication** (McLean & Pontiff, 2016) — published edges shrink as capital chases them. And **backtest overfitting**, compounded by the many prompt and threshold degrees of freedom an LLM pipeline offers.

---

## 6. Candidate data sources

| Need | Options |
|---|---|
| Earnings calendar | API Ninjas (free tier), Financial Modeling Prep (free tier), EODHD (cheap) |
| Analyst estimates and revisions | EODHD Calendar API — consensus history at 7/30/60/90-day lookbacks plus revision counts |
| Call transcripts | API Ninjas (free key, back to 2005), Alpha Vantage, Finnhub (premium) |
| News beyond RSS | GDELT (free, unlimited, research-grade — needs real engineering), Polygon (per-ticker sentiment on free tier), NewsData.io, Mediastack |

---

## 7. Support and resistance — does a level actually hold?

**Measured on our own data 2026-09-02.** `src/data/levels.py` finds where price
repeatedly stopped, and every stop and target now traces to it. What it never
did was say whether a given level is *likely to hold*: its `strength` field is a
recency-weighted touch count on a scale nobody calibrated. `src/data/level_quality.py`
measures that, and `scripts/level_quality_report.py` reproduces every number below.
Nothing is wired into sizing, stops or targets — a test (`TestNotWiredIntoTrading`)
fails if any module under `src/` imports it.

### What the literature says

Garzarelli, Cristelli, Pompa, Zaccaria & Pietronero, *Memory effects in stock price
dynamics* (Scientific Reports 4:4487, 2014), LSE tick data: the probability price
bounces off a level RISES with the number of times it has already bounced there,
and the effect disappears on shuffled surrogates.
<https://www.nature.com/articles/srep04487>

Chung & Bellotti (arXiv:2101.07410, 2021) replicate the touch-count effect and add
explicit decay — level strength falls with age. Neither paper publishes a decay rate
that transfers to daily US equity bars.

**Neither paper uses volume.** In the 2014 paper the word appears once, in an
unrelated context. No study found in review offers measured evidence that
high-volume levels hold more often than low-volume ones.

### Method

*Bin width* is `delta = mean(|x_t - x_(t-1)|)` over the series at the sampling scale
in use — both papers' resolution. It self-scales across instruments and price regimes,
so there is no per-symbol tuning and no fixed percentage to go stale. It is also
invariant under permutation of the increments, which makes the arithmetic surrogate an
*exact* match on resolution rather than an approximate one.

*Score* is a Beta-Bernoulli posterior, `P(bounce | b_prev) = (n + 1) / (N + 2)` under a
Beta(1,1) prior, pooled across levels and symbols by prior-touch count. A uniform prior
is the right one because the question under test is precisely whether the rate departs
from a coin flip.

*Control* is the same estimator on surrogates built by shuffling the series' own
returns. That keeps the volatility, fat tails and skew and destroys only the ordering —
which is the only thing a level can be made of.

### The data we actually have

Historical bars come from **yfinance** (`src/data/market.py::get_ohlcv`); Alpaca's IEX
feed is the *fallback* when yfinance returns empty, not the primary source. Vendor
history caps, measured 2026-09-02:

| Sampling | History available | Bars/symbol | Universe total |
|---|---|---|---|
| Daily | to 1993 (SPY); `trading.lookback_days: 1800` uses ~5y | 1,254 median at 5y | 121,698 |
| 1 hour | 730 days (vendor cap) | 5,066 | 490,646 (100/101 symbols) |
| 5 / 15 min | 60 days (vendor cap) | 4,609 | 461,302 |
| 1 min | 7 days (vendor cap) | ~2,366 | not measured |

The published studies used tick data at second-scale sampling. We have nothing
comparable, and the question of whether the estimator survives the coarser sampling
had to be answered before anything was built on it.

### The validation gate — the result

101 symbols, daily bars, five years (2021-09-02 to 2026-09-02), arithmetic surrogates
(band identical by construction at $3.1694), five surrogates per series:

| Prior touches | REAL bounces/arrivals | REAL P(bounce) [95%] | SHUFFLED P(bounce) [95%] |
|---|---|---|---|
| 0 | 1878 / 3636 | **0.516** [0.500, 0.533] | 0.471 [0.465, 0.478] |
| 1 | 895 / 1767 | **0.507** [0.483, 0.530] | 0.486 [0.476, 0.497] |
| 2 | 459 / 844 | **0.544** [0.510, 0.577] | 0.495 [0.479, 0.510] |
| 3 | 244 / 439 | **0.556** [0.509, 0.602] | 0.506 [0.483, 0.528] |
| 4 | 129 / 222 | **0.580** [0.515, 0.644] | 0.484 [0.452, 0.516] |
| 5+ | 200 / 310 | **0.644** [0.590, 0.696] | 0.505 [0.470, 0.539] |

Pooled: real **0.5271** against shuffled **0.4805**, separation **+0.0467**. Slope
**+0.0249** P(bounce) per prior touch on real series against **+0.0049** on shuffled.
Stable across seeds 0 / 7 / 13 (separation +0.0467 / +0.0412 / +0.0459; the real slope
is identical, the real series being the same each time).

**The published pattern reproduces in direction and shape — real rises with prior
touches, shuffled stays flat near a coin flip — and is far weaker in magnitude than
the tick-data papers report.** Bounce probability runs 0.52 to 0.64, not "well above
0.5" throughout. No parameter was adjusted to obtain the separation: the estimator has
one scale parameter and it is computed from the data.

At **1 hour** the same shape holds (real 0.461 to 0.594, slope +0.0265; shuffled flat,
slope +0.0051; separation +0.0388). At **5 minutes** it does NOT: the level shift
survives (real 0.5175 against shuffled 0.4753) but the *rise* vanishes — real slope
+0.0028 against a shuffled +0.0035. Whatever produces the touch-count effect on our
data is not visible at 5-minute sampling over 60 days.

### What was changed after seeing a result — stated plainly

The first configuration run did **not** show the separation, and two settings were
changed afterwards. Both changes are recorded here because a reader has to be able to
judge them:

1. **Surrogate mode: log returns to arithmetic differences.** With log-return
   surrogates the control's band came out $1.89 against the real series' $0.82 — the
   control was being measured at 2.3x coarser resolution than the thing it controlled,
   which is not a control. Arithmetic differences make the band identical by
   construction. This is a fairness fix to the control, not a setting on the estimator.
2. **History window: full vendor history to five years.** `trading.lookback_days: 1800`
   is what the desk actually fetches, and a single `delta` spanning 1993 to 2026 is
   meaningless — $3.17 is most of a day's range now and several months of range in
   1995. Five years matches the configured lookback.

Under the original configuration (30+ years, log surrogates, five symbols) real and
shuffled both rose with prior touches and the pooled separation was +0.0011 — no
finding. **A reader who thinks either change was self-serving should treat the result
as unestablished.** Nothing about the estimator itself was touched: it has one scale
parameter and that parameter is computed from the data.

### Two further caveats that matter

**The shuffle control does not isolate levels specifically.** It rules out the
distribution of returns as the explanation. It does not separate level memory from
ordinary volatility clustering or short-horizon autocorrelation, both of which the
surrogate also destroys. The measurement establishes that ordering matters; it does
not prove that *levels* are the mechanism.

**High prior-touch buckets are right-censored upward.** A penetration ends a level, so
a level never broken within the sample contributes bounces and no penetration. That
bias is real and is not corrected — the shuffled control is censored identically, which
is what makes the comparison, not either column alone, carry the finding. Reading the
real 5+ figure of 0.644 as a standalone hold probability would overstate it.

### Recency decay — not fittable at the scale the desk trades

Fitted on our own episodes as `p(age) = 0.5 + (p0 - 0.5) * 2^(-age/H)`, against a
constant-probability null, with the half-life reported only when the extra parameter
clears the chi-square 95% threshold at 1 df *and* lands inside the span of data.

- **Daily (7,218 episodes): NOT FITTABLE.** Likelihood ratio 0.00 on both age
  definitions. There is no age effect in the daily sample at all.
- **Hourly (20,178 episodes): NOT FITTABLE.** Level age fits the wrong way round
  (P(bounce) 0.069 at age zero — a level that *strengthens* with age, which is not the
  claim); time-since-last-touch pins on the grid's one-bar floor.
- **5-minute (18,578 episodes): fittable.** Half-life 19.7 bars (~98 minutes) from
  P(bounce) 0.599 at age zero; 14.1 bars (~70 minutes) on time-since-last-touch.

**Decay stays out.** The desk's levels are daily, and the daily sample supports no rate.
The papers' anchors (tick effect dying between 90 and 180 seconds; daily FX levels
persisting about five business days) are recorded here for sanity only and are
deliberately not imported — importing one would be inventing a market-structure
constant, which is exactly the failure mode this desk has ruled out.

### Volume-at-price — recorded, never scored

The standard profile is computed on the same `delta` bins: point of control is the
maximum-volume bin, and the value area expands one bin at a time toward the larger
neighbour until 70% of volume is enclosed. **The 70% is a normal-distribution
convention, not an empirically optimised figure.** Each bar's volume is spread
uniformly across the bins its range covers — an assumption, and the standard one,
because the alternative is intraday data we do not have at daily scale.

These fields ride along on every `LevelQualityRecord` and enter **no** probability.
The reasoning is stated in the module and repeated here because it is the part most
likely to be forgotten: the published mechanism for why levels exist at all is stacked
resting **limit orders** at a price, and traded volume is a *proxy* for that mechanism —
possibly a poor one, since a price where enormous volume traded is a price where those
resting orders were consumed. **The volume terms are mechanism-motivated, not
evidence-backed.** Recording them is what makes "does volume add anything over touch
count?" answerable on our own book later. Scoring them today would be inventing a
constant. A test (`TestVolumeIsNeverScored`) fails if volume ever moves a probability.

### What this does NOT license

Nothing about sizing. The measured edge is small, the mechanism is not isolated, and
the calibration is fitted on the same history any backtest would score against. Wiring
level quality into sizing, stop placement or the R/R gate is a separate decision on
separate evidence, and it has not been made.

### What changed in `levels.py` because of this measurement (2026-09-02)

`src/data/levels.py`'s `strength` field — the score that selects which 6 levels
per side the Tech Analyst is shown — weighted each touch by `0.5 ** (age_sessions
/ 252)`, a one-year half-life picked for being round and never measured. It is
now touch count alone, discounted by distance exactly as before:
`strength = touches / (1 + distance_pct / 10)`. This acts on the "no age effect"
finding above; it does not wire `level_quality.py` itself into the trading path —
nothing under `src/` imports that module, `TestNotWiredIntoTrading` still passes,
and sizing, stops, targets and the R/R gate are unchanged.

Checked on the full 101-symbol universe (same-day bars) before deciding: removing
the recency term changes the top-6 selection on at least one side for 66 of the 99
symbols that had any levels, and moves 27.1% of the 1,020 side-slots compared (6
slots x 170 sides with a candidate level). That is not a no-op — the Tech Analyst
has been shown a systematically different set of levels than the data supports for
as long as the invented half-life stood. A second alternative — scoring by the
measured pooled bounce probability per touch count instead of raw touch count —
was checked against the same universe and moves the selection even more (33.9% of
slots, 71/99 symbols), because that curve is non-monotonic at low touch counts and
pools everything past 5 touches into one number for posterior-sample-size reasons
unrelated to level quality. Touch count was kept for being simpler and for being
the less disruptive of the two changes, not merely the more convenient one.

The touch-count finding should be treated as promising, not settled, here exactly
as it is above: two settings were changed after an initial run found nothing, and
the shuffle control does not isolate levels from ordinary volatility clustering.

### The stop-honouring threshold, ratified 2026-09-03

Phase 12.1 (docs/QAMC_REMEDIATION_SPEC.md §12.1, "What this does NOT license"
above, and docs/WORK.md's tracked decision) asked the question this section
explicitly declined to answer: how many prior touches should a level need
before a stop resting on it is trusted enough to be honoured however tight?
That is a decision about wiring level quality into stops — separate evidence,
separate decision, made here rather than left in the measurement above.

**Decided: 5 touches** (`risk.min_level_touches_for_stop_honor`, wired in
`PortfolioConstructor._level_backing_stop`). Below the table, restated with
the CI overlap made explicit — the thing that actually decides where the
line goes:

| Prior touches | REAL P(bounce) [95%] | SHUFFLED P(bounce) [95%] | CIs overlap? |
|---|---|---|---|
| 0 | 0.516 [0.500, 0.533] | 0.471 [0.465, 0.478] | No — but only 1 touch above "no structure" |
| 1 | 0.507 [0.483, 0.530] | 0.486 [0.476, 0.497] | Yes (0.483-0.497) |
| 2 | 0.544 [0.510, 0.577] | 0.495 [0.479, 0.510] | Touching (real floor 0.510 = shuffled ceiling 0.510) |
| 3 | 0.556 [0.509, 0.602] | 0.506 [0.483, 0.528] | Yes (0.509-0.528) |
| 4 | 0.580 [0.515, 0.644] | 0.484 [0.452, 0.516] | Touching (0.515 vs 0.516) |
| 5+ | 0.644 [0.590, 0.696] | 0.505 [0.470, 0.539] | **No — 0.05 clear gap** |

5+ is the only bucket where the real and shuffled 95% intervals do not
overlap or touch at all — every other bucket's separation could plausibly be
sampling noise around a coin flip, given the interval widths actually
measured (small per-bucket sample sizes: n=222 to n=439 for buckets 2-4,
against 310 for 5+, so the non-monotonic overlap pattern across 2/3/4 is
itself evidence of noise at that resolution, not a reason to pick 3 or 4).
0 touches also does not overlap, but a 0-touch "level" is not structure by
this system's own definition (`MIN_TOUCHES = 2` in `find_structural_levels`)
and is excluded from consideration on that basis alone, not on the strength
of its separation.

Recency was deliberately NOT added as a second gate: the "Recency decay"
subsection above found no age effect at daily scale at all (likelihood
ratio 0.00 on both age definitions) and stated plainly that importing an
untested decay rate would be "inventing a market-structure constant." A
5-touch level defended three years ago is trusted exactly as much as one
defended last week, on the same evidence that says touch count matters and
age does not.

**What this does not change:** `find_structural_levels`' `MIN_TOUCHES = 2`
in `src/data/levels.py` is untouched — that constant decides whether a level
is shown to the analyst and eligible as a target at all, a question §12.1's
own spec text already treated as settled and separate from "trusted enough
for a tight stop." A level with 2, 3 or 4 touches still exists, still ranks
by touch-count `strength`, and can still anchor a derived target
(`TechAnalysisResult.computed_levels`, consumed by
`derive_structural_target`) — none of that reads
`computed_level_touches` or the new threshold. Only the stop-honouring
exemption in `_level_backing_stop` is gated by it. A level below the bar
does not make the trade untradeable — the stop falls back to the
pre-existing ATR-floor widening logic, the same fallback an unbacked stop
has always used.

**Status:** ratified, not merely proposed — the owner's standing instruction
was "go with whatever the research says," and 5 is what the measured
separation supports. Revisable the same way every other placeholder
threshold on this desk is, on new evidence, not a re-guess.

---

## Summary of what to build, in order

| Priority | Item | Type |
|---|---|---|
| 1 | Routine/opportunistic insider filter | Python — built on branch, not merged/deployed |
| 2 | Lazy Prices 10-K year-over-year diff | Python |
| 3 | News cascade: dedup → novelty score → LLM on the residual | Python + LLM |
| 4 | Deterministic macro regime; LLM confined to FOMC text | Python + LLM |
| 5 | Insider purchase as % of holdings, role weighting, cap tilt | Python |
| 6 | Post-cutoff discipline in the backtester | Process |
| 7 | Level-quality measurement — built, measured, `levels.py` strength now touch-count-based; `level_quality.py` itself NOT wired; stop-honouring touch bar (5) ratified 2026-09-03 | Python |

Items 1, 2 and 5 need no new data source and no model spend.
