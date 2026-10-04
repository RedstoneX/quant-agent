## item 55

**Plain language —** A "level" is a price the stock has bounced off before, and the desk uses them for almost everything — where to put a stop, whether a trade is worth taking, how big it can be. Three things define one. On 13 September the popular trading-software documentation was read and answered none of them. Later the same day the ACADEMIC work was found, and it changes the picture in three ways. First, it settles one of the three: a level needs at least two bounces, and a study of 733 US stocks over twenty years measured that demanding three or more makes no difference to how often price actually turns there. That number is now sourced rather than assumed, and locked so nobody quietly raises it. Second, it confirms that the desk's whole method — find the bounces, group the ones at similar prices, treat the group as a band — is the same method the academic work uses, so the design is not home-made. Third, on the two numbers still open, it does not give an answer but it does say where the desk is standing: the same study checked band widths from 2% up to 5% and found the results did not change, and the desk's band is 2% — the very tightest they looked at. Nobody has measured anything narrower.

**Example —** On a $200 stock the desk's band is $4 wide. Two bounces $1.90 apart are "the same level"; bounces $2.10 apart are two different levels. That single call decides whether a stop counts as sitting on real structure — and a stop that does gets honoured as-is, while one that does not gets pushed wider, which shrinks the position. So the width is quietly sizing trades, and the desk is running it at the edge of the only range anyone has tested.

**The decision —** None for you. It is a chart-structure question, so it goes to research, not to your judgement. It is on the board so that it gets answered rather than sitting in a code comment forever.

**Recommendation —** There is now a specific, runnable experiment rather than a wish. The academic study's own test — count how often price entering a band leaves the way it came, and compare that against bands drawn at random — has never been run on this desk's own stocks at this desk's own settings. Run it, and sweep the width and the bounce definition across a range. Either the desk's setting shows a real effect, or the effect is flat everywhere, in which case the honest answer is that the width does not matter and this closes. If it is flat, the better prize is still available: drop the percentage entirely and let the band be the actual height of the bars that made the bounces, so the stock states the width and the desk states nothing.

**Moved from WORK.md (2026-09-24) —** Open, both convention: the pivot window is 3 in one module and 5 in another, and the cluster tolerance is a flat 1% (a 2% span). **Every ruled-out source, and why harmonising the windows is not an answer: `docs/INCIDENT_HISTORY.md`, 2026-09-14. Do not re-search.** **Settles with** Tsinaslanidis §4.5's bounce test on the desk's own universe and bars, sweeping tolerance 0.5/1/2/3/5% and window 3/5/10/25 — a reading, not a fit; if flat, prefer a zone equal to the span of the pivot bars, which needs no constant. Cost: 1% decides "the same level", hence whether a stop is level-backed, the ATR floor, R/R and size.

**Re-verified 2026-09-30, STILL OPEN, and nothing was changed — the blocker is DATA ACCESS, not analysis.** Live code confirms neither number moved: `src/data/levels.py::PIVOT_WINDOW` is still 5, `src/data/levels.py::CLUSTER_TOLERANCE_PCT` is still 1.0, `src/risk/trailing.py::PIVOT_WINDOW` is still 3, and `config/number_ledger.yaml` still carries both as `status: arbitrary`. The "runnable experiment" in the Recommendation above is NOT runnable from a build worktree, and saying it was is the part of this note that was wrong: there is no local bar cache in the repo, `tests/fixtures/` holds no OHLCV series, and the only daily-bar source in the codebase is `src/execution/broker.py::get_bars`, which needs broker credentials. So the Tsinaslanidis 4.5 bounce sweep over this desk's own universe cannot be run by an agent that is barred from production credentials — it needs either a one-off bar pull into a committed fixture, or the rehearsal account's read path, and NEITHER EXISTS YET. That is the real next step for this item, ahead of any sweep.

**What a threshold-free answer would look like, recorded so it is not re-derived, and deliberately NOT shipped.** For the ZONE there is a genuine candidate that invents nothing: two pivots belong to the same level when the HIGH-LOW RANGES OF THE BARS THAT MADE THEM OVERLAP, and the level's zone is the union of those bar ranges. That reads the width off the instrument's own volatility — a wide-range bar states a wide level, a quiet one states a narrow level — and it removes both `CLUSTER_TOLERANCE_PCT` and the percentage in `level_zone_halfwidth` rather than replacing them with another constant. For the BAR COUNT there is NO equivalent: every candidate (a fixed window either side, a reversal of N ATRs, a zigzag percentage) ends in a picked multiple, and the only window that is not picked is the minimum symmetric one, which is a choice dressed as a derivation. So half of this item has a threshold-free form available and half does not.

