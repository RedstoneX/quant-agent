# QAMC Incident History

**What this file is:** a permanent, plain-language record of things that broke
and what was done about them. Newest first.

**Why it exists separately from `docs/WORK.md`.** WORK.md is the active
backlog and it has a hard 100,000-byte cap, so finished incidents get trimmed
out of it as new work arrives — which means the history of what went wrong
was being deleted by design. Git kept it, but git is not readable by the
owner. Rex asked for a record that does not disappear. This is it.

**Rules for this file.** Append, never trim. Never delete an entry to save
space; if it gets long, split by year. Every entry leads with one
non-technical line stating what actually broke in ordinary words, because the
person who most needs to read this is not a developer. Detail goes underneath
for whoever has to fix it again.

**Never record here what the repo already records** — no restating code, no
commit IDs as the substance of an entry. Record the *reasoning*: what the
symptom was, what the real cause turned out to be, what was ruled out, and
what would catch it next time.

---

### 2026-09-03 — alerts stop relying on colour

**In plain words:** the owner is red/green colour blind — red, orange and
green circles are effectively indistinguishable to him. Every critical
alert on this desk opened with a coloured circle (🔴 critical, 🟠 hold) and
colour was doing all the work of telling him how bad something was. This is
item 21(b) in `docs/WORK.md`, owner spec 2026-09-02.

**What changed.** Every 🔴/🟠 alert opening in `src/notifier.py`,
`src/pipeline_stages.py`, `src/trader_feed.py`, `scripts/alert_heartbeat.py`
and `scripts/run_if_et_window.sh` now uses a shape instead of a coloured
disc — 🛑 for stop/critical, ⚠️ for a degraded/hold state that needs no
immediate action — and every one of those messages now leads with a plain
English severity word (`FAILED`, `SUSPENDED`, `CRASHED`, `KILLED`,
`INCOMPLETE`) so the message is still correct read with zero emoji
rendering. One condition — a held position with ZERO stop-loss coverage,
unbounded loss, the single most catastrophic state on this desk — is marked
🛑🛑🛑 (repetition, not colour, marks the top severity tier); a partially
covered stop is downgraded from the old 🔴 to ⚠️ since a stop IS still
standing watch over most of the position.

**What was deliberately left alone.** `src/cost_circuit.py` and
`src/pipeline.py` still open alerts with 🔴/🟠 — both were being edited by
other sessions in parallel and touching them risked a merge collision;
someone still needs to apply the same shape/text-severity treatment there.
`src/alert_watchdog.py`'s self-test failure/recovery messages and the
generic per-mode status legends in `src/notifier.py`'s and
`src/trader_feed.py`'s own `_status_emoji` helpers (used for every routine
run summary, success included, not just alerts) were read but not touched:
they already carry a plain-text `status:`/`Status:` line immediately below
the coloured circle, so the colour is not the *only* signal there, but
they are visually inconsistent with the rest of the desk now and a good
candidate for the same shape treatment later.

**Tests.** Updated `tests/test_notifier.py`, `tests/test_daily_report.py`,
`tests/test_ops_audit_round2.py` and `tests/test_intraday_scan_crash_visibility.py`
to assert the new shapes and leading severity words instead of the old
colour + informal word order. Full suite run alongside; no regressions
beyond the two pre-existing `tests/test_rehearsal_reproduces_cost_ceiling.py`
failures (that suite reads live production state and is expected to fail
inside a worktree).

---

### 2026-09-02/03 — funnel item 8 ("stop on the wrong side of entry") checked, not a code defect

**In plain words:** the census found 2 of 68 proposals refused because the
stop price was on the wrong side of the entry price — a stop that could
never actually protect the trade. The refusal itself is correct and stays;
the question was what produced a broken stop in the first place.

**Why:** nothing produces a broken stop. Traced backward from the refusal,
through `_resolve_stop`, to the technical analyst's own output validator
(`src/models.py`), which already guarantees a proposal's stop sits on the
correct side of its OWN entry price at the moment it's created — the
analyst never emits a self-contradictory pair. No sign-flip, unit-conversion,
or rounding bug anywhere in that chain.

**What actually happens:** `PortfolioConstructor` prices the trade off a
live quote fetched fresh at construction time — after macro, news, earnings
and the portfolio manager have all already run, so real time has passed.
Measured against a production database snapshot, that live price can land
seconds to minutes and several percent away from the price the analyst's
entry/stop pair was computed against. One real case (DIS) moved -1.8% in
108 seconds against a stop only 2.2% from entry, landing $0.48 from
flipping outright. Ordinary price movement in that window can carry the
market through a perfectly sound stop level before the trade is ever
constructed. That is not a data-quality bug to fix upstream — it is the
refusal correctly catching a stop that is no longer valid by the time the
trade would be placed.

**New test** `tests/test_shorts_stage3.py::test_long_stop_breached_by_live_price_since_analysis_is_rejected`
covers the live-quote path (the existing test only covered the synthetic
`suggested_stop_price` path). No source change — this is a documented
non-fix.

---

### 2026-09-02/03 — funnel item 5 ("allocation rounds to zero shares") checked, not a live defect

**In plain words:** the census found 3 of 68 proposals died because a
$9.9k-scale account tried to buy a $200+ stock and the share count rounded
down to zero. Fractional sizing was already on, so the open question was
whether it was actually being used for these three, or getting bypassed.
Checked in code — it is not a current bug.

**Why:** fractional sizing merged 2026-09-01 23:24 UTC. The census window
runs 2026-08-18 through 2026-09-02 18:01 — 14 of its ~15 days predate
fractional sizing existing at all, when every BUY floored to a whole share
regardless of account scale. That matches what the 2026-08-29 census already
concluded about this same cause, before fractional was even built (see the
ranked-causes table below).

**Verified directly in code**, not just by timeline: in
`src/pipeline_stages.py`, `_fractional_sizing_allowed` is resolved BEFORE
`_size_shares` floors anything, so a BUY on a broker-confirmed-fractionable
symbol cannot round to zero at this account scale — the 4-decimal floor only
reaches zero below a 0.0001-share raw quantity, far under any allocation this
desk sizes. New test
`tests/test_fractional_sizing.py::test_a_9_9k_account_sizes_a_200_dollar_stock_without_rounding_to_zero`
pins the census's own $9.9k/$200+ numbers as a regression guard;
`test_a_sub_one_share_position_is_taken_not_skipped` already covered the
general case.

**Ruled out, not assumed:** a stale/disabled config flag (checked
`config/settings.yaml` and `src/config.py` — `fractional_enabled: true` in
both); a floor applied after fractional rounding that could re-zero it
(`below_min_notional` is a distinct, separately-recorded skip reason, never
folded into `qty_zero`); wrong evaluation order (fractional eligibility is
decided first, size second, correctly).

**What can still legitimately produce `qty_zero` today**, and is not this
defect: a SHORT (a borrowed share cannot be fractional —
`_fractional_sizing_allowed` returns `False` before even asking the broker,
per `test_short_entries_are_always_whole_share`), or a symbol the broker does
not confirm `fractionable` for (fails closed to whole shares, per
`test_non_fractionable_symbol_falls_back_to_whole_shares`).

**Not confirmed:** which of the 3 historical hits were shorts/non-fractionable
symbols versus ordinary pre-fractional-era longs — the backing
`data/resets/20260902T181859Z/quant_agent.db` is not in this checkout, only
on the live desk. Re-measure the census against a fractional-sizing-only
window before spending more time on this line.

---

### 2026-09-02 — the full proposal-to-fill census: where every trade idea actually dies, counted, not guessed

**In plain words:** the desk had a stale "23% of proposals become a fill"
number and no complete breakdown of why. This is the complete count, over
the whole recorded history, of every proposal and exactly what killed the
ones that did not fill. The 1.5 reward:risk floor already had a name and a
paper trail (see the entries below); this is the first time it was counted
alongside every OTHER cause on the same footing, so it can be ranked instead
of assumed.

**Data used.** `/home/qamc/quant-agent/data/quant_agent.db` was reset live,
mid-measurement (2026-09-02 18:18:59Z — `scripts/desk_reset.py`, flattening
8 positions and wiping `trades`/`intraday_evaluations`/the decision-linked
half of `specialist_evidence`, citing the reward:risk defect below as why
that history was "contaminated"). The reset script itself copied the
pre-wipe database to
`data/resets/20260902T181859Z/quant_agent.db` before deleting anything —
that copy is what this census is built from. It carries the full recorded
history: `specialist_evidence` 2026-08-17 through 2026-09-02 18:01, `trades`
2026-08-14 through 2026-09-02 17:02, 104 distinct runs. It supersedes the two
older `data/quant_agent.db.bak-*` files (both stop at 2026-08-28) and is the
only complete copy left once the reset runs.

**Denominator: 68.** That is every `target` (PM proposal) with a positive
size — an entry request, not an exit — recorded from 2026-08-18 through
2026-09-02. 15 of the 68 filled (22%, matching the stale figure — it was
stale in detail, not in headline). 53 did not.

**Ranked causes (of 53 blocked):**

| n | % of 68 | cause | classification |
|---|---|---|---|
| 17 | 25% | reward:risk floor (1.5) — 10 killed before an order was ever built (`PortfolioConstructor._widen_stop_past_noise`), 7 killed by the AI Risk Manager citing the same floor by name in its veto text | RULE TOO STRICT |
| 13 | 19% | unrecoverable — no table and no surviving log explains the drop | **gap in the record itself, not a rule** |
| 6 | 9% | order reached the broker, was accepted, never filled, got cancelled | WORKING AS INTENDED (price-protection cancels an order sitting unfilled beyond its window) but a real, material cost |
| 4 | 6% | execution-time reward:risk re-check (1.2 floor — a DIFFERENT, narrower rule than the 1.5 one above, fires only when execution moved the stop or limit after Risk Manager approval) | WORKING AS INTENDED |
| 3 | 4% | allocation rounded to zero shares | account-scale artifact (paper account ≈$9.9k against $200+ stocks), not really a rule at all |
| 3 | 4% | no structural level existed to derive a target from (`[no_level_in_direction]`) — a brand-new check, all 3 on 2026-09-02, one day old | too new to classify with confidence; watching |
| 2 | 3% | AI Risk Manager vetoed the whole plan for reading as internally inconsistent (not a reward:risk call) | RULE TOO STRICT — the exact failure mode `docs/STATE.md`'s "Removed Before You Saw This" fix (2026-08-31) targeted, still reproducing after that fix shipped |
| 2 | 3% | analyst supplied a stop on the wrong side of entry; constructor correctly refused rather than inventing one | the REFUSAL is WORKING AS INTENDED; the upstream stop being wrong-sided at all is a DEFECT worth its own look |
| 1 | 1% | insufficient cash | WORKING AS INTENDED, and not a material cause — 1 of 68 |
| 1 | 1% | quote 14.6% away from reference, refused rather than crossed | WORKING AS INTENDED against what looks like a bad IEX print (known paper-data limitation, not new) |
| 1 | 1% | broker explicitly rejected the submitted order | one occurrence, not investigated further |

**Direct answers to the six questions asked of this data:**

1. *Died at the 1.5 reward:risk floor specifically?* **17 of 68 (25%)** —
   confirmed as the single largest NAMED cause, ahead of every other rule.
   It is not, however, the majority of blocked proposals (53) the way "the
   single largest cause" might imply — the unrecoverable-gap bucket (13) is
   close behind it, and together the two dwarf everything else.
2. *Died for lack of a structural level to anchor a stop?* **3 of 68**,
   all on one day (2026-09-02) under a check that shipped 2026-09-01. Real,
   but currently small next to the reward:risk floor — one day of data is
   not enough to say whether it stays small.
3. *Died at the cash/exposure clamp rather than a risk rule?* **1 of 68.**
   Not a material cause over this window, whatever it may become at larger
   size.
4. *Died to something that looks like a bug rather than a rule?* Two
   patterns qualify: the 2 wrong-sided stops (#8 above — the constructor's
   refusal is correct, but a stop landing on the wrong side of entry at all
   means something upstream produced a self-contradictory number), and the
   9-of-53 `order_not_placed` shape specifically (constructor built the
   order, nothing in any table or surviving log says what happened next —
   no trade, no skip, no rejection). The second is the stronger DEFECT
   candidate: that shape is exactly what an interrupted or crashed run
   looks like from the outside, not what a deliberate no-trade looks like.
   Separately, `agent_logs`/`specialist_evidence` record 14 outright agent
   failures in this window — 10 of them `portfolio_manager:
   no_valid_grounded_decision` on 2026-08-25 alone, which is why that date
   produced zero proposals at all rather than merely zero fills.
5. *Sessions with zero fills, and the dominant cause on each?* **6 of the 11
   sessions that produced at least one proposal** (55%): 2026-08-18 and
   2026-08-19 (dominant: unexplained gap), 2026-08-20 and 2026-09-01
   (dominant: reward:risk-floor veto by the Risk Manager), 2026-08-24
   (dominant: orders cancelled after submission), 2026-08-28 (dominant:
   reward:risk floor at the constructor).
6. *Same symbol repeating across refusals?* NVDA was proposed **9** times
   and filled **once** — by far the most repeated name, and its 8 misses
   are spread across nearly every cause on the list above, not one. Three
   symbols were proposed 3+ times and filled **zero**: JPM, VLO, PATH.

