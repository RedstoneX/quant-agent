## item 186

Full text of the DONE WHEN lines moved out of docs/WORK.md to fit the per-item byte budget; ticks are unchanged.

- [ ] the short-side haircut CLOSES ON RECORDED EVIDENCE, NOT ON A THIRD DERIVATION. Two attempts to read the 1.5 off the instrument were made and both withdrawn 2026-09-30, with NO sizing change shipped: (1) worst historical gap over the fetch lookback — dead, that lookback is a 5-year window chosen for chart structure and a max over a fixed window can only grow, so one old gap governs size for years and a regime change cannot update it; (2) stop distance plus one ATR — dead on algebra, the stop is itself 2.5 ATR so the multiple is (2.5+1)/2.5 = 1.40 for every name, the per-name ATR cancels, and it is LOWER than the 1.5 it replaced. Do not attempt a third derivation. The criterion is now that the desk has RECORDED, for shorts it opened and closed, the adverse overnight gaps actually suffered beside the entry volatility read and the stop distance — shipped 2026-09-30 as `trades.max_adverse_overnight_gap` / `overnight_gap_sessions` joined to `entry_atr` and `initial_stop_loss` — and that enough closed shorts exist to read the distribution. Recording only: nothing reads it back into sizing. Separately, this number binds at EXECUTION, not in the constructor (see item 216)

- [x] RULED OUT 2026-10-04, not pending: this was filed as an owner-appetite dial and the owner has now answered it more than once -- "if the market is closed, what is the point of a stop?" A stop is an instruction to the exchange; with the exchange shut nothing can trigger it, so an overnight gap is not a stop failure and not a tolerance to be set. Asking for a percentage also breaches the standing 2026-09-30 ruling that risk is read PER NAME off the instrument's own behaviour and the seats' conviction, never as a global constant. The remaining work is therefore a BUILD, not a question: read the gap exposure from that stock's own overnight gap history, which is blocked only on the desk keeping stored daily bars. If it cannot be read per name, the honest outcome is that the desk does not take the position.

- [x] the queued-earnings BUY clamp (5% of the book) stops being a global share — SHIPPED 2026-10-01 as the structural alternative, not a re-derivation: the `queued` flag marks a filing the pre-market preprocess FAILED to analyse, so this is MISSING EVIDENCE and the BUY is refused on its own reason prefix (not the conviction bar's, which rotation string-matches on held names) and recorded per symbol; the 5 is deleted from the ledger; measured on the retained record the gate has never fired and no queued-unread filing appears at all, so no recorded BUY changes and no held position is affected. Detail: docs/board_notes/ (item 186)

- [x] THE RECORDING IS BUILT, 2026-10-02 — the two ceilings whose settlement route was `state: specified` (25 total at-risk, 40 cluster share) now have the write that can actually settle them: one durable row per run in `realised_risk_budget` carrying the book's realised committed at-risk, the held book's own at-risk, every correlation cluster's share OF THAT COMMITTED TOTAL (the unit the 40 is written in), the equity it was measured against and the session date, taken from the allocator's own output rather than recomputed so the row cannot disagree with the sizing it describes. RECORDING ONLY and nothing reads it back; a run whose held-book risk was unreadable writes `allocator_ran` 0 with the measurements NULL, because the ceilings going unenforced is the finding, not a book with no concentration. Both ledger rows move `specified` -> `built` with their `writes:` fields; NO ceiling value changed, no appetite was asked and none was picked.

- [x] the portfolio and cluster ceilings (25 total at-risk, 90 terminal sector and its constructor mirror, 40 cluster share) each end in a definite state rather than as an open appetite question — 2026-10-01: values unchanged and still owner-ratified, every appetite question WITHDRAWN under the 2026-09-30 ruling, both failed derivations written down per ceiling (two of them algebraic cancellations: 25 is five full-size names and 40% of 25% is two, at the ratified 5% per-trade envelope), and each row now names the recording that would settle it with the route ratchet moved to match. Do not re-derive these three

## 2026-10-04 — the gap-haircut box: data blocker disproved, derivation blocker found

Measured, not argued: 9,500 real overnight gaps (next open versus previous
close) across 19 symbols, 2024-09-30 to 2026-09-29, from the daily-bar dataset
already accepted for the alignment measurement. Harness
`measure_gap_per_name_186.py`, kept in the measurements directory outside this
repo. Nothing here is fitted to the desk's own trading record; these are market
bars only.

- The old blocker is wrong. Daily bars carry the previous close and the next
  open, which is exactly what an overnight gap is, so gap behaviour is
  measurable offline today. (Separately, the constructor still is not *handed*
  bar history at runtime — that plumbing gap is real, but it was never the
  reason the number could not be derived.)
- Names differ enormously in gap SIZE: gap standard deviation runs 0.55% (RSG)
  to 4.62% (FLNC), an 8.4x spread; the 95th-percentile gap runs 0.59% to 5.45%.
  All 19 symbols carry 500 bars, so every one has enough history to be read and
  the too-little-history fallback case is untested by this sample.
- But `short_gap_risk_multiple` is not a gap-size number. It divides a short's
  position size relative to the equivalent long, so what it has to express is
  the directional ASYMMETRY of overnight gaps: how much worse an overnight move
  is for a short than for a long in the same name.
- That asymmetry is near one and barely varies. Ratio of mean adverse (upward)
  gap to mean adverse (downward) gap: median 1.01, range 0.91 (AAPL) to 1.18
  (MRVL). Ten of 19 names exceed 1.0; none exceeds 1.5. At the 95th percentile
  the median ratio is 0.92.
- The per-name differences are inside the noise. The 0.27 spread across names
  is smaller than the median 0.17 swing a single name shows between the first
  and second halves of its own two-year sample (FLNC swings 0.58, AMD 0.41).
  A per-name read off this quantity would mostly be reading sampling error.
- Consequence: a multiple honestly derived from this data is about 1.0 for
  every name, which means removing a live-money short-side haircut. The
  haircut's stated grounds — unbounded loss, borrow recall, short squeeze — do
  not appear in overnight-gap data at all, so this measurement cannot be used
  to justify removing it either. Removing it is not what this box asks for.
- The alternative derivation, reading the multiple off gap SIZE rather than
  asymmetry, changes what the number means and still requires two picked
  values: the reference gap level that maps to a multiple of 1.0, and the tail
  percentile that defines "the gap risk". Neither is supplied by the data.

THE SPECIFIC CHOICE THAT BLOCKS THE BOX: nothing in the bar data supplies the
mapping from a name's gap dispersion to a position-size divisor. Until that
mapping comes from somewhere other than a pick, the box stays open. A fallback
for short-history names was not reached, and would in any case still be a
constant.

The box text in docs/WORK.md has been corrected to carry this blocker in place
of the disproved one. The value 1.5 is unchanged and no ledger row was touched.
