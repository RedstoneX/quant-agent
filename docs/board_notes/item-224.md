## item 224

224. The desk records no realised sector weights, so concentration can only be guessed before the fact and never read after it -- filed 2026-10-01 from item 221.** Item 221 established that the pre-decision preview cannot project a sector mix at all, because sizing depends on a PM target that does not exist when the preview is built; what the desk could record instead, and does not, is the sector weights of the orders the constructor ACTUALLY built, once per run. Without that row nobody can say afterwards whether a session concentrated the book or not. Detail in `docs/board_notes/item-221.md`.

Filed 2026-10-01 alongside the sector-preview ruling. The board carries
the item and its criteria; this file exists so the item has a note of its
own, as every item must, and is the place for detail as the work proceeds.

---

## THE RECORDING (2026-10-01) — BUILT, classified UNPROVEN

**What was built.** One durable row per run in a new `realised_sector_weights`
table, carrying the realised `(sector, side)` weights of the orders the
constructor actually built that session. The weights live in `weights_json` as
a list of `{sector, side, weight_pct, orders}` objects — a list, not a mapping,
precisely so an undeterminable sector can be stored as a JSON `null` key rather
than being forced into a string bucket.

**Call site, named.** `src/pipeline_stages.py`, in `DecisionStage`, immediately
after `pipeline.portfolio_constructor.construct_orders(...)` returns and beside
the existing `_record_constructor_side_flips` call. This is the right call site
and the only correct one: the constructor's gross-exposure rationing runs LAST,
on the finished order list, and changes order sizes after each one was built, so
any figure captured earlier is what was hoped for rather than what was built.
The helper is `_record_realised_sector_weights`; it never raises.

**Unit and denominator — item 222's, not a second convention.** Every weight is
*percent of total account equity, raw position notional, before the gross
multiplier*. That string is stored on every row in the `denominator` column,
and `total_value` carries the equity the weights are a share of at the moment
the constructor sized. This is the same unit and the same denominator item 222
settled the three single-name limits onto; nothing was converted and nothing
was invented. A test asserts the stored string still says both halves of it.

**Entry orders only in the weights.** A BUY or SHORT puts exposure on, and its
`allocation_pct` is that exposure in the unit above. A SELL or COVER takes
exposure off, and its `allocation_pct` is a reduction — adding the two would
produce a number that is not a weight of anything. The reducing orders are
COUNTED in `reducing_orders_built` so the row never implies the session built
only entries.

**Sector comes from the constructor's own lookup.** `_accrue_sector` already
resolves the sector of every BUY/SHORT while sizing; it now also keeps what the
lookup said on `PortfolioConstructor.last_order_sectors`. The recording reads
that, so it buys no market data (`_get_sector` is a live network call for an
uncached name) and cannot disagree with the sizing it describes.

**NULL discipline.**
- A sector the desk could not determine is `null` inside the JSON, counted in
  `unknown_sector_orders`, and never "other".
- A session that built no entry orders writes `weights_json` NULL with
  `entry_orders_built` 0 — the fact that nothing was built, which is a
  different fact from a book with zero concentration.

**RECORDING ONLY, and the bar is in the column comment.** Nothing may read
these rows back into a sizing, ordering or refusal decision, and they may NEVER
be swept for the sector cap that would have performed best. That is fitting a
number to this desk's own trading record, which doctrine bars outright. A test
(`test_nothing_reads_the_table_back_into_a_decision`) fails the build if any
product module ever SELECTs from the table.

## CAN IT FIRE?

**Reached from executable product code: YES.** The single call site above is on
the live `DecisionStage` path every session takes; it is not a test and not a
migration. A test asserts there is exactly one product call site and that the
helper really calls the writer.

**Classification: UNPROVEN.** No desk session has run since last night's close,
so no production row exists and the recording has never fired against a real
run. It is proven by test only. It becomes POPULATING when the next session's
row is read back out of the production database and shown non-empty.

**The board criterion is therefore NOT ticked and item 224 is NOT retired.** The
criterion asks for POPULATING against a real session, and claiming that today
would be exactly the "recordings that record nothing" failure this desk keeps
hitting.

## TESTS

`tests/test_realised_sector_weights.py`, eight tests: grouping by
`(sector, side)`, the denominator string, unknown-is-NULL, built-nothing is not
zero concentration, one row per run, the product call site exists, nothing reads
the table back, and the recording returns False rather than raising when the
write fails.
