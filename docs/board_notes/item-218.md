## item 218 — RECORD ONLY: two measurements, no behaviour change; the parity refusal was built and then REMOVED before merge

**Nothing in this item changes what the desk does.** A refusal of a range buy
whose reward:risk is below 1.0 ("parity") was written, reviewed adversarially
and deleted. Two measurements survive, as evidence for this board item only,
with no behaviour attached to either.

**Measurement 1 — the production reward:risk distribution.** Measured
2026-10-01 against the production database, read-only, over the 33 recorded
BUY trades that carry an entry, a stop and a target: median reward:risk 1.44,
minimum 0.68, and SIX below parity — RSG 0.76, RSG 0.90 and NUE 0.82 (range
entries) and COP 0.68, OXY 0.87 and RKLB 0.82 (breakout entries). Recorded
target distance over the same set is a median 3.25 ATR. This is the state of
the book's geometry on that date and nothing more; it is not a threshold, not
a ranking input and not a size input.

**Measurement 2 — the realised-advance study behind the reach cap.** Measured
2026-10-01 on the desk's own stored 400-bar daily set, 101 symbols, ATR(14),
rolling windows: over a 15-session hold the MEDIAN per-name realised
favourable excursion is 1.93 ATR and the per-name MAXIMUM is 8.78 ATR, against
`MAX_REACH_ATR_MULTIPLE`'s 1.5 * sqrt(15) = 5.81 ATR. `MAX_REACH_ATR_MULTIPLE`
is KEPT and unchanged; the note now sits beside it in `src/data/levels.py`.

**Correction to the record.** The reach cap was investigated and it is NOT
what holds the desk's targets close. An earlier diagnosis written down in this
repo treats the reach multiple as the thing clipping targets in; the
measurement above contradicts it — at a typical hold the cap sits at ~5.8 ATR
while the instrument's own typical advance is ~1.9 ATR and the recorded target
distance is a median 3.25 ATR, so the cap binds only in the tail and not on
the ordinary trade. Whatever keeps targets near entry, it is not this number.
A measured replacement for the multiple would also still need a QUANTILE — the
median (1.93) and the maximum (8.78) differ by 4.5x and straddle today's value
— so reading the instrument does not avoid picking a number.

**Why the refusal was removed.** It keyed off the WIDENED stop inside the
stop-widening path, so it fired as a function of stop width — the deleted
stop-width gate under a new name — and the owner's standing ruling is that a
wide stop ships and is answered by SMALLER SIZE, never by refusing the trade.
Seven existing guard tests fail against it, four of them the owner's own
worked examples of that ruling, and they are left untouched. The "parity is
arithmetic" argument does not survive either: the break-even identity assumes
the position is SOLD AT THE TARGET, and this desk never does that — profit
taking is trailing-stop driven (owner 2026-09-30, exits on alignment), so the
reward side is a FLOOR on the payoff, not the payoff, and 1.0 is not the
structural bound the change claimed. A size-based variant is barred too: the
reward:risk helper's own docstring records the owner ruling that the figure is
for RANKING, never a cutoff and never a size cap. The breakout exemption was
also backwards in effect — it spared the three worst measured ratios (COP
0.68, RKLB 0.82, OXY 0.87) and refused three better ones.

**What is left open, and it is an owner question.** The desk can presently
neither refuse arithmetically losing geometry nor resize for it, because the
ruling set forbids both. Only the owner can say whether such a trade may ship
at all. That question is recorded here and deliberately not routed, not
answered and not pre-empted by this branch.


**Why the breakout exemption was dropped at this gate.** The exemption's own
stated reason is that a trend trade's reward number is invented. That makes the
honest question whether the desk's level computation actually found a level
above entry, not whether an analyst typed "breakout" into a label.

**The corrected measurement, and what the first one got wrong.** This item
first recorded median 1.44, minimum 0.68 and six buys below parity. That read
the LIVE `stop_loss` and `take_profit` columns on 32 rows, which are
post-management trailed values, not what the gate sees at decision time. The
corrected figures below come from `initial_stop_loss` and
`initial_take_profit`.

**Why PARITY and nothing above it.** Parity is where the line sits because the
owner ruled refusal and parity is the only line that needs no invented value —
NOT because it is mathematically derived. The risk side is a real transactable
price; the reward side is the nearest structural level above entry, a forecast
this desk never actually sells at, since it rides a trailing stop out instead.
So the ratio compares one real number against one estimated one. He knows that
and accepted it.

**2026-10-01, the owner then RULED for a refusal after all**, on geometry rather than on the recorded target. What follows is that build's own note; the measurements above are the evidence it rests on and are kept deliberately.