**What could NOT be attributed, and how much of the total that is.** 13 of
68 proposals (19% of all proposals, 25% of blocked ones) resolve to
`no_order_built` or `order_not_placed` with no supporting record anywhere —
not in `specialist_evidence`, not in `trades`, and not in the systemd
`journalctl --user` history (which otherwise reaches back to 2026-08-09 and
resolved another 15 of the 68 by cross-referencing the constructor's own log
lines against proposal timestamps — that cross-reference is what produced
the #1 reward:risk-floor figure above; the constructor logs no reason to
any table, only to `logger.info`/`logger.warning`). One specific run
(`run-5834d319-dec-51d3bf`, 2026-08-31 ~19:06) has ZERO constructor log
lines in the journal despite producing a real verdict and real trades
minutes later — either that invocation didn't run under the systemd unit
this journal captures (a manual/replay run's stdout goes nowhere this
census can reach), or its logging was lost some other way. This is reported
as a genuine gap, not resolved further.

**What this does NOT change.** The reward:risk floor's value (1.5) and the
level-backed-stop exemption (spec §12.1) are unchanged by this entry — see
the 2026-08-31 and earlier entries below for that history. This entry adds
the ranking and the complete count; it does not re-litigate the threshold.

**Reusable going forward:** `scripts/blocked_proposals_census.py` — regenerates
every number above (and the per-day / per-symbol breakdowns) against any
`quant_agent.db` snapshot. Read-only, no pipeline imports, no broker calls.

---
### 2026-09-02 — the phantom bill was only half fixed, and a model going out of print could have switched the desk off for good

**In plain words:** two ways the desk could stop itself over money it never
actually spent. The first was the 2026-08-31 phantom charge, still alive on
the half nobody looked at. The second was worse: if the model marketplace ever
stopped listing one of our models, the desk would have shut down and stayed
shut down, with no way back except a person editing a file by hand.

**Neither was costing much. Both could cost a trading day.** Measured over the
clean period 2026-08-27 to 2026-09-02 (2026-08-31's own numbers only after the
operator's ledger correction that afternoon), real spend runs $0.73–$1.14 a
day against a $2.75 ceiling, and the Portfolio Manager is 93% of it — $4.25 of
$4.56 on 35 of 153 calls. Nothing found here moves that. What they move is
uptime, and an unattended desk that stops does not restart itself.

**The phantom charge survived on the branch where the retry works.**
`fail_call` learned on 2026-08-31 that a 429/400/401/403/404 or a pre-send
transport failure provably billed nothing. `complete_call` never learned it.
So when the first attempt was refused and the *second one succeeded*, the
refusal's conservative reserve was still added on top of the real cost of the
response we got and paid for. It happened twice in the recorded ledger:
tech_analyst on 2026-08-28 at 14:31 booked $0.0135 against a real $0.0014
(9.6x), news_analyst on 2026-08-31 at 14:36 booked $0.0126 against $0.0023
(5.4x). Two cents in total — and the same mechanism that put $1.90 of
imaginary spend on the ledger and darkened the desk twice in one day. The
direction matters and it is worth saying plainly: this error runs AGAINST the
desk. It never hides spending; it invents it, burns the day's budget with it,
and stops trading early on money that was never charged.

**Fixed the same way, deliberately: the same allow-list, the same contagious
ambiguity.** One attempt that might have been billed and the whole reservation
is charged exactly as before. A caller that cannot say what its attempts
failed with keeps the old behaviour, so this can only ever forgive more
genuinely-free attempts, never fewer.

**The pricing latch had a third door, and it was the one that could not be
walked back through.** The 2026-08-28 grace window fixed a stale price list.
It did not fix a price list we could read that simply did not carry one of our
models — and OpenRouter retiring a model id is an ordinary event, not a fault
(`google/gemini-2.5-flash-lite` is already refused to new Google keys). That
path returned "no pricing", which suspends paid analysis behind the durable
operator-reset latch. It could not recover on its own, because a successful
fetch **writes the cache before the completeness check runs**: every later
session read the same fresh-but-incomplete file, never re-fetched, never
reached the grace window, and failed identically. An unattended desk could
have been switched off indefinitely by a deprecation notice it had no part in.

**What was wrong was the premise, not the number.** That check can only ever
name a model that is already a row in the pinned baseline table in
`src/cost_table.py` — a verified, dated, drift-checked rate. "There is no rate
at all", which is what failing closed asserts, was never true for anything it
could name. It now prices that one model from its pinned rate, keeps the live
rate for every model the catalog did price, and shouts. The ceiling is
untouched: the call is still priced, still reserved, still counted, and a
model genuinely withdrawn answers 404 — which the zero-cost allow-list already
accounts at $0, so the seat fails safely per-call while the desk keeps
trading.

**Deliberately NOT changed, and someone should decide about it.** The pinned
`openai/gpt-5.5` rate is $5/$30 per million and the PM seat routes to the
`openai/flex` endpoint — provider-reported cost has been a **median 0.38x** of
the pinned estimate over 32 calls since 2026-08-28. Reservations, and
therefore the reserved-exposure ceilings, are sized from the pinned rate, so
every PM reservation is roughly **2.7x** what the seat actually gets billed.
That is what produced the 2026-08-28 hold ("would project session cost to
$1.9118, above ceiling $1.80") on a call that really cost about $0.25 — and
the response was to raise the ceiling from 1.80 to 2.60. The mis-measurement
is quietly loosening the real protection. It is NOT safe to just price
reservations at the flex rate: fallbacks are enabled, so a saturated flex tier
lands on the $5/$30 endpoint and the reservation would then under-cover
exactly the call that costs most. This needs an owner decision, not a patch.

**Also found, also not changed.** A session killed mid-call at the end of a
trading day leaves an attempted reservation that nothing sweeps until the next
session runs — which is the next morning, when the sweep charges it and raises
the hard latch. The day it darkens is not the day it broke. Never observed;
reported because the shape is the same as every incident above.

### 2026-09-02 — one word the model spelled differently could bin a whole stock analysis, and 118 other fields could do it too

**In plain words:** when an analyst wrote "nothing to say here" as an empty
blank, the system accepted it. When it wrote the same thing as the word
`null`, the system threw the *entire* analysis away — the rating, the entry
price, the stop, all of it — and only a log line said so. The specific field
this was found on had already been patched. The problem was that 118 more
fields could do exactly the same thing, and nobody was counting the losses.

**What was measured, not assumed.** Against the production response log for
2026-08-14..2026-09-01:

| field | explicit nulls | occurrences | status before |
|---|---|---|---|
| `TechAnalysisResult.thesis_invalid_if` | 42 | 2,021 | patched 2026-09-01 |
| `MissedOpportunity.theme_durability` | 25 | 50 | **still exposed** |
| `MissedOpportunity.universe_addition_reason` | 11 | 50 | **still exposed** |

The evening pair is the sharper case: half of all `theme_durability` slots
came back null, and each one silently deleted that whole missed-opportunity
entry from the quarterly theme aggregation. Replaying the stored responses,
12 entries that the desk had discarded now parse.

**What it actually cost, and what was RULED OUT.** The 42 nulls fall across
28 distinct symbols in 4 responses. Of those 28, **4 were lost permanently** —
EQNR on 2026-08-20, and AMT, EQIX and PLD on 2026-08-25. Those two batches
logged `90/91 symbols analyzed, 1 failed` and `84/87, 3 failed`, and the failed
symbols are exactly the null-carrying ones the retry did not rescue. The
remaining 24 were recovered by a bounded retry — a paid extra LLM call each
time, and invisible afterwards because a rescued batch reports `data_status`
"ok".

**What does NOT hold up** is the causal link to the zero-trade morning of
2026-09-01. Every one of the 42 null-carrying analyses — including all ten
that morning — was rated `neutral`, verified from the raw stored JSON
independent of the models, and that batch logged `58/58 symbols analyzed`
with nothing lost. A `neutral` read proposes no trade, so **no tradeable
candidate has been shown lost to this defect**, and the zero-trade day has a
different cause. What the defect demonstrably cost is 4 analyses, some paid
retry round-trips, and a failure mode that would have been invisible had it
landed on a `buy`. The fix is justified by that plus the exposure — not by a
lost trade, and it should not be described as one.

**The real cause.** Pydantic checks a field's declared type before any
whole-object rule runs, so an explicit null on a field that is not marked
optional is fatal even when that field declares a perfectly good default. An
omitted key and a null key mean the same thing to the model writing the JSON;
they meant opposite things to the parser. Fixing them one at a time loses by
attrition — there were 119 such fields.

**What was ruled IN, and what deliberately was not.** Every model parsed from
an LLM response now treats an explicit null on a *defaulted* field as an
absent key. That is a state the schema already declares legal and production
already exercises constantly. It does **not** apply to required fields (a null
`symbol`, `rating`, `stop_loss` or `sell_price` still rejects the object,
correctly — no default exists to fall back on), and two defaulted fields are
explicitly exempted because their defaults are affirmative instructions rather
than "nothing was said": a position's `direction` (defaults `long` — a null
must never quietly flip a short) and the Risk Manager's `scale_all_buys`
(defaults `1.0` — a null must never quietly release a brake it meant to pull).
The whole-object rules are untouched, so an actionable analysis still cannot
survive without an entry, a stop, a target, a setup type and a structural
level.

**Counting, because recovering it quietly is its own failure.**
`thesis_invalid_if` is the soft-exit signal. Blanking it keeps the analysis
but throws away the trigger that would exit before the hard stop fires, and
nothing anywhere said how often that happened. Two counts now reach the Risk
Manager's prompt and the session log as non-blocking advisories, on the same
seam `data_degraded` and `pm_audit_step_missing` already use:
`analysis_field_nulled` (kept the object, lost an input) and
`analysis_parse_loss` (lost the object entirely). The second one fires even
when a retry later recovers the symbol — the case that was previously
invisible, because `data_status["tech"]` reads "ok" and the only trace was an
INFO line.

**What would catch it next time.** A test that walks every model in
`src/models.py` and fails if one is parsed from LLM output, has a defaulted
field that rejects null, and has not opted into the rule. A new model cannot
reintroduce this by omission — which is the point, because the previous
approach depended on somebody remembering.

---

### 2026-09-01 — a network blip could silently switch off the sector concentration cap

**In plain words:** when the system couldn't figure out which industry a
stock belongs to, it treated that stock as if the sector limit didn't apply
to it at all. A position already held with the same problem also stopped
counting toward its sector's total. Neither of those showed up anywhere, so
an ordinary network hiccup could switch the concentration cap off for most
of the tradeable list, and nobody would have known.

**The real cause.** A stock's sector comes from a live network lookup; only
the ~21 sector ETFs have an offline backup table. The other 80 of 101
tradeable names have no fallback. When the lookup fails, times out, or a
stock genuinely has no listed sector, it comes back "Unknown" — and the risk
check's sector rule read "Unknown" as "skip this stock entirely," in both
directions: a new trade in an unresolved sector was never measured against
the limit, and a HELD position with an unresolved sector was invisible to
every sector's exposure total. This was a known, deliberately-deferred gap
from the same-day work that raised the sector target to 75% with a hard 90%
wall and split it by long/short side — the code that shipped that change
said so directly in a comment. With margin arriving the same night at up to
2x, an unresolved sector meant, in practice, no concentration limit at all
on a leveraged book.

**What was done.** An unresolved sector is no longer exempt. It is pooled
into its own "Unknown" bucket and checked against the exact same 75%
target / 90% hard wall every real sector gets, for new trades and for
positions already held. Hitting this condition now raises a visible flag:
a plain "degraded: sector" line appears in every session's summary (the
same line already used when the news or macro feed is having a bad day),
and it reaches the AI Risk Manager's review directly. A lookup that timed
out or failed reads differently from a stock that genuinely has no listed
sector — the first should fix itself on the next check, the second likely
won't — and the message says which one happened rather than reading the
same. The 75%/90% numbers themselves were not touched, and an ordinary,
successfully-resolved sector behaves exactly as it did before.

**Not done, and why.** No offline sector table was built for the 80
uncovered names — one that stays correct is its own project, and the point
of this fix is that the cap no longer depends on the lookup succeeding. The
order-sizing pass that pre-shrinks a trade for a crowded sector before it
ever reaches the risk check still does not count an unresolved sector
either — that is separate, unrelated code (fractional order sizing) another
effort owns tonight. Nothing is silently unenforced as a result: the risk
check's hard wall is what actually stops a trade from over-concentrating
regardless of whether it was pre-shrunk, and that wall now sees an
unresolved sector correctly.

### 2026-09-01 morning — the desk judged every trade by dividing a measurement by a guess

**What broke, plainly.** The desk looked at 38 tradeable ideas and placed
nothing. Every idea is scored on how much it could make against how much it
could lose. The "could lose" half was a real number the system measures off
the chart. The "could make" half was a number the AI simply wrote down. Two
thirds of the ideas failed the score before any human-style judgement was
applied at all — including the two the desk was most confident about.

**The real cause.** The score was arithmetic performed on an opinion, and it
failed systematically rather than randomly: a correctly-measured wide stop
divided into a modestly-guessed target misses the threshold as a matter of
arithmetic, whatever the trade is actually worth. **The threshold was not the
defect and was not moved** — lowering a threshold that sits on invented
numbers leaves it sitting on invented numbers.

**What was done.** The "could make" number is now computed from the same
measured chart structure the "could lose" number already came from, or the
trade is refused by name. Six distinct refusal reasons, because "no trade"
with no reason given is what let this survive unnoticed in the first place.
The AI's own guess is kept as evidence and the gap between the two is logged,
but it no longer enters the arithmetic.