**Why the zone change was not shipped anyway.** It moves where protective stops sit on live positions — a wider or narrower zone changes whether a proposed stop counts as level-backed, which changes whether it is honoured as-is or pushed wider, which changes size. The same missing bar data that blocks the sweep also blocks measuring how many currently-open positions would get a different stop, and shipping a stop-placement change with that number unmeasured is exactly the move this desk does not make. Change nothing was the correct outcome of this pass.

**Measured cost, new and belonging here:** a confirmed pivot on the trailing path needs 7 bars (`src/risk/trailing.py::PIVOT_WINDOW` = 3, so 3 either side plus the pivot), and the structural leg of the trailing stop has NEVER ONCE produced a candidate on a real position — partly because holds have run 4-9 sessions, which cannot reliably contain a 7-bar confirmation plus room to trail from it. So the bar count is not merely unsourced; on the trailing path it is currently switched off by arithmetic. This is the strongest argument yet that the window is the half of this item worth settling first, and it is an argument for MEASURING it, not for lowering it.

**RULED and BUILT 2026-10-01 — this closes on the desk's own record, not on another argument.** The rework offered for this item (PR 880: complete-linkage clustering plus a redefinition of what makes a stop "level-backed") is HELD and will NOT be merged: measured against the eleven real open positions it was a net LOOSENING, 0 of 11 level-backed today against 2 of 11 with both changes, which is the opposite of the intent — and worse, three separate measurements of the SAME baseline returned 0, 1 and 4. A number that unstable cannot govern money, and a fourth measurement would not fix it. What shipped instead changes NO behaviour at all: the desk now RECORDS, for every position it opens, what the stop was actually based on. The pinned half is a JSON `stop_level_basis` on the `trades` row — whether a computed structural level stood behind the stop, and if so its price and side, how many separate times price turned there, how many bars either side confirm a swing point and the whole confirmation span, the zone's edges and width as the live definition drew them, and the signed distances from the stop and from the entry to the level. It is written for stops with NOTHING behind them too, with `level_backed: false`, because that is the control group without which "levels hold" cannot be falsified. The running half is two raw distances widened from each session's position snapshot: how far price travelled beyond the FAR edge of the zone, and the closest it ever came to the NEAR edge. NO VERDICT IS STORED — "respected", "pierced and recovered" and "broken outright" each need a cutoff nobody can source today, so only raw distances in price units are kept and a later reader states and defends its own cutoff against numbers that were never rounded to it. Anything genuinely unknown at write time is NULL, never substituted. The record EXTENDS the per-closed-trade stop-basis and excursion store built 2026-09-30 rather than standing up a second parallel one — same table, same rows, same joins to the realised outcome, which is the honest fit because the question is about the same trades. HARD LIMIT, written into the code beside the recording and pinned by a test: this may show that the CURRENT definition of a level is WRONG, and it may NEVER be swept for a better bar count or zone width. Fitting a number to this desk's own trading history is barred outright. The item stays OPEN; nothing has been read yet, because nothing has been recorded yet.


### 2026-10-01 — the adversary pass on PR 880, and what it changed

THE REFUTED CLAIM. PR 880 argued that item 55 (clustering on bar overlap) and
item 215 (a stop must rest on a forming bar) cancel out, one widening the zone
and the other narrowing what counts as resting on it. They do not. Item 215
only punches holes in the zone's INTERIOR; it cannot narrow the outward reach
by one cent, because the zone's edges ARE bar extremes and an extreme always
lies inside some bar. The furthest a stop could sit from the level price and
still be called backed was therefore the zone halfwidth exactly, with NO bound
anywhere, against a hard 1.00% of price on main. Measured on the desk's own
400-bar, 101-symbol set, 704 levels under the new clustering: median halfwidth
3.33% of price, p90 9.41%, max 36.07%; restricted to the 154 levels with at
least 5 touches, median 4.31% and 38% of them above 5%. Since the break check
evaluates the matched LEVEL price and not the stop, the desk could report
"structure intact" with the stop a fifth of the price away.

