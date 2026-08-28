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
   **Built** on branch `feat/insider-signal-filter` (`f3aeba4` + `866e423`,
   `src/data/insider_signal.py`; not yet merged or deployed — see
   `docs/WORK.md` "Landed" and `docs/STATE.md`). The 10b5-1 caveat below was
   followed rather than the common folk rule: the flag never marks a large
   sale routine on its own, only ever supports a routine label for a sale
   that is already proportionally small. Measured on the live cache: 56.2%
   of open-market P/S rows routine, but the calendar-month test contributed
   zero of those matches — the multi-year history it needs did not exist
   before this branch, so the measured split is currently driven entirely by
   the proportional-sale rules, not the strongest, best-evidenced rule.
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

## Summary of what to build, in order

| Priority | Item | Type |
|---|---|---|
| 1 | Routine/opportunistic insider filter | Python — built on branch, not merged/deployed |
| 2 | Lazy Prices 10-K year-over-year diff | Python |
| 3 | News cascade: dedup → novelty score → LLM on the residual | Python + LLM |
| 4 | Deterministic macro regime; LLM confined to FOMC text | Python + LLM |
| 5 | Insider purchase as % of holdings, role weighting, cap tilt | Python |
| 6 | Post-cutoff discipline in the backtester | Process |

Items 1, 2 and 5 need no new data source and no model spend.
