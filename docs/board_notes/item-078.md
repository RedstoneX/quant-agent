## item 78

**CORRECTED 2026-10-09 — the "blank 54-68% of the time" figures below are wrong.** Measured on production agent_logs since 2026-09-25: the technical seat leaves "I'll sell if" blank ONLY when it rates a stock NEUTRAL (231 of 231 neutrals), which is what its prompt tells it to do; on every buy, sell or strong rating it filled the box (173 of 173). The earlier counts mixed neutrals in. The blank-rate condition is CLOSED; the older dated entries below are kept as history and are superseded by this one.

**Plain language —** You locked a standing rule: if something the desk needs is missing, find why and make that step actually produce it. Do not invent the missing words. Do not make "drop this name and trade the rest" the standing answer. The current case is a blank "I'll sell if". A temporary patch currently drops that name after we already asked twice, so one blank cannot veto the rest of the book. That patch is not the fix. The real path is: the seats write a real "I'll sell if" before a buy or short can be ticketed; a sentence the model already wrote is put back if a later wipe blanked it; the seat is asked once more; never invent the words. If it is still blank, that name is refused. A catalyst note stays optional.
**Example —** A buy on a chip stock arrives with prices and a stop but the "I'll sell if" box is empty. The desk does not make up a sentence, does not let that blank name veto the rest of the book, and does not ticket it. After one re-ask still blank, that name is refused and the others can proceed. The standing design is that the box is filled, not that the name is dropped.
**The decision —** You locked the standing rule. The temporary drop-the-name patch stays until a live session proves the seats actually fill the box. The rule is not only about "I'll sell if" — any missing required field is the same class of defect.
**Recommendation —** Keep the never-blank path. Keep the drop-the-name patch labelled temporary. Do not treat skip-and-continue as the product.

**Checked again 2026-09-30 — the answer is still no, and now with numbers.** The technical seat left the "I'll sell if" box empty on 60% of the stocks it looked at on the most recent trading day, 29 September (134 of 223), and on 54% the day before. That is the same as it was through the whole of the previous fortnight, where the daily figure moved between 30% and 73%. The seat's answer format was tightened on 25 September to force a strict shape, and the figures after that change are indistinguishable from the ones before it, so the tighter format did not make the seat do the work. On the much smaller set of names that actually reached a buy or short, the seat was still blank 5 times out of 75. Separately, two of the three things the code itself says must be shown before the patch can be removed cannot be checked at all: they are claims about the repair step, and the repair step for this particular box has never once written down what it did, so there is nothing to read. The patch stays. To judge this item next time, the repair step must record, per stock per session, whether it was tried, skipped, blocked or paid for.

**Checked again 2026-10-01 — still no.** On 30 September the technical seat left the "I'll sell if" box empty on 53 of 78 stocks (68%), so the blank rate has not come down; the portfolio manager filled it on both of the 2 names it targeted, which is too few to prove anything. The repair step still has no record of what it did, so the other two conditions still cannot be read. Source: live database, not the empty repo copy.

**Moved from WORK.md (2026-09-24) —** Plain-language account: `docs/BOARD_NOTES.md` ("item 78"). Permanent fix: heal, then one paid retry; still blank → refuse before the book. Never invent. Delete the isolate when a live session proves never-blank.

**Re-measured 2026-10-01 (live database, read-only) —** the blank rate has NOT moved: 53 of 78 (68%) on 30 September, 134 of 223 (60%) on 29 September, 14 of 26 (54%) on 28 September. The item's recorded figures are correct, so the isolate stays exactly where it is.

**Built 2026-10-01 — the repair step now writes down what it did.** The mechanical repair (it puts back the "I'll sell if" sentence the model itself already wrote, after a later step blanked it) ran inside a parsing step that had no run id and no database handle, so it had never recorded a single thing and two of this item's three removal conditions could not be read at all. Each run of it now parks one observation — the stock, whether the box arrived empty, whether the repair filled it, and from what source — and the session drains them to a new durable table once per run. Unknown stays empty: a payload with no stock name records nothing in that column rather than a guess, and the source column is empty unless something was really put back. RECORDING ONLY: nothing reads these rows back into a trading decision and they may never be swept for a threshold, which is written into the table's own comment. Classification: UNPROVEN — the write is reached from the main session path (the portfolio stage of `run` in `src/pipeline_stages.py`, call site line 6819), and tests prove the row contents, but no live session has run since, so no production row exists yet. It becomes POPULATING on the first live session that parses a seat answer.