THE BOUND THAT WAS RESTORED, AND IT IS NOT A NUMBER. The level must be more
precise than the thing it is backing: its measured zone (min low to max high
over the bars that drew it) must be STRICTLY NARROWER than the trade's own
stop distance, `abs(entry - stop)`, which is already decided before this
question is asked. Because the stop-to-level gap can never exceed that span,
this makes `abs(stop - level) < abs(entry - stop)` a guarantee: the level a
stop claims to rest on is never further from the stop than the stop is from
the entry. Nothing is chosen, so there is nothing to sweep and nothing to
ratify. It is enforced in `src/data/levels.py::stop_rests_on_level` and
mirrored in `src/risk/exit_guard.py`, and `tests/test_level_match_zone.py`
now pins it.

WHAT IT ADMITS AND REFUSES [measured 2026-10-01, same 704 levels, using the
desk's two EXISTING stop floors as the stop distance so the measurement
introduces no number either]: at a 1.0-ATR stop it admits 33/704 levels (5%)
and 1/154 of the 5-touch-plus levels; at a 2.5-ATR stop it admits 465/704
(66%) and 63/154 (41%). The levels it refuses at 2.5 ATR have median halfwidth
5.64% of price and reach 36.07%; the widest it admits has halfwidth 13.41%,
still inside the trade's own risk by construction. The tight-stop exemption
therefore becomes RARE, and that is the honest consequence of refusing to pick
a width rather than a flaw in the bound: a level too vague to be more precise
than the stop has not earned that stop the right to be tighter than the noise
floor.

THE DIRECTION OF FAILING CLOSED, STATED PLAINLY BOTH WAYS. The adversary's
one-way-tightening worry does NOT apply here, and saying otherwise would be
wrong: not-backed routes a stop to the 2.5-ATR floor while backed floors it at
1.0 ATR, so failing closed WIDENS the stop rather than tightening it, and
every fail-closed branch in this diff moves protection outward. The flip side
is equally plain: the shipped effect is that two live stops become eligible to
sit at 1.0 ATR where they sit at 2.5 ATR today. With n=11 positions and 2
affected, this book cannot see harm either way — that is a sample too small to
measure, not evidence of safety. The live stop prices quoted in item 215 were
never re-verified against the broker and should not be treated as current.

THE CLUSTERING CLAIM WAS CORRECTED, NOT DEFENDED. `_cluster`'s docstring
claimed complete linkage. The ACCEPTANCE TEST is all-members (a pivot joins
only if its bar overlaps every member's, which is what buys the anti-chaining
property), but the PARTITION is greedy first-fit over price-sorted pivots: a
pivot overlapping two levels joins the lower-priced one and the result depends
on sweep order. True complete linkage merges the globally closest pair at each
step and is order-independent. The docstring now says exactly that. An untrue
description of an algorithm is the same class of defect as an untrue alert.

THE TOUCH COUNT COULD NOT BE RE-DERIVED, AND BOTH FAILURES ARE RECORDED.
`min_level_touches_for_stop_honor` = 5 was `sourced` on a real-versus-shuffled
bounce table over 101 symbols — built on the 1% clustering this item deletes,
so docs/OUTCOME.md requires it re-checked. Two attempts, both failed.
(1) DATA. The panel this repo holds is 276 bars per symbol, not the original's
five years; half is spent discovering levels, leaving n=27 real observations
at 5 touches with a 95% interval of [0.407, 0.778] — roughly four times the
original's width, so no separation at ANY touch count could be detected even
if it were there. (2) METHOD. The original's bounce procedure is reported in
docs/RESEARCH_FINDINGS.md section 7 as a table, not as reproducible steps, so
the reconstruction is not the same test — and it fails its own sanity check:
the SHUFFLED control scored HIGHER than real at every touch count (shuffled
0.688/0.725/0.717 at 2/3/4 touches against real 0.641/0.646/0.679), which
means the reconstruction is measuring something other than structure. The
value stays at 5, because moving it would be inventing a number; its ledger
status is downgraded from `sourced` to `arbitrary`, with both ratchets
appended.

THE RECORDING THAT WOULD SETTLE IT: the original 101-symbol panel at five
years of daily bars, levels rebuilt under the overlap clustering, the section
7 bounce procedure restated in code in the repo rather than described, real
against a returns-shuffled control, 95% intervals by touch count; the
threshold is the lowest touch count whose interval clears the control's. Until
that exists the 5 is an unsourced bar deciding how tight a live stop may be.



### 2026-10-04 — THE DATA BLOCKER WAS FALSE, THE SWEEP RAN, AND NEITHER NUMBER IS DERIVABLE

