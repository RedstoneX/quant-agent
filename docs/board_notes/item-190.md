## item 190

**Filed 2026-09-30, out of the order-placement gates review.** The desk built a feature to automatically sweep idle cash into a short-term Treasury-bill fund, then turned it off. Turning it off did not remove it: the code that runs it is still wired into every trading session, just switched to do nothing. A one-line flag flip would turn it back on with no further review.

That earlier review found one small piece of this dormant feature — a reserve band that decides how much cash to hold back — could not simply be deleted, because deleting it while the rest of the sweep stays wired in would leave the feature reachable with no record of what it does. Properly retiring the whole feature is a big, mechanical job: an estimated 187 places in the code and its tests still refer to it.

Right now nobody owns that job. It is only described inside another item's writeup, where it risks being forgotten once that item's narrower question is answered. This item exists so the cleanup has its own visible line on the board until it is actually done.

**No owner decision needed here** — this is an engineering bookkeeping fix (remove dead, switched-off code) rather than a money or risk-appetite question.