**The thing to argue about next is the HOLDING PERIOD, not the threshold.**
The shape of the maths means a trade can only clear the bar if it is given
long enough to get there — roughly 27 sessions for an unstructured target, 12
when aiming at a real level on the chart. Below that the trade cannot pay
1.5:1 however it is judged. This is also why the stop-floor fix shipped the
same night matters: honouring a stop that sits on a real level roughly halves
the required holding period. The two are one fix in two halves.

**Not verified, and nobody should quote it as if it were.** How many of the 38
would now pass is unknown — the per-symbol chart data from that run is not
available offline. The worked example in the tests uses invented chart data;
it shows what the rule does with a plausible chart, not what that stock's
actual chart contained.

**A second defect found while fixing this.** Widening a stop always moved it
to the correct side of the entry price — so a short trade handed a nonsensical
stop came out the other end looking valid, silently repairing the very error
the safety check exists to catch. It now refuses. The test that should have
caught this passed only because its fixture had no volatility reading, which
is not a state production ever reaches.

### 2026-09-01 — the benchmark rig could not spend money; the trading desk was never affected

**In plain words:** three separate things stopped a model test from running.
None of them touched live trading. All three were in the test setup, and all
three were fixed the same afternoon.

**1. The working copy had no credentials file at all.** `AppConfig` refuses to
load when a provider named in `settings.yaml` has no key, so the benchmark
died at startup before any model call. Same failure family as the
`GOOGLE_API_KEY` blocker the day before (below) — and the same fix: a `.env`
holding only `placeholder-managed-by-onecli` values, with the real secrets
injected by the gateway on the way out. **The lesson repeats: a green CI run
says nothing about whether a config will load in the environment that has to
run it.**

**2. The benchmark billed against production's live-trading spend caps.**
`benchmark_models.py` runs inside the real LLM cost circuit, whose limits are
`session_cost_limit_usd: 0.90` and `daily_cost_limit_usd: 2.75`. Those exist to
stop a runaway *trading* session; a deliberate 23-trial benchmark is not that,
and would have tripped the session cap partway through and reported a
misleading partial result. Fixed by running against a benchmark-only config
with raised ceilings and a scratch database, so production's ledger, database
and daily budget are untouched. **Do not raise the production limits to make a
benchmark fit.**

**3. Muting Telegram latched the cost circuit closed.** The circuit treats a
reachable operator as a mandatory precondition for spending anything —
`require_telegram_alerts`, which `config.py` explicitly refuses to let anyone
set false. Setting `TELEGRAM_DISABLED=1` (to stop a benchmark alerting Rex's
phone about a circuit that is not his desk) therefore disabled the notifier,
which marked the circuit unavailable, which blocked all paid analysis. The
correct lever already existed: `QAMC_REHEARSAL=1` keeps the notifier *enabled*
so the precondition is honestly satisfied, while suppressing and logging every
send. Blast radius verified as `src/notifier.py` alone. **This is a good
design working as intended — the fix was to use it correctly, not to weaken
it.**

**Also cleared:** the failed attempt left an emergency-latch sidecar
(`bench.db.llm-circuit-unavailable`) that would have kept the next run blocked.
A latch is sticky by design; clearing it is an explicit operator step.

### 2026-09-01 — GLM 5.2 fails outright roughly one run in three at the PM seat

**In plain words:** a cheap model that looked perfect turned out to produce
nothing usable on one run out of three. When it fails, the portfolio manager
returns no decision at all, so that session places no trades.

Two trials on 2026-08-31 scored 1.00 at $0.055/run and made GLM 5.2 look like
an ~8x cost saving at the most expensive seat. A third trial on 2026-09-01
scored **0.00**: the model ran to `output_tokens: 16000`, hit the `max_tokens`
ceiling exactly, was truncated mid-JSON, and `finish_reason=length`. The
portfolio manager returned a non-JSON response and the whole review was
discarded.

**Ruled out before believing it was the model.** The scenario is a frozen
fixture, not live market data — input was byte-identical across all three runs
(21,033 tokens). `max_tokens: 16000` is a static config value, not fitted from
history, so the empty scratch database did not change it. GLM's own output
length is what varied: 7,219 / 9,071 / 16,000+. The settings comment notes the
16K ceiling was sized to cover "the observed production PM payload (~11K output
tokens)" — GLM runs well past that envelope.

**Why this matters more than the price.** A seat that silently produces nothing
one session in three is worse than an expensive seat that always answers. Cost
per run is the wrong metric on its own; cost per *usable* run is the number.
Two samples said "perfect" and were wrong — this is the argument for repeats.

Status at time of writing: a 10-repeat run is in flight to establish the real
truncation rate. **The seat was NOT moved.** GPT-5.5 remains the portfolio
manager.

---

### DEPLOYED 2026-08-31 21:20 UTC — and one blocker that tests could not catch

Running at `b88b836`. PRs #202 and #203 both merged, API restarted, served
cockpit bundle matches disk, rehearsal PASS against the deployed code with the
production database byte-identical.

**The blocker worth remembering: `GOOGLE_API_KEY` was not on the box.** Seven
seats now declare `provider: google`, and `_check_llm_provider_keys` refuses to
load a config whose in-use provider has no key. Not degraded — the desk would
not have STARTED, and the first symptom at 09:30 would have been silence. Every
test passed and both PRs were green; the thing that would have broken lived
entirely outside the repo. Fixed by adding
`GOOGLE_API_KEY=placeholder-managed-by-onecli` (same convention as
OPENROUTER_API_KEY — the real credential is injected by the gateway on the way
out; .env only needs it non-empty). Original .env backed up to
`/home/qamc/.env.bak-2026-08-31`, OUTSIDE the repo so it cannot read as drift.
**Generalise this: after any change to which providers the seats use, load the
config ON THE BOX before trusting a green CI run.**

**Proven live, not assumed.** A forced evening session ran on the new routing at
21:29 UTC: `evening_analyst` on `gemini-3.5-flash-lite`, 20,642 tokens, **cost
$0.00**, status `analyzed`. The free tier works end to end through the gateway,
and the $0.00 pricing row settles correctly rather than reading as unknown. The
two new alert lines were also confirmed rendering from the box.

**Runway.** OpenRouter is prepaid: $32.09 left, per-key cap removed. A CLEAN
day costs $1.02 (2026-08-27 — the only day that week where all six sessions ran
and the morning completed first time). Never estimate from an average of recent
days: they are cheap precisely because the desk kept crashing early.

### 2026-08-31 evening — the reward:risk gate was being narrated, not computed

**Two forced sessions rejected every trade. Neither rejection was a judgement
call; both were the deterministic constructor and the LLM Risk Manager
disagreeing about facts the RM should never have been asked to derive.**

- **The RM was doing the arithmetic its own gate is judged on.** It receives
  the constructed order as bare Entry/Stop/Target text with no ratio. For a
  BUY on RSG it computed the ratio TWICE IN ONE RESPONSE — `rr_audit` said
  "R/R = 1.65 ... above 1.5, so compliant", `reasoning` said "R/R = 1.31,
  which is below the 1.5 floor" and rejected. The pipeline acts on
  `reasoning`. 1.65 is right; 1.31 matches no combination of the inputs.
  `TradeDecision.reward_risk` is now a Python computed field, mirroring
  `TechAnalysisResult.risk_reward` and its "not trusted to the LLM" rule,
  rendered into the prompt and declared authoritative there. PR #202.
- **The constructor removed trades without telling anyone.** It struck NVDA on
  the reward:risk floor; PM's narrative — written BEFORE construction and
  rendered verbatim — still argued for it, so the RM vetoed the whole plan
  ("While COP and V are valid, the plan as presented is not internally
  consistent"). Two valid trades died for a bookkeeping mismatch.
  `PortfolioDecision.constructor_dropped` now carries removals into the
  prompt. Same pattern as the existing `cap_note`, whose own comment records
  the identical failure from 2026-08-20 — solved once for allocation caps,
  never extended to removals. PR #202.
- **Ten new tests over the constructor→RM handoff, which had none.** No test
  anywhere built a widened-stop order and asserted what the RM prompt shows
  for it. That is exactly the seam both defects lived in.

**Three grandfathered stops widened to the noise-band floor.** V, DIS and
CMCSA were opened 2026-08-27 13:36 UTC; `min_stop_atr_multiple = 3.0` was
committed the same day at 22:28 UTC, after the close, so they were never
subject to it. Measured 2026-08-31 they sat at 1.02x / 1.03x / 1.62x ATR —
inside a single ordinary day's range for the first two. Widened via
`broker.replace_stop_loss(..., allow_lowering=True)`, the same supported path
the ex-dividend adjustment uses, NOT a hand edit: it snapshots and rolls back
on failure and leaves no unprotected window for the ~30-min coverage
reconciler to "repair" by reinstating the original tight stop. MSFT was left
alone — its live stop had already trailed up to 485.10 and is correct against
current price, not the 480.30 the entry row still records.

**The ledger was corrected, on owner instruction, and the tool has no path for
it.** Recorded spend went 2.1741 -> 0.2494, the exact sum of `agent_logs`
provider-reported costs for the ET day. `scripts/cost_circuit.py` only ever
clears latches and promises never to erase settled spend, so this was a direct
row edit. **Editing `llm_budget_days` alone raises an emergency latch** — a
hard check joins it to `llm_budget_sessions` and `llm_budget_reservations`
(`cost_circuit.py:1216-1247`) and both ledgers must move in one transaction.
Learned by tripping it. Prior row backed up to
`data/ledger_backup_2026-08-31.json`; caps unchanged.

**STILL UNRESOLVED — do not let anyone tell you this is closed.** Whether a
real OpenRouter rate-limit reaches `_is_known_zero_cost_failure` carrying
`status_code = 429` is UNVERIFIED. Reading the allow-list is not proof. The
rehearsal rig cannot settle it either: `ops/rehearsal/faults.py` builds its
`RateLimited` fault with a hard-coded `status_code = 429`, so it encodes the
assumption in doubt and can only ever confirm it. Settling this needs a
captured real rate-limit, or a classifier robust to both shapes.

### DONE 2026-08-31 — primary moved to Gemini direct, OpenRouter to backup

**Shipped as PR #203.** Seven specialist seats run `provider: google` /
`gemini-3.5-flash-lite` on the AI Studio free tier; OpenRouter carries the
same model as a paid backup, so a failover changes the road and not the
reasoning. The failover target is configuration now, not the hard-coded
`claude-opus-4-7` — an inherited remnant at ~50x the primary's price that had
never once completed, because no Anthropic credential was ever configured.
Measured limits, endpoint verification and rates are in the code comments and
PR #203; not restated here.

**Three things that are still open, and only these:**

- **`position_reviewer` was deliberately NOT migrated.** It is a decision seat
  and `test_decision_seats_run_a_model_measured_at_that_seat` demands a model
  measured at its own scenario. No benchmark exists for 3.5 at `midday_exit`,
  and none can be produced against the 2.5 incumbent because Google refuses
  2.5 to new keys. It moves after
  `ops/model_policy/benchmark_models.py --scenario midday_exit --models gemini-3.5-flash-lite`
  is run and committed.
- **`ops/commissioning/verify_commissioning.py` carries stale
  `EXPECTED_PROVIDER` / `EXPECTED_ROUTING` constants** and will report FAIL
  against the new routing. Its own tests do not catch this because they
  self-reference the same constants instead of reading `settings.yaml`.
- **This saves ~7% of spend, not the bill.** Measured from `agent_logs` over
  7 days: `openai/gpt-5.5` (portfolio_manager) is $6.64 of a ~$7.16 week — 93%
  of spend on 21% of calls. The eight Gemini seats are $0.51 between them. The
  cost ledger remains, almost entirely, a guard on the PM seat. The real lever
  there is input size and prompt caching, NOT a weaker model — the owner has
  ruled that out and is right to.
### 2026-08-31, afternoon — charged $0.62 for calls that cost nothing

**The rate limit did not stop trading. The phantom bill for it did.**

A logical call can attempt several DIFFERENT providers, and `BaseAgent.run()`
re-raises the PRIMARY error while discarding whatever the failover hit. The
cost circuit judged "did this cost anything" from that single exception — so a
failover rejected **401** (which by definition billed nothing, and which the
zero-cost allow-list already covers) was invisible to it, and the whole call
was charged its conservative reserve at the **failover model's** dearer price.

Observed live at 12:36 ET: `news_analyst` refused 429 upstream, refused again,
then 401 from the failover for want of an Anthropic credential. Real cost
**$0.00**. Charged **$0.6159**. The unexplained spend tripped
`failed_call_unknown_cost` and latched the desk — for the third time that day,
and on the same underlying pattern as the 09:32 shutdown.

**Fixed:** every attempt's failure is now carried to the circuit, and a call
is treated as free only when **every** attempt is provably free. The rule gets
STRICTER, not looser — one ambiguous attempt (a cut stream, an unclassified
error, a 5xx after generation may have started) and the whole reservation is
charged exactly as before. A call is only known to have cost nothing when
nothing it did could have cost anything. Callers that cannot enumerate their
attempts keep the old single-exception behaviour, so this can only ever
recognise more genuinely-free failures, never fewer.

**And an unknown turned into a measurement.** When a call IS charged, the
shapes of every attempt are now logged — exception type, status code, and
which one made it chargeable. The remaining open question is whether
OpenRouter's "temporarily rate-limited upstream" arrives with a status the
allow-list carries; it could not be settled by reasoning and deliberately was
not settled by hammering the provider to reproduce it. The next occurrence
will say.

### 2026-08-31, afternoon — why the provider was refusing us, and the fix

**The owner's framing, and it was the right one: never discover a limit by
hitting it.** Every request costs money, so the work of sizing one belongs on
our side of the wire. That rules out both obvious designs — a rate ceiling
that backs off when it trips, and a ceiling that learns from refusals — because
each pays the provider for the lesson.

**What was actually happening.** Every one of the eleven rate limits in three
weeks of logs hit the **same agent**, the Technical Analyst, and no other agent
was declined once. It is the only one that bursts: ~314,000 tokens inside 80
seconds, ~252,000 per minute, against 20-30k for everything else. Not market-open
congestion — the refusals land at 10:01, 10:30, 11:31 on other days. And
OpenRouter's own message says why: *"add your own key to accumulate your rate
limits"* — we share its pooled Google credentials, so other customers' traffic
counts against us.

Nothing bounded the burst. Concurrency was capped at three requests and context
was capped per call, but a rate limit counts **tokens per minute**, and three
concurrent 80,000-token requests clear both caps while being exactly what gets
refused.

**Three things were wrong, all measurable:**

1. **72% of the payload was raw price bars, and half of that was noise.** Bars
   were serialised at full float64 precision — `O=14.920000076293945` for a
   stock that traded at $14.92. Those digits are float32-to-float64 conversion
   artefacts, not price data; the source could not represent them. Rendering at
   six significant figures cuts the payload 30% and loses nothing.
2. **The batch split was a fixed count calibrated against a stale assumption.**
   `_CHUNK_SIZE = 25` was set against "~300 input tokens per symbol". The bar
   window grew from 20 to 40, per-symbol context was added, and the real figure
   became ~3,600. Nothing failed loudly, because context length was never the
   binding constraint — the model takes a million tokens. **A count cannot bound
   tokens.**
3. **Relative-strength context depended on how the batch was split.** The index
   ETFs sit at the top of the universe, so only the first chunk ever contained
   SPY; every symbol after it silently lost its benchmark. Which symbols kept it
   was a function of the chunk size.

**The fix — build to a budget, not to a count.** Requests are packed until the
next symbol would exceed a token budget, then a new request starts. An oversized
request is not unlikely, it is unreachable: nothing is added to a full batch.
The bytes-to-tokens conversion comes from a model fitted to each agent's own
past calls — data already paid for, so measuring costs nothing and improves as
the desk runs. It never learns from a refusal.

The model is two parameters, and both are physically real:

    tokens = fixed_tokens + tokens_per_byte x content_bytes

    tech_analyst        4,127 tok + 0.939 tok/byte
    news_analyst        4,200 tok + 0.227 tok/byte
    portfolio_manager  12,483 tok + 0.214 tok/byte

The intercepts recover each agent's system prompt (tech_analyst's is 19,792
bytes of prose; 19,792/4,127 = 4.8 bytes per token, exactly what English
tokenizes at). The slopes recover the content: ~1.07 bytes/token for dense OHLCV
digits, ~4.4 for prose. Median prediction error 0.9-4.3%. A single
bytes-per-token ratio cannot express this — it folds a per-request constant into
a per-byte rate, so it is right at only one message size, and tech_analyst's
messages span 6KB to 379KB.