WHY THIS IS NOT THE DELETED STOP-WIDTH GATE, checked algebraically rather than asserted. The ratio is (level_used - entry) / (entry - final_stop). Its numerator is a price the level computation found on the chart (`derivation.level_used`, admitted only when `derivation.basis == "structural_level"`), so it is an independent reading and not a function of entry or of the stop; nothing in the numerator is derived from the stop distance, from ATR, from a multiple of either, or from the recorded take-profit target, which this gate never reads. Because numerator and denominator share no common factor that can be cancelled, the ratio cannot reduce to a constant nor to any expression in (entry - stop) alone: hold the stop fixed and move the level and the verdict changes, which a width test cannot do. The three stand-downs exist for exactly the cases where the numerator would stop being a reading — no level at all, a level past the horizon reach, or a reward the code has already labelled one-session noise — and in each the gate does not fire, nothing is invented, and the stand-down is recorded with its basis and the level it saw. The durable row keeps entry, stop, the level used, the ratio, the threshold and `level_was_measured`, so a later reader can recompute both distances and confirm the refusal was geometric.

The ruling, verbatim: "For now, let's refuse a bad risk reward ratio. See if that improves the desk purchases." The owner was shown buy at 100, stop at 94, nearest structural level above at 104 — risking 6 to make 4 — and chose refusal over both leaving it alone and shrinking the position. That supersedes the previous standing rule, which was that a wide stop ships and is answered by a smaller position, never by refusing the trade.

WHERE THE THRESHOLD SITS, AND WHY THE RECORD MUST NOT OVERSTATE IT. Parity, and nothing above it. Parity is where it sits because the owner ruled refusal and parity is the only line that needs no invented value — the boundary between arithmetically losing and arithmetically winning geometry, statable without picking a number, where any higher figure (1.5, 2.0) would be an arbitrary number and is barred. Parity is NOT mathematically derived. The ratio compares one real number against one estimated one: the risk side is a price the desk will actually transact at, while the reward side is the nearest structural level above entry, which is a forecast, and this desk never actually sells there — it rides a trailing stop out. The numerator is a yardstick, not a plan. The owner knows this and accepted it, and that caveat is written into `src/risk/constants.py::REWARD_RISK_PARITY` as well as here.

WHERE IT IS MEASURED. At the point where the trade as a whole is accepted or declined, on the final entry, the final stop and the derived target. Deliberately NOT inside the stop-widening path: keying the comparison off the widened stop makes the refusal a function of stop width, which is the deleted stop-width gate (item 56) wearing a new name. Nothing is resized and no stop, target or trailing behaviour changes — refusal is the entire mechanism.

THE BREAKOUT EXEMPTION WAS DROPPED AT THIS GATE, argued from the code's own reasoning rather than from precedent. `reward_risk_floor_applies` exempts a Type B / breakout trade because for a trend trade the reward number is INVENTED — nothing overhead is being defended, so the numerator is a figure produced to satisfy a ratio. The honest test of that proposition is the measurement, not the analyst's word, which is exactly what the measured half of `is_trend_trade` was added for. So the refusal applies whenever the desk's own level computation actually found a structural level above the entry, and stands down when the target had to be projected instead. The production record says the same thing: of the eleven sub-parity buys, five are labelled breakout, including the two worst ratios in the whole book, so a label-keyed exemption would spare the worst geometry the desk has ever bought while refusing better trades.

MEASURED EFFECT, AND A CORRECTION TO THIS ITEM'S EARLIER NUMBERS [measured 2026-10-01 against the production database at /home/qamc/quant-agent/data/quant_agent.db, read-only]. This item previously recorded median 1.44, minimum 0.68 and six buys below parity. That figure read the live `stop_loss` and `take_profit` columns, which carry post-management trailed values rather than the decision-time geometry this gate sees, and covered 32 rows. Against the decision-time columns — `initial_stop_loss` and `initial_take_profit`, 33 BUY trades carrying all three prices — the median is 1.24, the minimum 0.42, and ELEVEN buys would have been refused: 0.42, 0.44, 0.46, 0.68, 0.76, 0.79, 0.82, 0.87, 0.90, 0.95, 0.96 (symbols withheld: the repo is public), which is an UPPER BOUND on refusals and not a prediction, because it is computed from the reach-capped target this item argues is the wrong numerator. The `structural_ceiling` column is NULL on every one of those rows, so the split between measured-level and projected targets cannot be read from the stored record; the eleven are the upper bound on what the rule would have refused.

RECORDED WHEN IT FIRES. A durable per-symbol refusal row carrying the code `reward_below_risk_at_parity`, the measured ratio, both prices it was measured from and the caveat, so the owner's "see if that improves the desk purchases" can be answered later from data rather than from impression.

