# Future — Portfolio Hedging

**Status: not built, not scheduled. Owner asked for this to be written down on
2026-09-01 so it is not lost.** Nothing here is approved work.

**Prerequisite, stated plainly: do not start this until the desk is stable and
placing trades reliably.** As of 2026-09-01 it went a full session finding 38
actionable signals and placing zero orders. Hedging a book that will not trade
is solving the wrong problem in the wrong order.

## What exists today

**No hedging logic exists anywhere in QAMC.** Verified 2026-09-01 by searching
`src/` and `config/prompts/` — every occurrence of "hedge" is either incidental
prose in a docstring or the `is_bearish_hedge` FLAG on a candidate, which
merely labels a bearish idea. Nothing sizes, pairs, or manages a hedge, and
nothing reasons about net exposure as a deliberate position.

## Why the inverse ETFs stay

`SH`, `SDS`, `PSQ`, `SQQQ` remain in the universe. The owner reconfirmed this
on 2026-09-01 after asking whether they were redundant now that shorting is
wired: **"maybe let's keep the inverse ETFs, that could be utilized as a
hedge."**

Two facts make that the right call for now, both measured rather than assumed:
1. **None of them can be shorted at the broker** (`shortable: false`). They are
   only usable as longs — which is exactly what a hedge sleeve wants.
2. **Shorting has never actually executed.** The trades ledger holds 45 rows on
   2026-09-01 and **zero** are `SHORT` or `COVER`. Shorting is wired in code and
   unproven in the real world. Until one short fills and its stop behaves, the
   inverse ETFs are the only bearish tool that has ever worked.

Revisit removing them only after a real short has filled and exited cleanly.

## What "hedging" could mean here — none of this is decided

Listed roughly cheapest-to-hardest, so a future session can start small:

1. **Index hedge sleeve.** A bounded allocation to an inverse ETF (or a short
   SPY/QQQ once shorting is proven) sized off measured book beta rather than
   picked by feel. Answers "the book is 90% long into a macro shock" without
   liquidating conviction positions one by one.
2. **Net-exposure targeting.** Make net long-minus-short an explicit, sized
   output of the macro read — the natural continuation of Phase 10.2's "macro
   sets exposure, not selection". Today nothing computes or targets net
   exposure at all.
3. **Correlation-cluster hedging.** `src/data/correlation.py` already measures
   return correlation for the cluster risk budget. The same measurement could
   size a hedge against the dominant cluster instead of only capping it.
4. **Event hedging.** Bounded protection into a known binary — FOMC, CPI, an
   earnings date the book is concentrated into. The earnings and FOMC calendars
   already exist in the system.

## Traps to design around, learned elsewhere in this project

- **A hedge is a position, not an exemption.** It must carry a stop, appear in
  exposure maths, and be reviewable like anything else. The 2026-08-28 incident
  where positions were closed by a broker-resident stop and the ledger never
  heard about it applies here too.
- **Inverse and leveraged ETFs decay** through daily rebalancing. They are
  short-horizon instruments; a "hedge" left on for weeks bleeds. Any sleeve
  needs an explicit maximum holding period.
- **Under margin, a hedge consumes gross exposure.** With Phase 11.2's cap,
  hedging is not free — it competes with the very positions it protects. Decide
  deliberately whether hedges count against the cap.
- **Do not let a hedge become a way to avoid selling a losing position.** That
  is the failure mode this feature invites: it converts a clear exit decision
  into a more complicated book.

See `docs/QAMC_REMEDIATION_SPEC.md` Phase 10.2 (macro sets exposure) and
Phase 11.2 (margin ceiling), both of which this would build on.
