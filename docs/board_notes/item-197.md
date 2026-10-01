## item 197 — RETIRED 2026-09-30, the re-peg now derives a floor for a short and a ceiling for a long, with the room test, the quote side and the walk direction inverted to match, and a sell_short spec is exercised in tests

The re-peg path (`_repeg_entry_order`, `src/pipeline_stages.py`) was written for
the BUY side and never generalised. It builds a single bound —
`reference * (1 + slippage_bps / 10_000)` — names it `ceiling`, stores it on the
spec, and returns `no_room` when the submitted `limit_price` is already at or
above it. No `side`, `action` or `is_short` is read anywhere in the function.

For a BUY the logic is correct and, since the submitted limit IS the ceiling, it
almost always returns `no_room` immediately. For a `sell_short` the same
arithmetic produces a number ABOVE the reference when the fillable bound is
BELOW it, so two things break together: the room test fires backwards (a short
limit far from the floor reads as "already there"), and any walk it did perform
would move the limit UP, away from where a short can fill.

Not live. `repeg_enabled` is `false` in `config/settings.yaml` and defaults to
`False` in `src/config.py`. Board item 183 found this while removing the
far-through-quote entry skip and deliberately left it alone: it predates that
work and fixing it is a behaviour change on a money path with no live exercise
and no recorded outcomes to measure against.

The hazard to watch is ordering: the flag being turned on before the side fix
lands would put the defect straight into production on the short book.

## item 197 — RETIRED 2026-09-30, the re-peg now derives a floor for a short and a ceiling for a long, with the room test, the quote side and the walk direction inverted to match, and a sell_short spec is exercised in tests

The re-peg path (`_repeg_entry_order`, `src/pipeline_stages.py`) was written for
the BUY side and never generalised. It builds a single bound —
`reference * (1 + slippage_bps / 10_000)` — names it `ceiling`, stores it on the
spec, and returns `no_room` when the submitted `limit_price` is already at or
above it. No `side`, `action` or `is_short` is read anywhere in the function.

For a BUY the logic is correct and, since the submitted limit IS the ceiling, it
almost always returns `no_room` immediately. For a `sell_short` the same
arithmetic produces a number ABOVE the reference when the fillable bound is
BELOW it, so two things break together: the room test fires backwards (a short
limit far from the floor reads as "already there"), and any walk it did perform
would move the limit UP, away from where a short can fill.

Not live. `repeg_enabled` is `false` in `config/settings.yaml` and defaults to
`False` in `src/config.py`. Board item 183 found this while removing the
far-through-quote entry skip and deliberately left it alone: it predates that
work and fixing it is a behaviour change on a money path with no live exercise
and no recorded outcomes to measure against.

The hazard to watch is ordering: the flag being turned on before the side fix
lands would put the defect straight into production on the short book.

