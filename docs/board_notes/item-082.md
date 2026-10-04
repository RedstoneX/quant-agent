# Item 82 — why `structural_ceiling` and `stop_basis` are empty on every entry row

**MEASURED 2026-10-04 against the live database, read-only. Neither column has a live code defect; both emptinesses are fully explained, and the explanations are different.**

## `structural_ceiling` — NULL on all 41 entry rows, and every one of them is accounted for

The write shipped on 2026-09-24 22:00 ET (`Item 82 residue: persist the measured breakout verdict`). Production was running a commit containing it from then on [measured: the deployment reflog, plus `merge-base --is-ancestor` against the commit checked out at the time of each later entry].

Only **three** entry rows were written after that date, on 2026-09-30 and 2026-10-01. **All three are scale-in ADDs onto positions opened before the column existed** [measured: each shares its `position_id` with an earlier BUY, the oldest dating to 2026-09-17]. On a scale-in the desk deliberately carries the position's OWN pinned verdict forward and writes NULL where the position holds none — see `src/entry_evidence.py`, which exists to make absence reportable as absence rather than backfilled from a later top-up's reasoning. The remaining 38 rows predate the column.

So: **no fresh entry has been executed since the feature shipped.** The fresh-entry path is already covered end to end by `tests/test_buy_ceiling_full_path.py`, which drives the real stages and asserts the constructor's measured verdict reaches the row. Nothing to fix here; the first genuine new position will populate the column.

## `stop_basis` — NULL for a different and real reason: the rule that placed the stop is computed and thrown away

`stop_basis` is written on every entry from `TradeDecision.stop_rule`, which the constructor sets from `shipped_stop_rule`. That function answers one question only — is the shipped stop sitting at a computed level — and returns `STOP_RULE_LEVEL_HONOURED` or **None for everything else**. The column's whole domain is therefore one value plus NULL.

Meanwhile `_widen_stop_past_noise` (`src/portfolio_constructor/entry_stop/resolver.py`) already names the exact rule that placed each stop — ATR noise band, absolute ATR floor, kept outside the band, past the signal bar, no ATR reading — and **discards that name**: the function returns a bare float and only logs the rule. The three most recent entries confirm this: their `stop_level_basis` records `"shipped_stop_rule": null` with `"level_backed": false`, i.e. the stop was genuinely not level-backed, so NULL is the honest answer to the question the column currently asks [measured].

**This is a recording gap, not a wrong value, and it is NOT fixed here.** `TradeDecision.stop_rule` is behavioural — execution reads it against `LEVEL_BACKED_STOP_RULES` to decide whether its own ATR floor applies — so widening what it carries changes trading behaviour and must not be done as a side effect of a recording fix. The fix is to surface the resolver's already-computed rule name as a separate recording-only field and store THAT in `stop_basis`. Flagged, scoped, deliberately left open rather than half-shipped.
