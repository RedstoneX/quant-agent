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

**Step 4, part done 2026-10-01.** A latent defect in the piece step 4 owns was fixed ahead of the rest: the account view's "deployable cash" added the market value of the parked T-bill vehicle even with the sweep switched off, a state in which nothing converts that vehicle back into cash for a purchase. The engine never did this, so the two numbers the desk calls by the same name would have disagreed — the operator's tile reading higher than the money the desk can actually spend. Nothing is held in the vehicle, so it never produced a wrong number in real life; the production database records no such holding today.

The rest of step 4 is blocked rather than skipped. Steps 2 and 3 — rewriting the owner-facing funding wording, and deleting the sweeper itself — have not reached the main branch, so the sweeper is still live code. Deleting its number-ledger entries now would leave constants that still run with no record of where they came from, and stripping the sweep fields out of the account view and the frontend would remove the only place a leftover holding would be visible. The item stays open with those criteria written into its board entry.

**Retirement BLOCKED 2026-10-01 — measured, not estimated.** A full sweep of this checkout plus a read-only query of the production DB (`/home/qamc/quant-agent/data/quant_agent.db`) found the feature is not dead code. The sweeper's remaining method `release_retired_vehicle` runs on every session lane and places a real sell order with protection when a vehicle is held; the production ledger shows the feature really traded (8 `SWEEP_BUY`, 6 `SWEEP_SELL`, 2026-09-02 to 2026-09-17) and holds none of the vehicle now, so that path is idle because of the book's state and not because the code cannot fire. The $500 minimum-order number the board recorded as vestigial still refuses a small addition to a short position outright, and still decides whether the rotation report calls funding a binding constraint. The 1% reserve band still produces the reserve figures the account view shows. Deleting any of the three changes what the desk does with money, so the remaining retirement is an owner decision, not the mechanical cleanup this item was filed as.