**Criterion detail moved off the board 2026-10-01** to bring the item
inside its per-item byte budget. Each criterion keeps its statement on the
board and its full text here.

- MEASURED 2026-09-30 against the live database, condition NOT met: the  ... on the narrower set of names that actually became targets the seat was still blank 5 times in 75; and criteria 2 and 3 in the docstring of `_isolate_empty_soft_exit_entries` CANNOT BE EVALUATED AT ALL because no soft-exit heal row has ever been written — all 56 `seat_heal` rows in the database carry gate `seat_heal` for the news, smart-money and technical seats and none of them is the soft-exit gate, so before this item can be judged again the soft-exit heal path must record its own outcome row (`not_attempted`, `cap_blocked`, `failed`, `paid_retry`) per name per session.

- MEASURED AGAIN 2026-10-01 against the live database (specialist_eviden ... 327 technical-seat analysis rows since 2026-09-26): condition STILL NOT met, the technical seat returned a blank or `unknown` `thesis_invalid_if` on 53 of 78 stocks (68%) on 2026-09-30, after 134 of 223 on 2026-09-29 and 14 of 26 on 2026-09-28, so the blank rate has not fallen; the portfolio manager emitted a falsifier on all 2 targets it wrote on 2026-09-30, but 2 is too few to demonstrate anything; zero `soft-exit missing after retry` refusals and zero `soft_exit_heal` rows exist, so criteria 2 and 3 are still unevaluable. Do not re-measure until the soft-exit heal outcome row exists.

## The blank rate is measured against the wrong denominator (2026-10-02)

**Measured read-only against production (`agent_logs`/`trades`, 84 rows, latest
2026-10-01 13:49):** 44 of 84 trade rows carry no falsifier, which is the ~52-68%
figure this note has been quoting. That denominator is wrong. Broken out by
action:

  BUY               36 rows,  1 blank
  SHORT              5 rows,  0 blank
  SELL               4 rows,  4 blank
  STOP_OUT           9 rows,  9 blank
  TRAIL_STOP         9 rows,  9 blank
  SWEEP_BUY          8 rows,  8 blank
  SWEEP_SELL         6 rows,  6 blank
  PARTIAL_SELL       3 rows,  3 blank
  REDUCE             2 rows,  2 blank
  HOLD               2 rows,  2 blank

A falsifier is the ENTRY's statement of what would prove its thesis wrong. An
exit, a stop-out, a trail adjustment and a sweep do not have a thesis to
falsify, so a blank on those rows is correct and not a defect. The population
this item is about is entries: BUY and SHORT, 41 rows, of which **40 carry a
falsifier and 1 does not**.

**The one exception predates the gate.** ORCL BUY, 2026-09-02 18:32:45, stop
137.53, reasoning present, `thesis_invalid_if` NULL. The requirement that a
name cannot enter the ticket book without a real "I'll sell if" shipped after
that date, and no entry since carries a blank.

So the entry-side falsifier requirement is HOLDING in production (40/41, the
one miss pre-dating enforcement). Any remaining work on this item is about the
QUALITY of the sentence the seat writes, not about its absence — and the heal
built on 2026-10-02 closes a latent blanking path, not a live one, exactly as
its author said.

## The never-blank path is live (2026-10-04)

**Box 1 of 4 ticked.** The three behaviours now all exist on the live path,
and the two that were missing were missing for the same reason: the heal
could only see one of the ways a falsifier gets blanked, and the refusal
was never counted.

- **Healed from the model's own sentence.** The existing mechanical heal
  runs inside the null-drop validator, so it can only repair a wipe that
  happens inside that validator. A blank produced anywhere else left the
  sentence the model actually wrote sitting unread in the raw seat output,
  and the desk's only remaining moves were to pay for a sentence it already
  had, or to refuse a name the seat had in fact answered. The heal now also
  runs one level up, against the raw payload, immediately before any spend.
  It copies a stated string and nothing else.
- **Re-asked once, paid.** Unchanged. The paid re-ask is now reached only
  when the sentence genuinely does not exist anywhere.
- **Refused before the book, and COUNTED.** The per-name rows said which
  names were refused; nothing counted them, so a run that refused four
  names read exactly like a run that refused none. One counted row per run
  now carries the count and the heal outcome behind each refusal.

Nothing here invents a falsifier, and no refusal was converted into a
silent skip. The other three boxes stay unticked: each needs a live session
or a live-database measurement, and the desk is off.