**Measured on 53 real production symbol sections:**

| | requests | peak request | total |
| --- | --- | --- | --- |
| before | 3 | 136,449 tok | 290,329 tok |
| trim only | 3 | 96,135 tok | 205,833 tok |
| **trim + 45k budget** | **5** | **44,654 tok** | **214,087 tok** |

**Peak down 67%, total down 26%** — both directions improve, because the trim
more than pays for the extra repeated system prompts. Worst case with all three
concurrent slots full is ~134k/min, against the ~252k/min that was being refused.

**And the repeated system prompts may be free anyway.** This seat's route bills
a cached prompt token at $0.01/M against $0.10/M — a 10x discount that decides
the trade-off outright. Nobody knew whether it was happening because nothing
read the field. Cache hits are now logged; the answer arrives with the next
session's logs.

**Why 45,000 and not something else.** It is where the curve knees: 60k saves 2%
of total tokens for a 33% higher peak, 100k saves 4% for a peak nearly three
times larger. Overridable with `QUANT_AGENT_TECH_REQUEST_TOKENS`.

**A tokens-per-minute governor exists but is a BACKSTOP, not the control.** It
cannot fire in normal operation now — the packer cannot emit a burst that large.
It is there for what the budget cannot see (a new agent, a prompt that grows, a
retry storm) and it reports at CRITICAL if it ever engages, because that would
mean something grew.

**Failure posture, deliberately.** Nothing in the sizing path may take a session
down: every entry point catches broadly and degrades to the old fixed split. An
efficiency measure that can stop a trading desk is a bad trade — which is the
whole lesson of the morning recorded below.

**Left open, owner's call:** our own Google key (a free tier exists, and Google
serves the *same* Gemini model directly, so it is a same-model backup with no
consistency problem at all); and whether to pin a cheaper route — the identical
model is served by five endpoints, one at half our current price.

**Not the cause, and a correction to an earlier note in this file:** the
reservation estimator's conservatism was reported here as 6.8x. That was an
apples-to-oranges comparison of morning estimates against intra-day actuals.
Measured like-for-like across ten runs it is **1.49x**, which is roughly what a
deliberately conservative reserve should look like. It contributed to the
2026-08-28 ceiling trip; it is not the pattern, and it has not been changed.

### 2026-08-31, market open — the desk went dark two minutes after the bell

**Closed by `fix/provider-failover-attempt-budget`. Read this before touching
the retry, failover or cost-circuit code.**

What happened: at 09:32 ET, two minutes after the open, the morning session
stopped with `paid_analysis_suspended` and every session after it no-opped.
Spend at the moment it stopped: **$0.05 of a $2.75 day.** It required a
manual operator reset, so the desk stayed dark until a human noticed.

The cause was an arithmetic contradiction between two settings that lived in
different files and were never compared:

| | value | where |
| --- | --- | --- |
| attempts the retry loop can spend on one call | 3 (two primary + one failover) | `_max_retries()` in code, env-overridable |
| attempts the cost circuit permitted per call | 2 | `config/settings.yaml` |

So **cross-provider failover could never once complete.** Every failover was
attempt three against a ceiling of two. It only ever mattered when the
primary provider failed — which is the one situation failover exists for —
and on 2026-08-31 an upstream rate-limit on the cheap primary finally
produced it. The same family of limit had tripped on 08-26, 08-27 and twice
on 08-28; each time a different number was raised and this one was not
touched.

Making it worse, `provider_attempt_limit` was the only trigger of its family
still wired to the durable operator-reset latch. Its strictly WIDER sibling,
`session_retry_attempt_limit`, was already correctly scoped to the session.
Nothing chose that: unrecognised codes default to a hard latch.

What changed:

1. **The ceiling is derived, not typed.** `provider_attempt_budget()` in
   `src/agents/base.py` owns the arithmetic, next to the loop that actually
   spends the attempts. `config/settings.yaml` no longer pins it.
2. **Disagreement is now a startup failure.** `AppConfig` refuses to load a
   ceiling below the loop's worst case, naming both settings. It fails on
   the ground instead of at 09:32 on a Monday.
3. **`provider_attempt_limit` holds the session instead of latching the
   desk**, matching its sibling.

**Why a weekend of testing and auditing missed it, and the thing actually
worth remembering:** the suite had thorough failover tests AND thorough
circuit tests, and every one of them passed throughout. `tests/conftest.py`
sets `_allow_unmetered_for_tests = True` for the whole suite, so the failover
tests ran with **no cost circuit attached at all**, and the circuit tests ran
with no failover. Nothing anywhere ran the two together — which is precisely
where they contradicted each other. Two well-tested halves, an untested seam.
`tests/test_provider_attempt_budget.py` now attaches a real breaker to a real
agent and fails the primary for real reasons.

**And the rehearsal rig could not have caught it either**, which is why it
now can — see the fault-injection note above. Every recorded response it
replays is a response that succeeded, so the failure branch was unreachable
offline. It was reachable only by waiting for the market.

**A second defect this one was hiding, found by deploying it (same day):**
`fail_call` stamps the ET day's accounting inexact when it charges a
conservative reserve for a request whose true cost it never learned, and
nothing clears that flag within the day — it lifts only when the next ET day
seeds a fresh row. The quota reconciler refuses to rearm over an inexact day,
by design. Those two facts had never met, because an inexact day always came
with a hard latch and the reconciler returns early whenever one is set. **The
latch was masking the refusal.** Scoping `provider_attempt_limit` to the
session removed the mask, and the live desk went straight from "suspended" to
"every session start raises, and no operator action can clear it until
midnight". A crash loop is a worse failure than the suspension it replaced.

Fixed by letting an operator reset clear an inexact day, with the same
mandatory audited reason. It clears only the *we-could-not-prove-this-figure*
flag; the recorded amount is left exactly as it stands, conservative reserve
included, because that over-states cost rather than under-stating it and
`scripts/cost_circuit.py` promises reset never erases settled spend. Judging a
conservative figure good enough to continue on is an operator's call;
recomputing what the provider really charged is not something the code can
honestly do.

**The one underneath both of those — the real cause of the pattern (found
10:36 ET on the live desk, while verifying the fix):** the quota reconciler
runs on **every** paid call, and it refused to proceed whenever the current
ET day's accounting was inexact — even when it had nothing to do. Its
exactness precondition guards exactly one operation: releasing a hold carried
over from an *earlier* day. The check sat before the query that finds those
holds, so it fired when there were none.

`fail_call` stamps the day inexact whenever it charges a conservative reserve
for a request whose true cost it never learned. So **the first failed request
of any day poisoned every paid call after it** — the refusal reads as the
circuit's own infrastructure failing, which writes the emergency latch and
stops the desk until an operator clears it.

One rate-limited request, and the trading day was over. **That is the
2026-08-26 / 08-27 / 08-28 / 08-31 pattern**, and it survived four rounds of
raising limits because nobody was looking at the reconciler: the hard latches
masked it, since it returns early whenever one is set. Removing the last mask
is what finally showed it — as a crash rather than a suspension, which is
worse, and which is why it was found within minutes of deploying instead of
next Monday.

Fixed by restoring the check to what it protects: no cross-day hold, nothing
to rearm, nothing to be exact about. The safety property is unchanged and
pinned by its own test — rearming yesterday's stop on unproven books is still
refused.

**Two things deliberately left for the owner, not fixed here:**

- **The backup model costs ~50x the primary.** Primary is
  `google/gemini-2.5-flash-lite` at $0.10/$0.40 per million tokens; the
  failover target is hard-coded to `claude-opus-4-7` at $5.00/$25.00. For
  the Technical Analyst's ~150k-token prompt that is ~$0.02 versus ~$0.95 —
  against a $0.90 per-session cap. So a failover on the biggest agent now
  completes and delivers its analysis, then immediately holds the session on
  cost, which can starve the Portfolio Manager that runs after it. Better
  than a dead desk, but not a full rescue. A backup priced near the primary
  would be; `_FALLBACK_MODEL` is a module constant with no config knob.
  Model strategy is TABLED pending a clean spend re-measure, so this is
  recorded, not changed.
- **`fail_call` still charges for a request the circuit itself refused to
  send.** A blocked attempt is provably $0 — no bytes left the process — but
  the reservation covers the whole call, and earlier attempts on that same
  call may have burned tokens nobody was told about. Marking it zero-cost
  would risk under-counting real spend, which on a system that trades money
  is the worse error. It no longer fires on this failure mode (with the
  ceiling correct the failover simply succeeds), so what reaches it is a
  genuine attempt runaway — arguably a thing an operator should look at.
  Asserted explicitly in `test_cost_circuit.py` rather than glossed.

---

## 2026-09-02 — the rehearsal rig's verdict was a coin flip

**The pre-deploy gate has been giving PASS or FAIL on the same code depending
on which recorded responses it happened to draw. A green light from it meant
less than anyone believed.**

Found while gating the 2026-09-01 ship. The merged tip returned FAIL where the
starting commit returned PASS, so the merges were bisected one at a time with
the rig, each checkout verified clean before running.

Bisect result: the flip appeared at `b8d5986`, the fix that stopped a null
`thesis_invalid_if` binning an entire technical analysis. That fix is
demonstrably correct — it eliminated all 10 parse failures in the run and
recovered 2 more symbols.

**Then the variable was controlled.** With `--replay-run` pinned to a single
recorded session, BOTH commits FAIL identically, and the fix reduces rejections
from 23 to 21. Unpinned, the rig draws on ALL recorded responses; any change to
how many analyses parse consumes that shared pool differently, a different
recorded PM decision gets replayed, and the grounding check then compares that
decision against a session whose analyst coverage does not match it. The
verdict tracks pool consumption, not correctness.

Symptom to recognise: `pm_grounding_error` naming a symbol that is NOT in the
tech batch's unresolved list — on 2026-09-02 it was `ZS`, a real target from
the previous afternoon's session, replayed into a morning that never analysed
it.

**What this means for anyone using the rig as a gate:**
- An unpinned PASS is not evidence. Two runs of the same code can differ.
- The rig CANNOT return PASS on this scenario in any state, because it cannot
  reproduce full analyst coverage offline. That is a gap in the gate, not in
  the code — see "what is still not fixed" below.
