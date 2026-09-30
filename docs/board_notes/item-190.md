## item 190

**Filed 2026-09-30, out of the order-placement gates review.** The desk built a feature to automatically sweep idle cash into a short-term Treasury-bill fund, then turned it off. Turning it off did not remove it: the code that runs it is still wired into every trading session, just switched to do nothing. A one-line flag flip would turn it back on with no further review.

That earlier review found one small piece of this dormant feature — a reserve band that decides how much cash to hold back — could not simply be deleted, because deleting it while the rest of the sweep stays wired in would leave the feature reachable with no record of what it does. Properly retiring the whole feature is a big, mechanical job: an estimated 187 places in the code and its tests still refer to it.

Right now nobody owns that job. It is only described inside another item's writeup, where it risks being forgotten once that item's narrower question is answered. This item exists so the cleanup has its own visible line on the board until it is actually done.

**No owner decision needed here** — this is an engineering bookkeeping fix (remove dead, switched-off code) rather than a money or risk-appetite question.

## item 190 — detail moved from the board 2026-09-30

Item 183 found that `CashSweepConfig.reserve_pct` (the 1% cash-reserve band) cannot be deleted on its own: the sweeper is disabled (`cash_sweep.enabled: false`) but still constructed and called by the pipeline, so the band, its dead pad/buffer constants and the sweeper itself would have to be removed together or not at all — a job item 183 sized at roughly 187 references across the pipeline, the API and nine test modules, and explicitly did not start. That job has no board item of its own; it exists only inside item 183's prose, where it risks being read as done once item 183's own three gates close. This item tracks it separately so it survives item 183's closure.


**Scope corrected 2026-09-30 after reading live code.** The item was filed as "remove dead, switched-off code". Two of its pieces are not dead.

`reserve_pct` is read outside the sweeper by `src.risk.rules.deployment_gap_band_pct`, which is what makes the desk's `deployment_gap` advisory fire or stay quiet, and by `src.api.deps.get_cash_sweep_reserve_pct` for the `/account` reserve figures. So item 183's band cannot be deleted as a side effect of retiring the sweep either — the band's real question (how far short of fully-invested still counts as fully invested) outlives the feature and has to be answered or explicitly dropped with the advisory.

`min_order_usd` is the opposite shape: the code itself records that no trade path rejects on it any more and that `apply_gross_ceiling` ignores it, so it is vestigial as a gate — but the portfolio manager still says the number out loud to the owner in its funding narrative, so the deletion has to rewrite that prose rather than just remove a field.

The 187-reference estimate is low: ~550 mentions across 88 files, including four frontend components, the Mission Control API schema and routes, the branch-preview tool, and roughly thirty test modules. No removal was attempted in this pass — a partial gut of a path that runs before every BUY is worse than leaving the switched-off shell standing, and the two live readers above have to be settled first.

