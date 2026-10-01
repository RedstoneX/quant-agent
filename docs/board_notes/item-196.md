## item 196 — RETIRED 2026-10-01, both criteria met: refusing was MEASURED to be the better policy and the frequency it left unmeasured is now counted in production

The open half was a doctrine question — when the chandelier candidate itself
lands inside the noise band, is refusing to move correct (the move would be
noise) or a protection failure (the stop should have tightened and did not)?
It is answered with a measurement, not an argument.

MEASURED, on the desk's own stored daily bars (101 symbols x 276 bars,
`scratchpad/zone/bars400.pkl`), replaying the live `evaluate_trailing_stop`
geometry — same chandelier, same 1.25-ATR band, same 2% minimum ratchet, same
derived opening level for both policies — against the only alternative the
item named, falling back to the band edge `current_price - 1.25 * ATR`:

* Over a 14-session horizon (the LONGEST round trip in the production record,
  `trades` table, 19 closed round trips, min 0 / median 3 / max 14 days):
  the fallback changed the exit on 28 of 4,877 simulated holdings. 15 were
  WORSE by a mean 1.44 ATR, 13 were BETTER by a mean 0.61 ATR; the mean of
  all 28 is -0.49 ATR. Tightening into the band loses more when it is wrong
  than it saves when it is right.
* Over the median 3-session horizon the fallback changed 2 exits and both
  were worse.
* A candidate lands inside the band on 9.1% (3 sessions) to 18.6% (14
  sessions) of evaluations, but only 84 of 9,902 in-band events would have
  produced a band-edge level that both beat the resting stop and cleared the
  minimum ratchet — so the path is common and its consequences are rare.

So refusing outright STANDS, now on evidence rather than on the doctrine
conflict alone (the band edge is read off today's price, which this module's
own `_swing_lows` docstring rejects, and a level-based exit conflicts with
the ratified rule to exit on ALIGNMENT, never on a level).
`tests/test_trail_code_census.py` pins the refusal alongside
`tests/test_trailing_candidate_set.py` and carries the measurement in its
docstring so a later patch cannot reopen it without answering the numbers.

The second criterion is now built rather than argued.
`record_trail_state_if_changed` writes nothing when a stock refuses for the
same reason two runs running — bounded by design — so it can say WHY a stop
has not moved and never HOW OFTEN, which is exactly why the production record
carried ZERO `inside_noise_band` rows (MEASURED 2026-10-01 on production:
37 `trail_state` rows over 5 days, codes `no_structure_and_no_usable_chandelier`
11, `move_smaller_than_min_ratchet` 10, `trailed` 7, `range_below_target_not_yet_1r`
5, two others 2 each, and no noise-band refusal at all). A per-run census
(`kind='trail_code_census'`) now counts EVERY trail outcome once per run in
one portfolio-scoped row, so the frequency becomes readable without a row per
stock per tick. Recording only: nothing reads it back to decide anything.

CARRIED FORWARD and still barred: criterion 195/2, that `PIVOT_WINDOW` cannot
confirm a swing low inside this desk's typical holding period (median 3
sessions, MEASURED above, against a window needing `2 * PIVOT_WINDOW + 1`
bars), remains unfixable while that constant is documented as unsourceable.
No number was invented here; the band width, the chandelier multiple and the
minimum ratchet are all unchanged.