- This is the same family as the already-recorded limitation that the rig
  cannot validate a prompt change. Both come from replaying recorded answers
  into a session that no longer matches them.

### Fixed the next day (2026-09-02)

**The replay is pinned by default.** Omitting `--replay-run` no longer means
"draw on all history"; it now means "the most recent COMPLETE recorded run of
this session type that had already started by `--as-of`", chosen by
`select_replay_run` in `ops/rehearsal/replay.py` and **printed under the
verdict** so a reader knows what was compared. The verdict is now a function
of (code, session, `--as-of`, database) and nothing else. `--replay-run <id>`
still overrides; `--replay-run any` asks for the old pool-wide behaviour
deliberately and says in the report that the result is not reproducible.

Reproduced end to end rather than asserted, on the two commits that
disagreed, with the same unpinned invocation both times and the checkout
verified clean inside the runner script:

| commit | before | after |
|---|---|---|
| `af266de` | **PASS**, 22 rejections | **FAIL**, 23 rejections |
| `0bbb69c` | **FAIL** (`ZS`), 21 rejections | **FAIL**, 21 rejections |

Both now auto-pin to `run-64290730` and fail identically on
`NVDA: claims earnings coverage that does not exist`. The 23 → 21 improvement
that used to read as a PASS → FAIL regression now reads as what it is.

**A third verdict exists: INCONCLUSIVE.** A replay-coverage mismatch used to
be printed exactly like a real defect, which is how a red gate got argued
about for an hour instead of believed or dismissed. `_replay_fidelity`
(`ops/rehearsal/report.py`) now separates them, and is deliberately narrow —
it downgrades only when BOTH hold: the answer replayed for the portfolio
manager came from a different recorded run than the analysts' answers (a
mechanical fact, and impossible under a pin — so a pinned run can never be
downgraded), AND the failure names a symbol this session never analysed,
never rejected and does not hold. Either alone stays FAIL. A hallucinated
ticker in a faithfully replayed session is still the defect it is. Exit codes
are now PASS 0, FAIL 1, INCONCLUSIVE 2.

**The report states its own coverage next to the verdict**, not in the log:
"INCOMPLETE ANALYST COVERAGE: 20 of 56 symbol(s) never got a technical
analysis in this rehearsal ... Do not read it as 'the session was fully
exercised'", plus a one-line summary of how far the replayed prompts have
drifted from the recorded ones (worst overlap 23% on the news seat, 42% on
the portfolio manager, on both commits above).

**What is still not fixed, and is the more important half.** Pinning makes
the gate honest, not useful. A pinned morning rehearsal still cannot PASS,
and the reason is not the code: offline, `macro` and `news` fail outright,
`smart_money` is degraded and `tech` is partial, so the session reaching the
decision stage is not the session the recorded portfolio-manager answer was
grounded in. That answer legitimately cites evidence the rehearsed session
does not have, and the grounding gate correctly throws it out. **The rig can
therefore tell you a morning got worse; it cannot yet tell you a morning is
well.** Nothing in this fix changes that, and the conservative choice was
made deliberately: that failure reports FAIL, not INCONCLUSIVE, because it
does not meet the two-part test above.


## Archive — work completed before 2026-09-01

Moved out of `docs/WORK.md` on 2026-09-01 under the rule at the top of that
file: **finished work is moved here, never deleted.** WORK.md is capped and
loaded into context every session, so it must hold only what is still to be
done.

Every claim below was checked against `gh`/`git` and against the production
checkout before being moved — all 20 PRs cited are genuinely merged AND
deployed (production HEAD matched `origin/main` at the time of the check).
Four claims did NOT survive that check and are corrected inline where they
appear; they are listed here so the corrections are not buried:

1. **Phase 9 (the research desk deliberates) was listed as the FIRST pending
   item.** It is done: §9.1/9.2 shipped as PR #153 and §9.3/9.4 as PR #160,
   both deployed. Only §9.5 (the conviction ledger) is partial, and that was
   never named in the item.
2. **"Insider filter — PR #133 not yet merged/deployed."** Merged 2026-08-29
   and deployed. WORK.md already contradicted itself on this two hundred
   lines further down.
3. **"Inverse-ETF retirement still outstanding."** Obsolete rather than
   undone — the owner reversed this on 2026-08-30 and the inverse ETFs stay.
   `SH`/`SDS`/`PSQ`/`SQQQ` remain in the universe deliberately.
4. **"26 unmerged branches await triage."** The remote now carries 5
   non-main branches. The VPS security branch named there no longer exists;
   its salvageable content was rescued as PR #143.

Text below is moved verbatim. Where an entry contains its own later
correction, both the original claim and the correction are preserved — that
pairing is the record.

**Landed (2026-08-31) — six PRs, all merged and deployed. Nine open defects closed, four more deleted, six new ones found — read this first**

Tonight's audit worked through the thirteen open defects recorded below on 2026-08-30. Full detail and re-check commands for every item are in `docs/phases.yaml`'s `open_defects` entry — this is the plain-language summary.

- The macro event calendar is now half-real. The desk fetches a genuine forward schedule of seven US macro releases (CPI, payrolls, PPI, PCE, GDP, retail sales, jobless claims) from a free government source and shows it to the Risk Manager and the Macro Analyst, and the earnings-date lookup that existed but was never called is now wired in. **Still missing: Fed meeting dates specifically.** No free source publishes those, so both prompts now say so outright instead of guessing. Whether to add the Fed's own free calendar page just for that has been put to the owner — **not yet decided.**
- The price-list refresh gap is closed. A scheduled job now refreshes it twice a day, every day including weekends, and pages over Telegram the moment it starts going stale rather than waiting for the hard cutoff. (Correction to an earlier note: the claim that the box had no scheduled jobs at all was a checking mistake, not a real finding — the box has always had them. The refresh gap itself was real and is now fixed.)
- The evening report's lessons and reminders now actually carry forward: they're saved and read back by tomorrow's trading decisions instead of being generated and thrown away. One of the four fields — the report grading its own prior forecast — is kept for the record but deliberately not fed back in, to avoid the same self-review loop that was cut once before.
- A smaller inconsistency is fixed: the parked cash-equivalent holding can no longer take up one of the limited slots meant for real positions' news coverage.
- Every trade's conviction, requested risk, allocated risk and which model decided it are now visible on the dashboard and through the API, not just recorded internally.
- The rehearsal tool used to test changes offline no longer depends on how stale the live price list happens to be at the moment someone runs it — that dependency mismatched what it was supposed to be testing and could produce a false failure.
- A safety-net test that was supposed to guarantee every outcome prints a plain explanation, but only ever checked itself against itself, has been rebuilt to check against the real code instead. It already found one genuine, small gap in the process (see below).
- Three previously-identical "nothing happened" outcomes inside the mid-day quick check (feature off, already running, nothing found) now report distinctly, so they can be told apart after the fact. All three are and remain harmless.
- Four pieces of dead code confirmed to have zero callers anywhere, including tests, were deleted: an unused local copy of the holdings list, a write-only bookkeeping field, a superseded internal data shape, and four small orphaned helper functions.

**Landed (2026-08-31, later) — two of the items below were fixed the same night**

- Fed meeting dates are no longer a gap. The Federal Reserve publishes its own
  meeting calendar free and the desk now reads it. A second source covers the
  years the machine-readable feed does not reach — without it the desk would
  have confidently reported "no meeting" for dates it simply could not see.
  "No meeting is scheduled" now prints only when a real schedule genuinely
  covers the window asked about; every other case says so in words.
- The alarm that tells the owner a change never reached the live server can
  now actually send. It never could: the credentials were never wired into it,
  so an alert would have gone to a log file and nobody. It has a probe that
  proves the channel still works rather than assuming it.
  **Not finished:** the accompanying scheduled jobs are written but were
  deliberately NOT switched on. They implement a weekly confirmation the owner
  rejected — a week of undetected silence is not monitoring. The replacement,
  where every trading session proves the alert path as part of its own run, was
  unfinished when this was written and has since merged — see below.

**Closed (2026-08-31, later still):**

- The mid-day failure outcome that would have shown a raw internal code now
  explains itself in plain English. The safety-net test that found it is back
  to tracking nothing, which is the state it is meant to be kept in — anything
  parked in it is a defect deferred in writing.
- The offline rehearsal tool no longer misreports how much slack the price-list
  safeguard has. **This was recorded as cosmetic and it was not.** It was
  checked before the setting was available, so it always read "none" — which
  meant the tool told the reader the desk would refuse to run any paid analysis
  in exactly the situation where the desk would in fact have run normally. The
  opposite verdict, not a wrong number. The silent guess that hid it is now a
  hard stop: asked before it can know, the tool refuses to answer rather than
  making something up.
- The scheduled job that pointed at a folder from the project's original owner
  is corrected and merged. It was never wrong on the live server — only in the
  repository's own copy, which meant installing that copy would have broken the
  daily report on contact. Confirmed against the running server, not just the
  merge: the job runs from the right folder and its last run sent the report
  as normal.
- The setup guide one internal file pointed readers at, and that never
  existed, is no longer promised. The reference was removed rather than
  writing a guide — the module it points at already documents itself in
  full, and a second document would only have restated it.
- Full wire-service news coverage is formally closed, not merely unchanged:
  the owner has declined the paid subscription it would need. Free coverage
  stays as already widened.

**Both pull requests noted here as unfinished have since merged:** every one of
the server's startup files is now under version control with an automatic daily
check that reports any difference between the server and the repository; and
the alert-path rework landed, making the trading sessions themselves the
alert-channel watchdog and retiring the weekly digest the owner rejected.

**Landed (2026-08-30, later) — two fixes plus a close call, all deployed**