**THE NOTE ABOVE WAS WRONG ABOUT THE DATA, AND THAT IS THE FIRST FINDING.** The
2026-09-30 entry states the Tsinaslanidis 4.5 sweep "cannot be run by an agent
barred from production credentials" because "there is no local bar cache in the
repo". There is. `ops/model_policy/fixtures/yf_daily_bars_pm_public_day_2026-09-14.json.gz`
is committed and holds **101 symbols x 1236 completed daily bars, 2021-10-11 to
2026-09-12** — the five-year public panel the note says does not exist. It was
missed because the search looked in `tests/fixtures/`. No credentials, no
network and no production read are needed. Separately confirmed in the same
pass: the production database holds **no OHLCV table at all** and the recorded
evening replays carry no bars, so the committed panel is the only bar source —
but it is sufficient, and it is 4.5x the 276-bar panel whose width the
2026-10-01 note blamed for the failed touch-count re-derivation.

**THE EXPERIMENT.** `ops/research/item55_level_sweep.py`, hermetic and
rerunnable. Pivots are confirmed swing highs/lows over a symmetric window;
pivots are grouped into levels; a level needs the settled two touches. Levels
are discovered on the **first 60% of each symbol's series and every event is
counted on the remaining 40%**, so nothing is fitted and there is no lookahead.
The bounce test is Tsinaslanidis 4.5 restated in code rather than described:
price enters the zone from clearly outside, and the first subsequent close back
outside it either lands on the side it came from (HOLD) or through it (BREAK).
**No cutoff is chosen anywhere** — "respected" is not defined, only "came back
out the same way". The control is the identical procedure on the same symbol's
own daily returns shuffled, so the control has the same volatility and the same
drift and no structure. Swept: pivot window 3/5/10/25, zone 0.5/1/2/3/5%, plus
the threshold-free bar-overlap clustering, 24 settings.

**THE RESULT [measured 2026-10-04, 99 symbols that cleaned, ~230,000 out-of-sample
bounce events across the 24 settings].** Not one setting's real hold-rate beats
its own shuffled control at 95%. Every single edge is zero or NEGATIVE. At the
desk's live setting (window 5, zone 1%) real holds 0.6461 against control
0.6579, edge -0.0118 +/- 0.0104 on 15,066 events. The widest tested zone (5%)
holds 0.83 and the narrowest (0.5%) holds 0.58 — but the control does exactly
the same, because a wider zone is simply harder to close outside of. **The
entire apparent "levels hold" effect is explained by zone width and by the
return distribution, with nothing left over for structure.**

**A DIFFERENT ANSWER WAS AVAILABLE AND DID NOT APPEAR.** Real structure has a
signature: a positive edge concentrated at the narrow zones and the small
windows, decaying as the zone widens past the precision of the turn. Narrow
zones were swept down to 0.5%, a fifth of anything in the published range, and
windows out to 25. The surface is flat and slightly below zero everywhere. Had
the panel carried the effect, this sweep would have shown it; it is not a test
that could only have returned one answer.

**WHAT THIS DOES NOT LICENCE, STATED PLAINLY.** This is NOT a finding that
levels are harmful, and the three settings whose negative edge clears 95% must
not be read that way. Shuffling returns destroys volatility clustering, so the
control series trend less and therefore close outside a band less often; that
biases the control UPWARD by an unmeasured amount and is the most likely source
of a uniform small negative. The honest statement is **no measurable structure
effect at any setting, with a control known to be imperfect in the direction
observed**. Note this also explains the 2026-10-01 touch-count attempt's failed
sanity check — shuffled scoring higher there was not a broken reconstruction, it
is what this control does, reproduced here at 24/24 settings on 4.5x the data.

**THE ANSWER TO THE ITEM: outcome 3, NEITHER number can be derived from the data
available, and NOTHING WAS CHANGED.** `PIVOT_WINDOW` stays 5 and 3,
`CLUSTER_TOLERANCE_PCT` stays 1.0, every ledger status stays `arbitrary`. There
is no measured basis to move any of them, and inventing one here would be the
exact defect this item exists to remove. The bar-overlap clustering remains the
only threshold-free candidate for the zone and remains unshipped — it scores no
better than the percentages and its measured edge is the most negative of the
set (-0.023 to -0.029), so there is now a measurement arguing against adopting
it, where before there was only an argument for it.