- The macro data feed's retry policy has been rebuilt. The old one gave a failing economic-data series one quick second try and then gave up; that is exactly what let one bad three-minute stretch (2026-08-26) lose all nine numbers the macro seat reads, silently. It now tries harder, with a real time limit on the whole job — a minute and a half, not per series — so a slow patch can be ridden out without ever risking a session running long. Six new free indicators were added on top of what was already tracked — a real (inflation-adjusted) 10-year yield, the market's inflation expectation, a 3-month Treasury rate, the dollar's strength against other currencies, investment-grade borrowing costs, and weekly unemployment claims — each checked against the real data source before being wired in. And if any of these numbers fail to come back, the desk is now told so directly, the same way it is already told when the news feed is degraded, instead of quietly reasoning from nothing (PR #162).
- A second, smaller repair: if the mid-day opportunity scan crashed partway through, it used to look exactly like a normal quiet check that found nothing — no error, no signal, nothing for anyone to see. It now says plainly that it crashed, and the rehearsal report counts that as a failure instead of a pass (PR #163).
- A close call, caught in time: the price list the spending safeguard uses to know what each AI call costs is supposed to refresh itself, but only when a real trading session actually starts one — nothing refreshes it on a clock. Over the weekend it sat unrefreshed long enough to cross the point where the safeguard would have refused to run any paid analysis at all come Monday morning, meaning the desk would have opened and done nothing. It was noticed and refreshed by hand before that happened. (Closed: PR #168 added a systemd timer that refreshes the price list twice a day, seven days a week — see the 2026-08-31 entry above.)
- Still missing on the macro side: there is no calendar of upcoming Fed decisions or inflation reports. Asked whether one is coming up, the desk still answers from what the model remembers, not from a real schedule.

**Found (2026-08-30, documentation audit) — ten more open defects recorded, none fixed yet**

An audit raised eleven candidate defects beyond the two already tracked above; ten verified real, one turned out false. All ten are now recorded in `docs/phases.yaml`'s `open_defects` entry as items (c) through (m), ranked by how directly each touches a trading or risk decision — read that entry for the full detail and the exact re-check command for each. In order: (c) the earnings-date lookup meant to ground the risk manager's mandatory event-risk check is wired to nothing, so that check still runs on the model's memory instead of real data; (d) the evening report's discipline-notes / selection-rules / thesis-update / outlook-grading fields are generated by the LLM every night and dropped before they reach storage or any later decision, so the loop the evening report explicitly promises never closes; (e) the evening session excludes the parked cash-sweep vehicle from per-symbol news selection but the two same-day checks earlier in the day don't, so it can occupy one of the capped news slots that would otherwise go to a real position; (f) every trade's conviction / requested-risk / allocated-risk / deciding-model fields are persisted but not exposed anywhere a human can see them, dashboard or API; (g) the rehearsal harness's own test that proves it can reproduce last week's spending-limit failure is quietly coupled to the same OpenRouter pricing-cache staleness already recorded as defect (b) above, so it can fail for a reason unrelated to what it exists to test; (h) a test meant to guarantee every outcome of the mid-day quick check prints a plain explanation is not actually exhaustive — it only checks that its own checklist agrees with itself — though it does not currently fail; (i) several healthy outcomes of the intraday opportunity scan (feature off, lock contention, nothing found) are indistinguishable from each other after the fact (benign, a residual loose end from this month's crash-visibility fix, PR #163); (j)-(m) are lower-consequence dead code found in the same pass: an unused local positions table nothing in production reads, a run-context field written once and never read back, a superseded news-analysis model family kept alive only by tests, and four small helper functions with zero callers anywhere.

**Checked and found NOT to be a defect:** a claim that `docs/STATE.md` pins a specific production commit that is now several merges behind current `main`. Verified live 2026-08-30: production HEAD and `origin/main` are both `6a8694a` — zero merges of gap (`scripts/status_board.py`'s own live `undeployed_merges` reading is 0). Not recorded as a defect.

**Separately noticed while checking the above:** `docs/STATE.md`'s "Intraday opportunity discovery" section — the file was dated 2026-08-27 at the top at the time — still said the broker layer has no short-selling capability at all today, and that nothing in the codebase tells the Portfolio Manager the inverse ETFs are bearish instruments. Both were false: shorting went live 2026-08-29, and the inverse-ETF/Portfolio-Manager wiring landed 2026-08-30 (PR #158). Flagged here rather than fixed in the moment — and fixed twelve minutes later anyway, in commit `4fb02e47`, which corrected both claims in place with dated notes.

**Landed (2026-08-30 through ~15:00 UTC 2026-08-31) — all five items now deployed to production**

- All five ordered items from the 2026-08-29 backlog have shipped. (1) Inverse-ETF longs now count against the bearish exposure ceiling, with a second commit fixing a sign error: shorting an inverse ETF is bullish, not bearish (PR #158). (2) Free per-symbol news feeds are scoped to held positions and candidates instead of universally requested (PR #157). (3) Every trade carries its allocation, conviction, and deciding model pinned at entry; exits label whether they link to an originating decision (PR #159). (4) The rehearsal harness can now read the intraday scan's own outcome report instead of only the top-level status — at the time, one limitation was left in place on purpose: a crashed scan produced no marker, so the session status stayed 'ok' even on crash (production honesty gap, documented but not fixed by design) (PR #156). That gap is now closed too — see "Landed (2026-08-30, later)" above (PR #163): a crash now attaches its own status and reports as a failure. The other limitation from PR #156 still holds: the nested-outcome path is unit-tested but no production replay has actually contained an intraday scan yet (none in live history so far). (5) The desk can now formally argue out disagreements and size trade risk by the number of independent seats that agree: a target carrying an unadjudicated conflict is dropped before grounding (punishment fits offence, single-target drop not session-wide), and risk_allocation_pct is ceilinged by agreement count in the deterministic risk code (PR #160, merged during this audit window).
- To check the live state: `sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1` should show PR #160 merged.

**Landed (2026-08-29) — read this first, supersedes most of what follows**

- Short selling is complete and live: the desk can now open a short and cover
  it, not merely hold one safely, on the same careful caps and gates a long
  trade gets.
- A tool to test a strategy change against real history now exists, though it
  can only check the mechanical trading rules, not what the AI agents
  themselves would have decided — that was never recorded, so it cannot be
  replayed.
- Any research seat, not only the chart analyst, can now bring a candidate to
  the desk's attention. Still missing: the desk formally arguing out a
  disagreement between seats, and sizing a trade bigger when more seats
  independently agree.
- The cost-circuit outage from two days ago is fully fixed, not just
  patched, and its safety backstop now recovers on its own instead of
  shutting a trading mode down for the rest of the day.
- Both defects logged below that were specific to short trades — blind
  performance stats and confused crash recovery — are verified fixed.
- The news desk's free source list was widened, and a feed that had quietly
  stopped publishing while still reporting success was found and removed.
  Real wire-service coverage still needs a paid subscription and an owner
  decision.
- All of the above is merged and confirmed deployed to production as of
  today. Most of "STILL OPEN — 2026-08-29" and "EXECUTION ORDER FOR THE NEXT
  SESSION" below is now done; see their own superseded-notices rather than
  reading them as current.

Five items, ordered by dependency then value:

1. **DONE (PR #158)** — Make the inverse funds coherent with real short selling. Since they stay, a
   long position in one is bearish exposure the short-side ceiling cannot
   currently see. That exposure now counts against the same ceiling, and the
   Portfolio Manager knows these are bearish instruments. Correction 2026-08-30:
   a second commit in the same PR fixed a sign error: shorting an inverse ETF is
   bullish (betting the underlying index rises), not bearish, and must not consume
   the bearish budget. The setting was renamed from `risk.max_short_gross_pct` to
   `risk.max_gross_bearish_pct` to reflect the widened scope.
2. **DONE (PR #157)** — Widen the free news sources further. Per-company
   coverage now exists but is scoped to the names the desk holds or is watching
   that day instead of universally, capped so it does not explode to ~100 requests.
   Yahoo per-symbol only; Seeking Alpha was verified working and deliberately not
   enabled due to cost constraints.
3. **DONE (PR #160, merged during audit)** — The unbuilt half of the research-desk work: seats formally arguing out a
   disagreement, and a name more independent seats agree on earning a larger
   share of the risk budget. A target carrying an unadjudicated conflict is now dropped
   before grounding (punishment fits offence — single-target drop, not session-wide).
   Sizing by agreement is now in the deterministic risk code (not a model instruction):
   risk_allocation_pct is ceilinged by how many independent seats are directionally aligned,
   indexed by agreement count. Default schedule is [3.0, 4.0, 5.0, 5.0, 5.0]%, keeping
   1-source trades at 60% of the 5% envelope, 2-source at 80%, 3+ at full 100%.
4. **DONE (PR #159)** — Log every trade's allocated risk against how it actually turned out, so
   conviction can be judged from data. Each trade now carries its allocated risk
   percentage, stated conviction, and deciding model pinned at entry. Exit rows
   label whether they link to an originating decision or have none. The grouping
   of outcome-by-conviction exists but is gated: below 20 per bucket it reaches
   the human operator only and is kept out of every agent prompt.
5. **DONE (PR #156, with one gap since closed)** — The intra-session scan result that never
   reaches the session report. The rehearsal harness can now read the intraday
   scan's nested outcome from its own report instead of only the top-level status.
   At the time, two limitations were recorded and not fixed by design: the path is
   unit-tested but no current production replay actually contains an intraday_scan
   key (still true — none exist in live history), and a crashed scan produced no
   marker — the session read healthy and the operator could not see the crash.
   **Correction 2026-08-30 (PR #163): the crash gap is now closed** — a crashed
   scan attaches its own status and the session reports it as a failure.

**Next, in order**

1. **Phase 9 — the research desk deliberates.** Every seat may nominate a
   candidate; Technical becomes a responder rather than the gatekeeper on
   candidacy; material disagreements must be adjudicated, not just logged;
   conviction follows multi-source agreement. Full design in
   `docs/QAMC_REMEDIATION_SPEC.md` Phase 9. Depended on Phase 2b — now
   committed (`75c0233`, `feat/pm-flex-routing`) — because "agreement earns
   size" is meaningless until size is expressed as risk.

3. **Earnings filing extraction fix — PR #115 (`009ab78`, branch
   `feat/shorts-visible`, misnamed — it carries the earnings fix, not
   shorting), merged into `main`.** Deploy status is not tracked here — check
   `sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1` against
   `git log origin/main` to see whether it has shipped yet. Corrects the diagnosis
   previously recorded here, which was wrong: the class is
   `EarningsDataProvider` (`src/data/earnings.py`), not `EarningsProvider`,
   and it was never doing a naive first-30,000-characters slice — structured
   section extraction and a density-seeking fallback both already existed.
   The real defect: `_extract_key_sections` matches the phrase "financial
   statements", which also appears verbatim inside the auditor's opinion
   letter ("...the related notes (collectively referred to as the financial
   statements)"). The acceptance test measured only LENGTH (≥3,000 chars), so
   that prose comfortably cleared the bar and suppressed the density-seeking
   fallback that would have found the real tables. Measured over the 68
   filings cached on the production box: 17 reached the earnings analyst
   starved (<40 financial figures), 12 of those with ZERO — MSFT, AAPL,
   GOOGL, BAC, CVX, NFLX among them. Fix: require ≥40 financial figures
   (dollar amounts, comma-grouped thousands, parenthesized negatives — the
   same pattern `_find_financial_dense_region` already scored by, now a
   shared module constant) in addition to length; failing the content check
   falls through to the fallback instead of returning. Re-measured across all
   68: 17 improved, 51 unchanged, 0 regressed, 0 starved.

4. **Insider routine/opportunistic filter — PR #133 opened against `main`, not yet
   merged/deployed.** See the "Landed" entry above (`feat/insider-signal-filter`)
   for what it does and the measured routine split with its caveat.

6. **Phase 4.2 — repair the data feeds.** News-feed half **DONE** (branch
   `fix/news-feeds-and-coverage`, 2026-08-28): Reuters/AP investigated live —
   neither is fixable for free (Reuters retired public RSS in 2020; AP's own
   feed requires a paid OAuth2 API, and the free third-party proxy is
   Cloudflare-walled) — both removed from `RSS_FEEDS`, Yahoo Finance News
   added as a partial free substitute, and `NewsCoverage` (`src/data/news.py`)
   now makes a dead feed impossible to miss: it's in the analyst's own prompt
   and in `data_status["news"]` (`ok`/`partial`/`failed`), which is what
   `trader_feed.py`/`notifier.py` already render as the operator-facing
   `⚠️ Data degraded` banner. **FRED half also DONE (2026-08-30, PR #162)** —
   see the "Landed (2026-08-30, later)" entry above; this line was left
   "still open" for two days after that stopped being true.
   (4.1, un-blindfolding the intraday buy path, is done — `fb88e08`,
   `feat/pm-flex-routing`, see the landed section above.)

7. **Phase 5 — short selling, now a three-stage plan.** The prior estimate
   recorded here — "bounded and additive, roughly a day, NOT a rewrite" — was
   **wrong**. A survey for Stage 1 found roughly 50 long-only assumptions
   across the money path, several failing silently: the constructor would
   re-open a held short every session (a short's weight was absent from
   `_current_weights`, so `.get(sym, 0.0)` read an already-held short as
   unheld); short orders bypass the risk engine entirely via an early
   `return []` on SELL; and shorts counted as zero portfolio risk in
   `portfolio_heat` (`qty <= 0` was excluded). Discovery still ALREADY WORKS —
   `TechAnalysisResult.rating` emits `sell` / `strong_sell` today. **Inverse
   ETFs are explicitly NOT the answer** — the owner rejected that workaround;
   he wants real short selling. Split into:

   1. **Make shorts countable — PR #116 (`feat/shorts-countable`, two commits
      `71325b1` + `a81bfde`), merged into `main`.** Deploy status is not
      tracked here — check `sudo -n -u qamc git -C /home/qamc/quant-agent log
      --oneline -1` against `git log origin/main`. Signed weights
      in `_current_weights`, side-aware `r_multiple`/`position_risk`/
      `portfolio_heat` in `src/risk/metrics.py`, and `qty != 0` (not `qty >
      0`) in every reporting filter (`src/storage/db.py`,
      `src/notifier.py`, `src/trader_feed.py`, `src/pipeline.py`). No order
      path is touched — the constructor emits nothing for a held short
      (`current_pct < 0`) rather than routing a cover or an add-to-short
      through paths that don't yet handle direction. 40 tests added
      (`tests/test_shorts_countable.py`), 21 of which failed pre-merge on
      `main` without this fix; the rest are a no-op wall proving long
      arithmetic is unperturbed.

   2. **Make shorts safe — landed 2026-08-28 (commit `10e0f10`, "Stage 2 of
      short selling: make shorts safe"; `tests/test_shorts_safe.py`, 28
      tests), merged to `main`.** Correction 2026-08-29: this line
      previously read "(not yet started)" — that was wrong as of today's
      check against origin/main; stage 2 is done, stage 3 below is what
      remains. Risk-engine routing so a SELL on an unheld symbol doesn't
      skip the deterministic gate via the early `return []`; stop direction
      (above entry) and trailing direction inverted for shorts; unbounded-loss
      margin accounting.

   3. **Turn it on (not yet started).** Order placement in the broker layer,
      then retire the inverse ETFs (`SH`, `SDS`, `PSQ`, `SQQQ`) as the
      bearish-expression mechanism. **Correction 2026-08-29: the first half
      is done — stage 3 (PR #150) landed and is merged and deployed, so
      shorts can be opened and covered.** The inverse-ETF retirement is
      still outstanding: `SH`/`SDS`/`PSQ`/`SQQQ` remain in the trading
      universe, now redundant rather than necessary.
   Alpaca is ready: `shorting_enabled: true`, `no_shorting: false`,
   `max_margin_multiplier: 4`, assets `shortable` with `borrow_status:
   easy_to_borrow`. The Alpaca paper account was verified on 2026-08-28 as
   already margin-enabled (`shorting_enabled: True`, `multiplier: 4`, equity
   $9,871.87) — no owner action is outstanding. (`docs/QAMC_REMEDIATION_SPEC.md`
   Phase 5 previously recorded an owner action to switch the account to
   margin; that is stale and has been corrected there.)

   **Known residual, needs a schema migration.** `pending_protection_restores`
   WAL rows predate shorts and carry no side column, so a row for a short's
   cancelled BUY stops looks byte-identical to one for a long's cancelled
   SELL stops. `_derive_close_side_for_drain` (`src/pipeline.py`) reads the
   broker's live signed position to tell them apart, but when that read
   itself fails, `_drain_pending_protection_restores` deliberately degrades
   to the pre-existing `sell` default rather than stalling the row —
   verified in the code and comments as of 2026-08-29. Unreachable today
   because shorts cannot yet be opened; becomes reachable, and wrong, the
   day they can. Close it as part of stage 3, not after. Current behaviour
   is pinned by `tests/test_shorts_emergency_close.py`'s crash-recovery
   drain-path coverage (added `e9851ea`, flagged by PR #135's own coverage
   audit as the one corner with zero tests) — read it before changing it.
   **Correction 2026-08-29: closed as part of stage 3, as planned.**
   `pending_protection_restores` now persists a `side` column, written at
   row-creation time by whoever is closing the position; the drain path
   prefers that persisted value and only falls back to the live-broker
   derivation (never a blind `sell` default) for a legacy row written before
   the migration. See `tests/test_wal_protection_side.py`.

8. **Phase 6 — cost circuit and transparency.** Dollar-based cap with an
   afternoon reserve; `position_id` linking a buy to the sell that closed it;
   surface the reasoning already stored but never displayed. **Correction
   2026-08-29: done** — see "Landed (2026-08-29)" at the top of this backlog.

#### THE REHEARSAL HARNESS — built, acceptance test PASSING (corrected 2026-08-29)
Merged to `main` as PR #122 (`feat/session-rehearsal`). Runs a full session offline against a snapshot of production, replaying recorded model responses. Free, deterministic, about 50 seconds. Blocks outbound network at the process level and proves the production database is byte-identical afterwards. Operator alerts are suppressed via `QAMC_REHEARSAL=1`.

**This section previously said the acceptance test did not pass. That was stale by the time it was read on 2026-08-29** — the fix landed the same day it was written, inside the same PR (`ee6f671`, "un-merge chunked agent rows so replay stops running dry"), and nothing after that commit ever came back to correct this text. Verified again on 2026-08-29 by re-running both acceptance tests from a fresh worktree against the live production snapshot: `pytest tests/test_rehearsal_replay.py tests/test_rehearsal_reproduces_cost_ceiling.py` — **11 passed**.

What was wrong and the fix: `tech_analyst.analyze_batch` auto-chunks a large symbol batch into several real provider calls (3 chunks + 1 missing-symbol recovery for the 2026-08-28 incident run, `agent_logs.provider_requests = 4`), then merges them into ONE `agent_logs` row before logging. Replay patches the provider transport, invoked once per real call, so it needed 4 recorded answers for that row and found 1 — the first chunk consumed it, every later chunk raised `MissingRecordedResponse`, and the resulting failure cascade masked the actual incident behind an unrelated `failed_call_unknown_cost` trip on `tech_analyst`. Fix (`ops/rehearsal/replay.py::_unmerge_chunked_call`): `analyze_batch` already joins each real call's text behind `"--- chunk i/N ---"` / `"--- missing-symbol recovery ---"` markers, in call order, in both `input_message` and `full_response` — a complete ordered record of the real calls a merged row represents. Replay now splits one row back into one `RecordedCall` per real call before matching, prorating merged-only token/cost figures by each part's share of the row text (last part takes the remainder, so parts always sum to exactly the recorded total).

With the chunk defect fixed, the harness reproduces the 2026-08-28 incident timeline exactly (`llm_circuit_events` on the live box: 09:32 defect 1 — projected session cost, `portfolio_manager`, session $0.0461 / day $0.0476; 11:15 operator reset; 11:30 defect 4 — paid-session count cap, `tech_analyst`, day $0.1765). `test_the_pre_fix_estimator_still_reproduces_the_2026_08_28_block` forces the old byte-as-a-token estimator back on through config (demanding more measured history than the ledger holds) and confirms the reserved-exposure ceiling still blocks the Portfolio Manager exactly as it did that morning, zero trades proposed or executed. `test_rehearsal_reproduces_2026_08_28_pm_cost_ceiling_failure` runs the same incident under the four cost-circuit fixes (PR #126, merged same day) and confirms the ceiling no longer fires and the Portfolio Manager is reached — the correct post-fix outcome. Both tests require `sudo -n -u qamc` read access to the production database and skip cleanly where that access is unavailable.

**Two more rig-only defects found 2026-08-29 by actually running the harness (not just its acceptance test) across `morning`/`midday`/`close`/`evening`/`intra_check` against the live snapshot, both fixed in the same pass:**

1. **`ResponseLibrary.match()` could crash on an ordinary, unpinned rehearsal.** Reproduced live: a plain `morning` rehearsal (no incident pinning, matching against full history) hit `TypeError: '<' not supported between instances of 'RecordedCall' and 'RecordedCall'` inside `scored.sort(reverse=True)`. Cause: `_unmerge_chunked_call` gives every part of one merged `agent_logs` row the same `row_id`, so two un-merged parts of one chunked row tie exactly on `(score, -row_id)` whenever they also tie on Jaccard score (trivially true when neither shares a word with the live prompt), forcing Python to compare the un-orderable `RecordedCall` objects to break the tie. In production this cascaded: tech_analyst's retry logic caught it as a call failure, exhausted retries, failed over to a second provider, hit the identical crash on the identical tied candidates, burned through the cost circuit's `provider_attempt_limit`, and suspended paid analysis for the rest of the session — a rig-only bug that looked exactly like a production incident. Fixed in `ops/rehearsal/replay.py` by ranking candidates by index instead of by object; `tests/test_rehearsal_replay.py::test_match_does_not_crash_when_two_unmerged_parts_of_one_row_tie` pins it.
2. **The verdict didn't know its own pipeline's status vocabulary.** `midday`, `close` and `intra_check` rehearsals that ran perfectly normally — no crash, no missing recording, no blocked agent — all came back `VERDICT: FAIL`, because `_verdict`'s healthy-status set only recognized `executed`/`no_orders`/`no_trades`/`market_holiday`. `run_position_review` (shared by midday/close) returns `"reviewed"` on a normal completion, `run_intra_check` returns `"ok"` when there is no loss violation (the common case on a 30-minute cadence), and `run_evening` returns `"analyzed"`. Production's own `src/trader_feed.py` and `src/notifier.py` already group these with the statuses the rig did recognize as healthy — the rig disagreeing with production about what counts as "this worked" is exactly the dishonest-output failure mode this harness exists to catch in the trading system, reproduced in the harness itself. Fixed in `ops/rehearsal/report.py`; `tests/test_rehearsal_report_verdict.py` pins it (new file, 6 tests).

Full suite: 2892 tests passed before this pass; +7 net new (1 in `test_rehearsal_replay.py`, 6 in new `tests/test_rehearsal_report_verdict.py`). Now lives at `ops/rehearsal/` on `origin/main`, not on a standalone branch/worktree; see "Session start" above for the owner's 2026-08-29 instruction to run it routinely.

**Hardening pass 2026-08-29 (second): verified the five just-added healthy statuses against `src/pipeline.py` directly rather than trusting the comments above, and audited every `run_*` session function for other gaps.** Found and fixed three more:

1. Three genuine *failure* statuses — `position_review_parse_error` (`run_position_review`/midday+close), `evening_analysis_error` and `evening_parse_error` (`run_evening`) — were already asserted as FAIL by this pass's own tests but had no `STATUS_PLAIN` entry at all, so each would have printed the generic "ended with status 'X'" fallback instead of a real explanation. Added.
2. `early_close` (`run_position_review`/midday+close, `src/pipeline.py:7806`) — a deliberate skip on half-day-holiday sessions, the same shape as `market_holiday` — was missing from both `STATUS_PLAIN` and the healthy set. Added to both.
3. `run_morning`'s PM-failure family — `pm_parse_error`, `pm_schema_error`, `pm_grounding_error`, `pm_repair_changed_decision` (`src/agents/portfolio_manager.py`, surfaced via `ctx.analysis_failure_status`) — were real, reachable statuses with no `STATUS_PLAIN` entry. Production's own `src/notifier.py`/`src/trader_feed.py` already match on `status.startswith("pm_")` as a PM-decision failure; the rig's vocabulary had not caught up. Added as failures (not healthy).

Also added `run_earnings_preprocess`'s statuses (`fetch_error`, `nothing_new`, `analysis_error`, `preprocessed`) pre-emptively — that session is real and scheduled but the rig still cannot invoke it (unchanged, separate gap, see below) — so the vocabulary is already correct on the day that gap closes.

One nuance worth recording: `intraday_no_trades`/`intraday_executed` (from the first hardening pass above) are correct in meaning but were found to be currently **unreachable** as `report.status` — they only ever appear nested at `result["intraday_scan"]["status"]` (`src/pipeline.py:8497-8498`), which `ops/rehearsal/report.py`'s `collect()` never reads; production's own `src/trader_feed.py` reads that nesting explicitly (`nested = result.get("intraday_scan")`, line 54) rather than trusting `result["status"]` for intra_check. Left in `STATUS_PLAIN`/the healthy set (harmless, correct-if-ever-reached) but the rig having no visibility into the intraday scan's own outcome is a real, separate gap — reported, not fixed here.

New guard test `tests/test_rehearsal_report_verdict.py::test_every_known_pipeline_terminal_status_is_classified` pins the full status vocabulary against a hardcoded, file:line-cited list (dynamic AST discovery was tried and rejected — the PM-failure family lives on `AgentResult.semantic_status`, set in a different file, not a string literal at the `"status"` key's return site, so a literal-string walk would silently miss exactly the drift this test exists to catch) — a future undocumented pipeline status now fails CI instead of printing raw.

Full suite: 2900 passed (2899 after the first hardening pass + this test).

#### BRANCHES READY, NO PR YET

- `feat/insider-signal-filter` — merged as PR #133, no longer pending
- `fix/news-feeds-and-coverage` — merged as PR #132

- `fix/dollar-based-session-cap` — its first commit, `766a35d`, added
  `afternoon_reserve_pct` (40) and `afternoon_reserve_release_et_hour` (12)
  plus a `_morning_spend_ceiling()` helper that was defined and never
  called. **Correction, 2026-08-29: superseded, not still open.**
  `_morning_spend_ceiling()` is called from `begin_call` in current `main`
  (`src/cost_circuit.py`), with dedicated passing tests
  (`tests/test_cost_circuit.py::test_morning_spend_ceiling_pure_computation`,
  `test_afternoon_reserve_blocks_morning_spend_above_the_ceiling`,
  `test_afternoon_reserve_recovers_the_same_day_without_a_rollover`) —
  landed via PR #126 (`fix/cost-circuit-four`) and PR #131
  (`fix/pricing-staleness`), both already merged. See "STILL OPEN —
  2026-08-29" item 5 below.

- `feat/bounded-repeg` — PR #144 opened 2026-08-29. Agent decision: merge it
  rather than leave it to rot, shipping the re-peg disabled by default. Check
  `gh pr list` for current status before treating this as landed.

- `MarketDataProvider.get_next_earnings_date()` is implemented but **unwired**;
  the Tech Analyst accepts a `days_to_earnings` kwarg that nothing supplies.
  **Correction: no longer true.** A real caller now exists
  (`src/data/event_calendar.py`, submitted through a bounded
  `ThreadPoolExecutor`), landed as part of closing defect (c) in
  `docs/phases.yaml`'s `open_defects` entry.

- Nothing tells the Portfolio Manager that `SH`, `SDS`, `PSQ` and `SQQQ` are
  bearish instruments, so even the sanctioned bearish expression is unwired.
  **Correction: no longer true.** `config/prompts/portfolio_manager.md` now
  carries a dedicated "Inverse ETFs are bearish, not a hedge-flavoured long"
  section (PR #158; also confirmed in `config/prompts/risk_manager.md`'s
  "Short discipline" section).

- 26 unmerged branches await triage, including two abandoned VPS security
  branches (`claude/vps-security-hardening-t8m3qz`,
  `claude/vps-deployment-hardening-q3f7k2`) worth rescuing before deletion.

---

## Moved out of docs/WORK.md, 2026-09-02 — finished 2026-09-01/2026-08-27 records

WORK.md is capped at 100,000 bytes and had grown past it. These sections describe work that shipped, merged or deployed and were already superseded by the state block at the top of WORK.md. They are moved here verbatim, not trimmed, because this log is append-only.

### The 2026-09-01 handoff — branches, Phase 11 merge record, telegram deep link

**START HERE — 2026-09-01 handoff. Everything below is a POINTER; the detail
lives in the files named and is not repeated.**

**Read first, in this order:**
1. `docs/QAMC_REMEDIATION_SPEC.md` **Phase 12** — four decisions Rex ratified
   2026-09-01. Nothing is implemented. This is the work.
2. Then **Phase 10** (per-trade risk verdict, macro sizes rather than selects,
   concentration scales, target from levels) and **Phase 11** (fractional
   sizing, 2.0x margin).
3. `docs/OUTCOME.md` — "This is a trading desk, not a retirement portfolio".
   Read before touching any risk rule; it decides which rules are legitimate.
4. `docs/INCIDENT_HISTORY.md` — what already broke and was fixed. Append-only.

**Why it matters:** on 2026-09-01 the desk reviewed 38 qualified signals and
placed zero trades. Root cause is Phase 12.1. It is still unfixed.

**Owner instruction: ship everything in ONE pass, tonight.** Deliberate
acceptance of change risk (Phase 12.4) — the desk cannot trade at all, so a
partial fix leaves it that way. **The rehearsal rig is the mitigation and must
run against the merged result before deploy.**

**Six branches, all pushed, none merged, none deployed:**

| branch | spec | tests |
|---|---|---|
| `fix/risk-verdict-per-trade` | 10.1 | 3853 pass |
| `fix/concentration-scales-size` | 10.3 | 3848 pass |
| `fix/target-from-structure` | 10.4 | 3850 pass |
| `feat/golden-pm-prompt` | PM prompt rewrite + Phases 10/11/12 in the spec | 3848 pass, 1 unrelated |
| `feat/telegram-run-deeplink` | symbol links + company names in alerts | full suite green |
| `rescue/price-provenance` | rescued 11-day-old work — **parked, NOT mergeable**, reference only |

**Merge hazard, read before merging anything:** spec Phases 10/11/12 exist ONLY
on `feat/golden-pm-prompt`. The three fix agents could not see them and each
wrote its own reconstructed Phase 10 into the spec. **Merge
`feat/golden-pm-prompt` FIRST, then reconcile the others' spec sections against
it — the owner-ratified text is the one on that branch, not the
reconstructions.**

**The model-benchmark results are STALE — do not choose a PM model from them.**
Every score in `ops/model_policy/results/*2026-09-01*.json` was measured against
the OLD prompt, which is what produced the restrictive behaviour, so the scores
are entangled with it. **The rig does not need rewriting, only re-running** —
`benchmark_models.py` drives the real agent class, which reads
`config/prompts/portfolio_manager.md` from disk, so re-running after the
rewritten prompt lands tests the new prompt automatically. Keep the DIAGNOSIS
(gpt-5.5 picked SPY in 5 of 5 runs and never proposed more than 2 positions —
the most literal rule-follower, hence the most timid under a prompt full of
"never"); discard the RANKING. Expect absolute scores to rise across the board
if the rewrite works, so compare rankings, not numbers. **Also re-examine the
`actionable_book` check itself** — if the new prompt legitimately produces more
targets, that check may now be too easy to pass and stop discriminating.

**Universe expansion and pruning — DESIGN AGREED WITH THE OWNER 2026-09-01,
never written down until now. Not built. Do not redesign it; implement this.**

Today the 101-symbol universe is a hand-written list. Symbols CAN be added
dynamically but only narrowly: up to 3/run via SEC Form 4 smart-money
admission, and up to 3 per seat / 6 total per run via Phase 9 nominations.
**Nothing ever removes a symbol.**

*Admission screen.* The criteria already exist under `smart_money:` but are
used only to admit outsiders, never to BUILD the list. Reuse them, with these
corrections:
- **A year of price history minimum.** `min_external_history_days: 20` is
  badly wrong — the analysis computes a 200-day moving average and its slope,
  so a symbol admitted at 20 days produces blanks in the fields the analyst
  leans on hardest.
- **Screen on bid-ask spread, not the $10m dollar-volume floor.** $10m/day is
  far stricter than a ~$10k account needs, and it screens the wrong thing:
  what costs money is the spread.
- **Require shortable / easy-to-borrow.** Half the point is bearish trades.
- **Exclude warrants, units and rights.** Delisted warrants already reached
  the data layer once and caused a recursion fault.
- **Exclude names under a pending takeover** — an acquisition target trades
  flat at the deal price, so every technical signal becomes noise while
  looking like a calm uptrend.
- **Volatility ceiling.** A name so wild that its structural stop is enormous
  fails reward:risk by construction — screen it at the door rather than
  rejecting it daily.
- **Minimum company size**, so a micro-cap cannot qualify on one freak volume
  day.
- **NO earnings-date requirement.** Considered and REJECTED by the owner: ETFs
  have no earnings and ~20 of the universe are ETFs. Reducing their size for a
  missing date is equally wrong. Dropped entirely.
- **No maximum price** — Phase 11.1 turned fractional sizing on (built
  2026-09-01), so a $500 share is no longer unsizeable.

*Pruning.* Nothing does this today.
- **Re-screen weekly.** Fail once -> flagged. Fail twice consecutively ->
  removed. One bad week must not evict a good name.
- **NEVER remove a symbol currently held.** That cuts a live position off from
  analysis while its stop still sits at the broker — unwatched but real.
- **Immediate removal, no second chance,** when the broker reports the asset
  does not exist, or it is delisted or permanently halted.
- **Log every addition and removal with its reason, and summarise them in the
  morning alert.** The owner must never discover the universe changed by
  accident.

*Cost.* Scanning is free — the specialist seats run on the free Gemini tier
(measured 2026-09-01: `tech_analyst` processed 307,754 input tokens at $0.00).
The cost is the PM reading a longer candidate list. **Cap how many screened
candidates reach the PM**, the same way `NominationConfig` already caps
nominations, so the bill is a number that is set rather than one that emerges.
Untested risk: the free tier is rate-limited and a much wider scan may hit it.

**Phase 11 status, 2026-09-01, after verification and merge — this replaces
both earlier claims, which had each gone stale in a different direction.**

**MERGED and verified on `integration/ship-2026-09-01`:**
- **Margin interest tracker.** Measures only, never gates. Review found and
  fixed a real defect: it fast-exited whenever `allow_margin` was false and so
  never read cash at all — unsafe, because covering a losing short is exempt
  from the cash-only block and can carry a genuine debit balance.
- **The sector cap no longer switches itself off.** Pre-existing rot,
  reproduced against pristine `af266de`: a holding at 85% of equity plus a 10%
  order, both in an unresolved sector, produced ZERO violations — 95% pooled
  straight past the 90% ceiling. 80 of 101 universe symbols depend on a live
  network lookup with no offline fallback, so the cap was inert for most of
  what the desk trades. Verified NOT over-broad: cash-park never reaches the
  code, index and sector ETFs resolve from static tables offline.
- **Phase 11.2's ceiling and ladder.** 2.0x gross cap at the sizing and
  execution gates; the ladder stepping 2.0/1.5/1.0/0.5 on peak-to-trough
  drawdown. Both owner gates verified BY MUTATION, not by reading the tests:
  breaking the ladder so it never steps fails 24 of 55 tests, making it trim
  to make room fails 6, making the rung compound fails 2. The ladder runs in
  the session preamble BEFORE any agent, from account state alone, so a blank
  PM response cannot skip it — which is not hypothetical, a benchmark run that
  night had one candidate model return an empty book on 1 run in 5.

- **Phase 11.1, fractional sizing and its three stop guards — MERGED.** — fractional
sizing and its three stop guards. No path leaves a fractional position
silently unprotected; the owner alert fires unconditionally after every
protection attempt. Review found and fixed a real gap: the 30-minute sweep's
repair belt used a bare single-shot stop submit with no retry and no
whole-share fallback — the same weakness the entry guard exists to close.

**`allow_margin` REMAINS `false`.** The ceiling was built before borrowing is
enabled; that was the sequencing requirement and it held. The flag and the PM
prompt's exposure table move together, later, after the rehearsal rig has run
against the merged result.

**Still to build:** the wider universe with pruning (design recorded below, no
code), and Phase 10.2 deterministic analyst weighting.

**Open, documented, not blocking:** the live rung is not on Mission Control.
`src/api/` may never import `src.risk` (ratified guardrail,
`tests/test_api_safety.py`), so the dashboard shows the standing cap only; the
session alert is the sole operator surface until the measurement functions
move out of the risk package. Also worth the owner's eye: the ladder
introduces a SECOND drawdown measure (peak-to-trough against a 252-day
high-water mark) alongside the existing rolling-window one. One table, one
resolver, so the mechanism is not duplicated — but it adds a measure rather
than reusing the existing one.

**Phase 11 was MISSING from this line until 2026-09-01 and that caused a real
scope error.** A session read this list, built Phase 12 and the branch merges,
and correctly believed Phase 11 was out of scope. Verified by search across
every branch: no fractional sizing anywhere, and **no gross-exposure cap of any
kind exists** — `max_portfolio_risk_pct` bounds AT-RISK capital, not gross. So
11.2 ADDS a ceiling where none exists; it is a tightening, not a loosening, and
it must land before `allow_margin` is turned on.

**Phase 12.1/12.2/12.3 are now IMPLEMENTED** (2026-09-01) along with the five
open branches. Phase 10.2 — computing analyst weighting in Python so no seat
dominates by prompt position — is also still unbuilt.

**Baseline:** `pytest tests/ -q` gives 2 pre-existing failures in
`tests/test_rehearsal_reproduces_cost_ceiling.py` — they read live production
state and pass in CI. Anything else failing is yours.


**READY TO DEPLOY, WAITING ONLY ON THE MARKET CLOSING — do this first.**
**MERGED 2026-09-01 into `integration/ship-2026-09-01`** — verified with
`git merge-base --is-ancestor`. This entry previously said "not merged",
which was wrong and was repeated onward before being checked. **What is
still owed is the DEPLOY, not the merge.**

Branch `feat/telegram-run-deeplink`. Makes
every ticker in a Telegram alert tappable through to that company's quote
page. Full suite green: 3837 passed, 1 skipped, and only the two known
`test_rehearsal_reproduces_cost_ceiling.py` failures that read live
production state.

It was NOT deployed on 2026-09-01 for two specific reasons, neither of
which is "later, vaguely":
1. The market was open (12:44 ET) with the midday session 16 minutes out.
   Deploying restarts the trading service; mid-session is the wrong moment.
2. It touches `main.py` and `src/scheduler.py`, not only message text — so
   the rehearsal-rig rule above applies and the rig has not been run
   against it yet.

**Superseded 2026-09-01 22:00 UTC:** the merge is done, so the remaining
order is: run the rehearsal rig against the merged integration branch, wait
for CI, deploy, restart, confirm the served bundle matches disk.

**Timing constraint, measured from the live timers rather than assumed:** the
production evening session fires at 23:30 UTC and several timers fire with
it; the morning run is 13:00 UTC, half an hour before the open. Deploying
restarts the service, so the window is AFTER the evening session completes
and BEFORE 13:00 UTC.

Rex asked directly: "who's gonna remember to deploy it? I'm not gonna
remember." This entry is the answer. It stays here until it is deployed,
and whoever picks this file up next is the one who owes him the deploy.

**Known and deliberately NOT fixed on that branch** (do not let it block
the merge): the alerts still link to the Mission Control home page rather
than to the specific run. The cockpit has no URL routing whatsoever — no
router package, no query or hash parsing anywhere in `frontend/src`, and
`selectedRunId` is in-memory `useState` only. Deep-linking needs ~15 lines
in `App.tsx` to read `?run=<id>` on mount for same-day runs, and more than
that for older ones, because there is no UI to view a non-current run at
all. Separately: `_append_company_identities` in `src/notifier.py` is dead
code for real trading alerts — `src/pipeline.py` emits `executed`/
`no_trades`, which route to `trader_feed.py`'s own formatters, and those
never call `CompanyProfileStore`. That is why company names have never
appeared in an alert.

### The 2026-08-27 evening deploy

**Historical — the 2026-08-27 evening deploy.** As of that evening,
production was deployed at `46b2029` (merge of PR #113,
`feat/pm-flex-routing`), superseding `32c174b` (PR #114, the deploy-drift
alarm, merged on top of `e6ada88` — PR #113 carries `32c174b` in its own
merge history). Phase 3 (exit rework), the execution limit fix, the
deploy-drift alarm, Phase 2b risk-based sizing, the stop-width fix, the
OpenRouter flex-routing change and the intraday un-blindfolding were all live
as of that deploy: seven positions open, all with broker-resident stops,
`paper: true`, daily LLM budget raised to $2.75. (Earlier same-day notes had
claimed `18dd4bc`, then `e6ada88`, as the deploy SHA; `18dd4bc` was never
actually on the box, and `e6ada88` was superseded within the same session —
recorded here only for the forensic trail, not because it matters now.)
**None of this paragraph describes current state** — use the command above.

**Historical — 2026-08-27 night, nothing further deployed, deliberately.**
The sizing and stop-width change (`3dff940`, part of the deploy above) had
its first live session 2026-08-28 09:30 ET, and the operator chose not to
confound that read with another deploy that night. PR #115 (earnings fix)
and PR #116 (shorts Stage 1) were both open, reviewed, and intentionally left
undeployed that night. **Whether they are deployed now is a different
question — check reality, above, and see the ordered backlog below.**

**Historical — deploy-drift alarm (PR #114, `9eef617` + `38a985c`), landed in
the 2026-08-27 deploy.** `scripts/check_deploy_drift.py` plus
`quant-agent-drift-check.timer` (Mon-Fri 08:45 ET) alerts over Telegram when
the box's deployed HEAD falls behind `origin/main`. Built because PR #111 sat
merged-but-undeployed for eight hours with nothing catching it. Verified
firing.

**Historical — also in the 2026-08-27 deploy, PR #113 (`feat/pm-flex-routing`,
merged as `46b2029`):**

- `75c0233` Phase 2b risk-based sizing + the correlation-aware risk budget
  gate — **the highest-consequence change in this deploy.** It decides how
  much money each trade may lose. `b712f4c` and `3dff940` land on top of it,
  same branch: the constructor now clamps to the risk engine's 20%
  single-name ceiling instead of proposing orders it hard-blocks, and entry
  stops sitting inside ordinary volatility get pushed out to a
  regime-and-setup-scaled ATR floor (`risk.min_stop_atr_multiple`) — a
  widened stop that drops reward:risk below 1.5 rejects the trade outright.
  Measured against the real book: MSFT's stop went 2.4% → 7.0%, VLO 4.5% →
  9.2%, OKLO 7.7% → 24.7%, and 0.5/1.0/1.5% conviction now produces
  7.1/14.2/20.0% positions instead of clamping all three to 20%. First live
  session under this change is 2026-08-28 09:30 ET.
- `fb88e08` the intraday PM un-blindfolding.
- `16f6535` the PM's OpenRouter `openai/flex` endpoint routing.
- `6b7af86` the Mission Control `input_message` surface, `cdb387b` the
  sector-stance vocabulary + `TypeError` crash fix, `002095c` risk-sized
  targets reappearing in the cockpit funnel, `300ea14` + `6f897a1` + `55f0e05`
  the benchmark-harness repair and its guards, and the `docs:` commits.