**EXACTLY WHAT WOULD SETTLE IT.** A control that preserves volatility clustering
— a stationary block bootstrap, or a GARCH-filtered resample — run over this same
committed panel with this same script. That is the one missing piece; the data
and the procedure now both exist in the repo. Until a control that cannot be
accused of this bias is built, no bar count and no zone width is derivable, and
the two open boxes below remain correctly unticked: the desk's own recording,
not this panel, is still the route to a verdict on whether its levels hold.

---

## FOLLOW-UP 2026-10-04 — the missing control was built. THE CONCLUSION SURVIVES.

The weakness named above has been closed. `ops/research/item55_volclustered_control.py`
re-runs the identical sweep — same committed panel, same pivot definition, same
greedy clustering, same `MIN_TOUCHES=2`, same 60/40 out-of-sample split, same
bounce counter, same interval — and changes **only** the control. Every other
function is imported unchanged from `item55_level_sweep.py`, so nothing but the
counterfactual differs.

**The fair control: a sign-randomised surrogate.** Each log return is written as
drift + deviation; the deviation keeps its MAGNITUDE at its own index and only
its SIGN is flipped. The series of absolute deviations is therefore identical
bar for bar to the real one, so volatility clustering is preserved exactly — not
approximately, and with no block length, half-life, window or cut-off invented
anywhere. The intrabar high/low shape stays at its own index too. Only the sign
sequence, the thing that builds a path and puts turns at particular prices, is
destroyed.

**Measured proof the control is now fair.** Median lag-1 autocorrelation of
|log return| across the 99 usable symbols: real **+0.1159**, sign-flip control
**+0.1172**. The plain shuffle it replaces drives that quantity to ~0 (asserted
in the test). The clustering objection is answered on its own terms.

**Result over 20 independent control replications** (per-setting spread reported,
not a single draw; control standard deviation 0.0044–0.0138 across settings, so
no conclusion here rests on one lucky surrogate):

- All 24 definitions score **negative** against the fair control.
- Edges range **-0.0153 to -0.0748**; 23 of 24 clear the 95% band on the wrong
  side. The desk's live setting (window 5, 1.0% zone) measures real 0.6461 vs
  control 0.7043, edge **-0.0582 +/- 0.0098**.
- The negatives got **LARGER**, not smaller, than under the plain shuffle. The
  hypothesis recorded above — that the uniform small negative was an artefact of
  the unfair control — is **disproved**. The bias ran the other way.

**VERDICT: outcome 1, the conclusion survives.** Levels as this desk defines them
show no measurable edge against a control that cannot be accused of the
volatility-clustering bias. Price entering one of these zones holds its side
*less* often than a structureless series with the same volatility path does.
This is a finding about a core part of the strategy and it is stated plainly:
there is no measurement supporting these level definitions, and there is now a
measurement against them.

**A different answer was possible, and the method is proven able to produce it.**
Had levels been real, the measurement would have shown the real series holding
its side materially more often than the surrogate, with the edge clearing its
95% band on the positive side. `test_a_different_answer_is_possible_when_levels_are_real`
runs the exact same pipeline and the exact same fair control over a synthetic
panel in which price genuinely reflects off two fixed prices, and it reports
precisely that: a large, significant POSITIVE edge. The same assertion applied
to the real panel fails at all 24 settings. The method is not one that can only
return zero.

**What this does NOT license.** Nothing is changed by this run. `PIVOT_WINDOW`
stays 5 and 3, `CLUSTER_TOLERANCE_PCT` stays 1.0, no stop, size or exit moves,
and every ledger status stays `arbitrary` — a measurement that a number has no
support is not a derivation of a better one. The remaining honest caveat is
scope, not method: this is 99 symbols of public daily bars over roughly five
years, and daily bars cannot see intraday touches, so a level effect living
inside the day would not appear here. That is a different measurement needing
intraday data the repo does not have, not a defect in this one.


### 2026-10-04 — THE DATA BLOCKER WAS FALSE, THE SWEEP RAN, AND NEITHER NUMBER IS DERIVABLE

**THE NOTE ABOVE WAS WRONG ABOUT THE DATA, AND THAT IS THE FIRST FINDING.** The
2026-09-30 entry states the Tsinaslanidis 4.5 sweep "cannot be run by an agent
barred from production credentials" because "there is no local bar cache in the
repo". There is. `ops/model_policy/fixtures/yf_daily_bars_pm_public_day_2026-09-14.json.gz`
is committed and holds **101 symbols x 1236 completed daily bars, 2021-10-11 to
2026-09-12** — the five-year public panel the note says does not exist. It was
missed because the search looked in `tests/fixtures/`. No credentials, no
network and no production read are needed. Separately confirmed in the same
pass: the production database holds **no OHLCV table at all** and the recorded
evening replays carry no bars, so the committed panel is the only bar source —
but it is sufficient, and it is 4.5x the 276-bar panel whose width the
2026-10-01 note blamed for the failed touch-count re-derivation.

**THE EXPERIMENT.** `ops/research/item55_level_sweep.py`, hermetic and
rerunnable. Pivots are confirmed swing highs/lows over a symmetric window;
pivots are grouped into levels; a level needs the settled two touches. Levels
are discovered on the **first 60% of each symbol's series and every event is
counted on the remaining 40%**, so nothing is fitted and there is no lookahead.
The bounce test is Tsinaslanidis 4.5 restated in code rather than described:
price enters the zone from clearly outside, and the first subsequent close back
outside it either lands on the side it came from (HOLD) or through it (BREAK).
**No cutoff is chosen anywhere** — "respected" is not defined, only "came back
out the same way". The control is the identical procedure on the same symbol's
own daily returns shuffled, so the control has the same volatility and the same
drift and no structure. Swept: pivot window 3/5/10/25, zone 0.5/1/2/3/5%, plus
the threshold-free bar-overlap clustering, 24 settings.

**THE RESULT [measured 2026-10-04, 99 symbols that cleaned, ~230,000 out-of-sample
bounce events across the 24 settings].** Not one setting's real hold-rate beats
its own shuffled control at 95%. Every single edge is zero or NEGATIVE. At the
desk's live setting (window 5, zone 1%) real holds 0.6461 against control
0.6579, edge -0.0118 +/- 0.0104 on 15,066 events. The widest tested zone (5%)
holds 0.83 and the narrowest (0.5%) holds 0.58 — but the control does exactly
the same, because a wider zone is simply harder to close outside of. **The
entire apparent "levels hold" effect is explained by zone width and by the
return distribution, with nothing left over for structure.**

**A DIFFERENT ANSWER WAS AVAILABLE AND DID NOT APPEAR.** Real structure has a
signature: a positive edge concentrated at the narrow zones and the small
windows, decaying as the zone widens past the precision of the turn. Narrow
zones were swept down to 0.5%, a fifth of anything in the published range, and
windows out to 25. The surface is flat and slightly below zero everywhere. Had
the panel carried the effect, this sweep would have shown it; it is not a test
that could only have returned one answer.

**WHAT THIS DOES NOT LICENCE, STATED PLAINLY.** This is NOT a finding that
levels are harmful, and the three settings whose negative edge clears 95% must
not be read that way. Shuffling returns destroys volatility clustering, so the
control series trend less and therefore close outside a band less often; that
biases the control UPWARD by an unmeasured amount and is the most likely source
of a uniform small negative. The honest statement is **no measurable structure
effect at any setting, with a control known to be imperfect in the direction
observed**. Note this also explains the 2026-10-01 touch-count attempt's failed
sanity check — shuffled scoring higher there was not a broken reconstruction, it
is what this control does, reproduced here at 24/24 settings on 4.5x the data.

**THE ANSWER TO THE ITEM: outcome 3, NEITHER number can be derived from the data
available, and NOTHING WAS CHANGED.** `PIVOT_WINDOW` stays 5 and 3,
`CLUSTER_TOLERANCE_PCT` stays 1.0, every ledger status stays `arbitrary`. There
is no measured basis to move any of them, and inventing one here would be the
exact defect this item exists to remove. The bar-overlap clustering remains the
only threshold-free candidate for the zone and remains unshipped — it scores no
better than the percentages and its measured edge is the most negative of the
set (-0.023 to -0.029), so there is now a measurement arguing against adopting
it, where before there was only an argument for it.

**EXACTLY WHAT WOULD SETTLE IT.** A control that preserves volatility clustering
— a stationary block bootstrap, or a GARCH-filtered resample — run over this same
committed panel with this same script. That is the one missing piece; the data
and the procedure now both exist in the repo. Until a control that cannot be
accused of this bias is built, no bar count and no zone width is derivable, and
the two open boxes below remain correctly unticked: the desk's own recording,
not this panel, is still the route to a verdict on whether its levels hold.
