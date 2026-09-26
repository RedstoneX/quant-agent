# Board notes — owner-facing prose for the desk status board

**This file is NOT the source of truth.** `docs/WORK.md` is: it alone
records what this desk is doing — an item's number, its title, its status,
its ordering. `docs/BOARD_NOTES.md` supplies nothing but the words a trader
reads about each item: a plain-language explanation, a worked example, and
where a ruling is needed, the decision plus a recommendation. If this file
disagrees with `docs/WORK.md` about whether something is open, done, or even
still exists, `docs/WORK.md` wins, always.

**Who writes this file.** The orchestrating assistant, drafting explanation
for the owner in plain English — never the owner, and never a mechanical
process. Nothing here is generated, inferred, or summarised out of
engineering notes. An item with no entry below renders on the board with an
explicit "not yet explained in plain language" marker — that is the honest
state, and it is never papered over with invented prose.

**No size cap.** `docs/WORK.md` is mechanically capped at 100,000 bytes on
purpose (see
`tests/test_status_board.py::test_work_md_stays_under_a_hundred_thousand_bytes`)
because it must stay a short, current, agent-facing backlog — not an
append-only prose archive. This file carries none of that risk. It can grow
without threatening the backlog's cap, so it has no size limit of its own.

`scripts/status_board.py` reads this file for prose and `docs/WORK.md` for
everything else, every time the board is built. See that script's module
docstring ("The prose problem, and the convention that solves it") for the
full mechanical account.

## How an entry is keyed

Each block below is introduced by a heading naming the exact item it
explains, by its NUMBER and SECTION — never by its title. A title can be
reworded in `docs/WORK.md` at any time; a prose block keyed to words would
silently go stale, or orphaned, the moment that happened. Keying on the
number and section survives a rename intact.

  * `## item N` — a funnel-queue item, N being its rank under
    `## THE FUNNEL QUEUE` in `docs/WORK.md`.
  * `## gate item N` — a PM-test-gate item, N being its rank under
    `## PM TEST GATE` in `docs/WORK.md`. The funnel queue and the gate both
    number from 1, which is why "gate item" and "item" are distinct keys —
    see `_SOURCE_REF_LABEL` in `scripts/status_board.py`.
  * `## decision due YYYY-MM-DD` — a pending `- [ ] DECIDE BY ...` line in
    `docs/WORK.md`, keyed by its due date because a decision carries no
    number of its own.

These are exactly the identifiers the board already shows the owner, and
that he quotes back to refer to something ("item 32", "gate item 4",
"decision due 2026-09-16") — see `QueueItem.ref` and `PendingDecision.ref`
in `scripts/status_board.py`. The heading is matched case-insensitively and
with any amount of whitespace, but the words and the number must be there.

Under each heading, each field on its own line, in the same shape
`scripts/status_board.py`'s `parse_prose` has always read:

    **Plain language —** what this is, in words a trader understands.
    **Example —** a concrete case that makes it tangible.
    **The decision —** what the owner specifically has to rule on.
    **Recommendation —** what we think he should do, stated as a
    recommendation.
    **Why only you —** the one reason this decision cannot be made without
    him: money, mandate, risk appetite, or public disclosure. Never a
    market-structure number — those are researched, not decided, and an
    item like that writes `**The decision —** None for you` instead.

The label must start the line, the surrounding `**` is optional, and the
separator may be an em dash, a hyphen or a colon. A block ends at the next
label, the next heading, or a blank line — one paragraph per label. Nothing
is mandatory: an item can carry only a `Plain language` line and nothing
else, and any field left out renders as absent, never guessed.

**"Waiting on you" on the board is built from two fields together.** An
item shows there only when it carries BOTH a live `The decision` (not
starting "None", "Not yet" or "Possibly") AND a `Why only you`. Writing a
decision without a reason, or a reason without a decision, leaves it where
it already was — in the running order, not promoted. See
`_is_live_owner_ask` in `scripts/status_board.py`.

## Worked example — illustrative only, not a real backlog item

The heading below uses the letter `N` in place of a number so it can never
collide with a real item in `docs/WORK.md`. It exists only to show the
shape; delete nothing above this section when adding real entries below it.

```
## item N

**Plain language —** The desk refuses a trade unless the likely gain is at
least one and a half times what it is risking.
**Example —** You want to buy at $100 with a stop at $98 and a target at
$105: risking $2 to make $5. The desk moves the stop to $95 on its own,
recalculates it as risking $5 to make $5, and refuses the trade.
**The decision —** Should a stop you can point at on the chart always be
honoured, however tight it is?
**Recommendation —** Yes. Pad the stop only when there is no real level to
put it at.
```

---

Real entries for the current backlog are drafted separately and go below
this line, one heading per item.

## decision due 2026-10-31

**Plain language —** The seat that actually decides trades runs on a paid AI model, and it drives almost the whole AI bill. Comparing it against a cheaper model means paying for fresh test runs — and you have ruled that no such test can mean anything while the job board is still dirty, because the comparison feeds on the same data the open items are about.
**Example —** This one seat accounts for roughly 93 cents of every dollar spent on AI. But the test hands each model the same day's evidence and grades what it picks — so if that evidence is coming from seats with open faults against them, a cheaper model can win or lose the comparison on the quality of the input rather than its own judgement, and the answer would be worthless at any price.
**The decision —** Not yet yours to make. It becomes a decision once the model-test gate below is clear; until then the date only exists because this file needs one, and it moves rather than forcing an answer.
**Recommendation —** Nothing to approve. Clear the gate first. A recommendation to spend roughly $5 and settle it was put to you on 2026-09-13 and withdrawn the same day for this reason.

## item 17

**Plain language —** If the desk's own record-keeping breaks, a safety switch can shut down all further AI-based decisions completely, and it stays off until a person manually clears it — working as intended. The real problem, observed live, was that the alert meant to warn someone about it also failed to send, so the desk could sit switched off for a full day or a weekend with nobody aware, looking exactly like an ordinary quiet market.
**Example —** This happened for real: the safety switch tripped because a data file couldn't be opened, and the message meant to warn the owner about it failed to deliver too, so both the shutdown and the warning about it went unnoticed at once.
**The decision —** None for you right now. Already decided, 2026-09-03: not now, bigger problems to solve first. No due date; revisit only at your discretion. This line used to describe it as open — it wasn't kept in sync with your own ruling, corrected 2026-09-13.
**Recommendation —** Nothing to approve right now. Bring it back yourself when you want to revisit it.

**Moved from WORK.md (2026-09-24) —** Recommendation: `docs/BOARD_NOTES.md` ("item 17").

## item 18

**Plain language —** The AI that picks the trades reads a long briefing built from every other seat's work. Two separate things were wrong with it. The first was bulk: it opened with a wall of raw earnings-report text before ever reaching the list of stocks worth buying. That is fixed — the earnings step now hands over a short conclusion and keeps the full detail on file. The second was subtler and is fixed as of today: one section of the briefing told the AI to check the economics seat's reasoning for mistakes in logic, but there was no box anywhere in its answer sheet where it could report finding one. It was being asked to do a job and given nowhere to write the answer.
**Example —** Before the first fix, of about 200,000 characters read each session, 70% was raw earnings text and the buy-worthy list was buried in under half a percent of it. That 70% was re-counted from the real briefing and was exactly right. It is now about a fifth of a briefing that is well under half the original size — re-counted again today, from scratch, before anything was changed, so the figure is this desk's own and not a quote.
**Today (2026-09-14) —** the missing box now exists, and the risk-checking seat sees whether it was filled in or left blank. An honest caveat, in your own terms: this is a change to what the AI is *asked*, and this desk has no way to test whether a change in wording makes it decide better — the replay rig would pass either version. So the case for this rests entirely on the plain fact that the box did not exist, which anyone can check, and not on any measured improvement. Nothing was switched off to get there: the option of simply deleting those paragraphs would have dropped the question instead of answering it.
**Also worth knowing —** the briefing measured 1,000-odd characters longer than the last count, and that is a good sign, not a slip: the extra text is the new record of *why* each rejected idea was rejected, which is the opposite of filler.
**The decision —** Whether two more pieces of evidence, reward-to-risk and net evidence, should be folded into the scoring system used to rank ideas.
**Recommendation —** Hold off until the reward-to-risk fix above is fully re-measured; folding in a number still being corrected risks baking the same distortion into the ranking.

**Moved from WORK.md (2026-09-24) —** (b) Whether reward:risk (today only a within-tier tiebreak) and net evidence (not used at all) join the composite score — not an owner call; per-seat sizing weights stay refused. (c) A spend cap on the OpenRouter key itself, the one unbuilt leg of the cost-circuit replacement, outside our code.

## item 19

**Plain language —** Given the exact same information twice, the AI gives a strikingly consistent answer, which is useful: the desk can use repeat runs to prove a code change actually reached the AI, potentially skip paying for repeats where the answer never varies, and mathematically correct a known, repeatable bias instead of arguing it away with wording changes. All secondary to the bigger prompt fix already underway elsewhere.
**Example —** Five runs with stock names hidden and five with them shown produced answers identical to four decimal places; a later batch of five runs failed the same check four times out of five, always flagging the same two stock names.

**Moved from WORK.md (2026-09-24) —** (a) Use it as a test instrument — any change in its answer proves a pipeline change reached the model. (b) Stop paying for repeats where the answer does not vary; measure first. (c) Subtract the stable famous-name bias arithmetically in the ratified weighted composite (three prompt-wording fixes measured no-change).

## item 20

**Plain language —** Your rule from 2 September: if the research behind a
decision isn't really there, don't decide — skip the round loudly and try
again later. The half of that rule which does NOT need a number is now built
and switched on. Before the expensive decision step runs, the desk checks
each of its five research seats and asks one question: did this seat answer,
or did its answer never arrive? A seat that looked and found nothing — no
insider filings today, no new inflation print published yet — has answered,
and the desk carries on. A seat that was asked and came back with nothing at
all, because the call failed or its reply was unreadable, has not answered,
and the desk refuses to decide, spends nothing, says so, and waits.

**Why there is no number in it —** you said the minimum bar is a risk
judgement and not an agent's to invent, and that stands: nothing published
says how many of five research seats a desk needs before a decision is
sound, and picking one off our own trading history would be fitting a number
to ourselves. So the rule was built to need no bar at all. "Answered" versus
"never answered" is a yes-or-no fact, not a score.

**How hard it bites, measured, not guessed —** replayed against every
morning the desk has actually run: **5 of the 27 that got as far as the
decision step would have been refused — about one in five, on 4 of 13
trading days.** Four of those five are the same single fault: the news
analyst occasionally replies with something that isn't readable at all. That
fault is real and recent (it happened again on 4 September). So expect this
to fire, and expect the news seat to be the reason until that is fixed
separately.

**The thirty-minute scan —** it still does not redo the morning research,
and on a calm day a refused morning is still closer to a lost day than a
lost half-hour. What changed: that scan used to treat "we chose not to
re-read the filings" and "this morning's research never arrived" as the
same thing, so it could decide on a move with a missing morning read. Those
are now different. Not re-reading earnings is an honest skip and the scan
carries on. A morning seat that failed and left nothing to reuse is a
refusal, same rule as the morning session — a made-up decision is worse
than none.

**Today (2026-09-16) —** the risk reviewer was still treating "we reused
this morning's research, we did not pay for it again" as a data problem,
and on that basis refused a whole plan about forty minutes after a
successful morning. That is now stopped. Same-session reuse is usable. A
morning seat that actually never arrived still stops the scan before the
expensive decision step. This is not a ruling on how many usable reads is
enough; that question is still yours.

**Today, later (2026-09-16) —** paying twice for the same unbroken fact is
now the thing being refused, not reuse itself. News is paid for again when
a newer wire actually landed, or when the session ended. Price is always
read live at the moment of the order, never remembered from the morning
tape. The economics call and the filings can be remembered across days
until the regime really changes, or until the next earnings report or a
new insider filing. An empty or unreadable answer is still not research,
and the desk will try a mechanical repair first, then at most one paid
retry, rather than freeze forever on a missing seat. If that retry is
blocked by the spend cap, or still fails, you get a message naming the
seat. A successful repair is written down, not paged.

**Your ruling, 18 September — "only technical analysis can stop the
desk" —** is built. Before it, ALL five seats could stop a decision, and
nobody had decided that: it was an accident of which seat happened to
report a failure word, so any seat that gained a new failure word quietly
gained the power to halt trading. Now the chart research is the one seat
that can stop the desk, and that is written down in one place with your
ruling and its date next to it. The other four still matter: if one of them
loses its answer it is recorded, you are still told, and the desk still
counts the run as degraded — it just goes ahead and decides.

**What shipped alongside it, because otherwise the change would have been
invisible —** with four seats advisory, a decision can now stand on ONE
piece of research read just now plus four answers carried over from the
morning. Every one of those carried answers reports as fine, so nothing
anywhere said "four fifths of this was not looked at again". Now every
decision says so: how many seats were read just now, how many were carried
over, how many had nothing, and which carried answers the desk already
knows are out of date. That is a statement of fact, not a bar — it does not
refuse anything and no minimum was invented.

**One more correction in the same pass —** "I have a good answer and I know
a newer one exists" was being filed as "the answer never arrived". Those
are not the same thing and the rule itself says so. It is now its own
category; it still counts as a degraded run and it is now named to you
explicitly whenever a decision leans on one.

**The decision —** Still yours, and now the only thing left in this item:
whether *partial* evidence should also stop a decision, and if so, where the
line sits. Not "did the seat answer" — that is settled and built, morning
and the later scan alike — but "the seat answered about 40 of 65 companies,
is that enough?". Nothing published answers that, so it either gets a
number from you or a ruling that partial coverage should never stop a
decision at all.

**One thing you may want to look at —** on the every-thirty-minutes scan,
the chart research can never be recorded as having lost its answer; the way
that scan is written, it is always either "fine" or "partly fine". So on
that scan the rule can now record and report, but it can no longer refuse
anything. That follows from your ruling rather than from a fault, and no
agent has widened the rule to work around it.

**Technical detail, moved from `docs/WORK.md` 2026-09-24 —** The mandate is declared in `evidence_gate.BLOCKING_SEATS` and may only be widened by the owner's ruling; every other seat's lost answer is still recorded, logged, and reaches the unsuppressible data-quality alert, but no longer halts. Two things shipped with it: (a) `expired` is no longer classified as LOST — it means the desk holds a good answer and knows a newer one exists, which is neither absence nor a lost answer, and is still counted as degraded; (b) every decision now discloses its own evidence freshness — how many seats were read on this tick versus carried from earlier versus absent — durably in the decision's record and in the owner's message, with no threshold invented. Also open, surfaced by the mandate change and not acted on: on the intraday scan the technical seat's status is hard-coded to `partial`/`ok` (`src/pipeline.py`, the intra `data_status` literal), so the only blocking seat can never be lost there — a consequence of the mandate, not a defect, needing the owner's ruling rather than an agent's second blocking seat.

**Moved from WORK.md (2026-09-24) —** The categorical half is live on morning and the intraday scan since 2026-09-14. **MANDATE CHANGE, owner, 2026-09-18: "Only technical analysis can stop the desk"** — declared in `evidence_gate.BLOCKING_SEATS`, every other seat advisory. **Still open and still his:** the counting half — no published source gives a minimum count of usable reads, and fitting one to the desk's history is forbidden. Settles with his ratified number, or a ruling that partial coverage never gates. Never ship a placeholder. **Also open, surfaced by the mandate change and NOT acted on:** the intraday scan's technical-seat status is hard-coded, so the only blocking seat can never be lost there — needs his ruling, not an agent's second blocking seat.

## item 55

**Plain language —** A "level" is a price the stock has bounced off before, and the desk uses them for almost everything — where to put a stop, whether a trade is worth taking, how big it can be. Three things define one. On 13 September the popular trading-software documentation was read and answered none of them. Later the same day the ACADEMIC work was found, and it changes the picture in three ways. First, it settles one of the three: a level needs at least two bounces, and a study of 733 US stocks over twenty years measured that demanding three or more makes no difference to how often price actually turns there. That number is now sourced rather than assumed, and locked so nobody quietly raises it. Second, it confirms that the desk's whole method — find the bounces, group the ones at similar prices, treat the group as a band — is the same method the academic work uses, so the design is not home-made. Third, on the two numbers still open, it does not give an answer but it does say where the desk is standing: the same study checked band widths from 2% up to 5% and found the results did not change, and the desk's band is 2% — the very tightest they looked at. Nobody has measured anything narrower.

**Example —** On a $200 stock the desk's band is $4 wide. Two bounces $1.90 apart are "the same level"; bounces $2.10 apart are two different levels. That single call decides whether a stop counts as sitting on real structure — and a stop that does gets honoured as-is, while one that does not gets pushed wider, which shrinks the position. So the width is quietly sizing trades, and the desk is running it at the edge of the only range anyone has tested.

**The decision —** None for you. It is a chart-structure question, so it goes to research, not to your judgement. It is on the board so that it gets answered rather than sitting in a code comment forever.

**Recommendation —** There is now a specific, runnable experiment rather than a wish. The academic study's own test — count how often price entering a band leaves the way it came, and compare that against bands drawn at random — has never been run on this desk's own stocks at this desk's own settings. Run it, and sweep the width and the bounce definition across a range. Either the desk's setting shows a real effect, or the effect is flat everywhere, in which case the honest answer is that the width does not matter and this closes. If it is flat, the better prize is still available: drop the percentage entirely and let the band be the actual height of the bars that made the bounces, so the stock states the width and the desk states nothing.

**Moved from WORK.md (2026-09-24) —** Open, both convention: the pivot window is 3 in one module and 5 in another, and the cluster tolerance is a flat 1% (a 2% span). **Every ruled-out source, and why harmonising the windows is not an answer: `docs/INCIDENT_HISTORY.md`, 2026-09-14. Do not re-search.** **Settles with** Tsinaslanidis §4.5's bounce test on the desk's own universe and bars, sweeping tolerance 0.5/1/2/3/5% and window 3/5/10/25 — a reading, not a fit; if flat, prefer a zone equal to the span of the pivot bars, which needs no constant. Cost: 1% decides "the same level", hence whether a stop is level-backed, the ATR floor, R/R and size.

## item 63

**Plain language —** When a company insider sells shares, the desk wants to know whether that's a real opinion about the stock or just someone raising cash. The best measure is how much of their own pile they sold. The research that measures this found something counter-intuitive: an insider selling a *small* slice of what they hold is actually a mildly *good* sign — they need money, they're keeping the rest, they still like the company. Selling more than half is the only case that reliably means bad news. The desk was doing the opposite of reading that correctly: it treated small sales as meaningless and threw them out of the ranking entirely. That's now fixed — nothing is thrown out, and every insider trade arrives at the analyst carrying how big it was relative to what the person held, plus what the research says that size means. What's still missing is narrower: the desk's internal "how much does this matter" score is a single dial from 0 to 1, and a dial cannot say "this matters, and it points the *other* way." So the analyst reads the direction in the notes, but the automatic ranking underneath it doesn't.
**Example —** An executive holding 100,000 shares sells 1,000 of them. Research says that's worth about +0.68% over the next quarter — a small positive. Another sells 80,000 of 100,000; that's worth about −0.81% — a real negative. Today both arrive at the analyst with the same "importance" score of 1.0, distinguishable only by the written note attached. Before this change the first one scored 0.0 and the analyst never saw it at all.
**The decision —** None needed from you right now, and deliberately so. The obvious move — invent a number that scores the bullish case lower or higher — would be exactly the kind of made-up figure this desk refuses. Two sources were checked for a signed scoring scheme and neither has one. This item exists so the gap is on the record rather than quietly papered over, and it gets picked up when either a published source or enough of the desk's own trading history can settle it.

**Moved from WORK.md (2026-09-24) —** Scott & Xu (FAJ 2004): an insider sale under 10% of the holding earns +0.68% adjusted quarterly excess return yet gets weight 1.0, identical to dumping 80% (-0.81%). Ratio and band are already reported so the seat can read the sign; the question is whether the deterministic ranking should too. **Ruled out, with sources: `docs/INCIDENT_HISTORY.md`, 2026-09-13.** Settles with a published signed scoring scheme, or enough own outcome data to read a separation.

**Structure fix shipped (2026-09-25) —** The deterministic ranking now HAS a sign. `SmartMoneyObservation.signal_direction` (derived from `direction`, never stored) returns +1 for a buy, 0 for a sale/exchange/unknown; both ranking keys in `src/agents/smart_money_analyst.py` (`_symbol_rank`, `_transaction_rank`) multiply the `value * signal_weight` term by it. So a bearish sale can no longer tie or outrank a bullish buy of the same dollar value — the exact identity this item names — and a buy's contribution is unchanged (existing behaviour preserved; covered by `tests/test_smart_money.py`). The desk is long-only on smart-money admission (admission requires `direction == "buy"`), so a sale is NEUTRALISED (0), not counted as bullish; the row still reaches the analyst as evidence, so the LLM can still read it bearish. **What stays open:** signing a sale -1 by magnitude (the sourced >50%-of-holdings band is the hook) is the SIGNED SCORING SCHEME still ruled out above — owner appetite or a published source, not a number to guess.

## item 64

**Plain language —** The desk's practice runs against historical data still ask for the same risk on every trade they consider. When the risk ceiling runs out on a busy day, the tie is still broken by ticker spelling. That has not been fixed. What changed is the printout: every practice-run result now says how many of its days the ceiling ran out, and that the tie-break is alphabetical, so nobody reads those numbers as evidence about how the live desk picks among trades. The live desk still spends its budget on the best-ranked ideas first. The practice run still has no ranking, and making up a score to stand in for one is still refused.

**Example —** A practice-run day where the candidates together want more risk than the ceiling allows: Apple gets funded, Nvidia does not, purely because A comes before N. The result now prints that this happened. Nothing about either chart is consulted, and nothing about the live ranking is either.

**The decision —** None for you yet. The reporting half is done. The remaining question is whether practice runs should score their own candidates off something that is actually how the live desk ranks — or whether they simply cannot evaluate rationing. Do not read this item as the ranking having been fixed.

**Moved from WORK.md (2026-09-24) —** Remaining: a non-fitted score that is the live rule (needs verdicts this engine cannot replay), or an owner decision that practice runs cannot evaluate rationing. Do not close as a ranking fix.

## item 65

**Plain language —** When the desk decides which stock ideas look best, each of its five specialists contributes two things: which way it leans, and how sure it is. Only the chart specialist actually publishes a "how strongly" number — it has strong-buy and buy as separate ratings. The other four have nothing of the kind, so for a while the desk quietly made one up for them, three different ways. Those made-up numbers are now gone, and those four specialists count purely on how sure they are. That is honest, but it leaves a real question nobody has answered: should those four be able to say "strongly" at all, or is "which way, and how sure" genuinely everything they can tell you?
**Example —** The news specialist reads a headline and says "bearish, low confidence". The chart specialist can say "bearish" or "strongly bearish" — those are two different ratings it publishes. The news specialist has no such distinction available to it. Today the desk takes that at face value and scores the news read on its confidence alone. The alternative is to add a "how strongly" question to what the news specialist is asked, so it has to state one and justify it per story — which is how the chart specialist works.
**The decision —** Do you want the other four specialists asked to rate their own strength, separately from their confidence? It is a change to what each is asked to produce, not a number to pick.
**Recommendation —** Not yet, and not urgent. The current state invents nothing, which is the important part, and the ranking is honestly described as breadth-and-confidence. Adding a strength question to four prompts is cheap to do and expensive to get wrong — every one of them would be a fresh place for a specialist to assert a number nobody can check. Worth revisiting if the ranking ever looks like it is missing an obvious distinction; not worth doing pre-emptively.

**Moved from WORK.md (2026-09-24) —** Should those four get a real strength field, or is direction plus confidence all they can say? Ruled out: deriving lean from a field they already report (double-counted conviction, deleted 2026-09-13); borrowing Technical's rung; fitting (forbidden, and the conviction ledger is far short of its minimum); a published cross-seat spacing (none exists). Settles with a per-call strength field the analyst must state and justify, or the ledger clearing 20 resolved calls per seat. Do NOT pick a number, and do NOT drop the four seats from ranking.

## item 70

**Plain language —** One made-up number, 1.0, is doing two different jobs in the selling path, and neither job is read off anything. The first job is deciding how far a stock has to move against you before the move counts as real rather than ordinary daily wobble. The second is deciding how tight a stop-loss is allowed to be before the desk refuses it as too close. Both are expressed as "one average day's range". That they are the same figure is a coincidence — nothing ties them — so changing one would not change the other, and changing neither is not a source. The first job is also the only measured over-refusal on this path: of eight proposed sales the reviewer approved, seven were blocked as "too small a move". Closing the plumbing next door did not answer why.
**Example —** A stock whose average daily range is $4 has to move $4 against you before the desk stops calling it noise, and separately, its stop is refused if it sits closer than $4 away. Those two rules constrain each other in a way nobody chose, because somebody typed 1.0 twice.
**The decision —** None for you on the number yet. How readily the desk should block a sale at all is still yours and is not this item — this item is only the two unsourced 1.0s. Each stays open until it has either a published measurement of the quantity it bounds, or a decision to derive one from the other as a single named constant.
**Recommendation —** Keep them as two questions. Do not retune either number to make sales easier or harder — that would be picking an appetite figure. Do not collapse them into one shared constant just because the digits match. Search for a published measurement of each quantity, or name a single derivation that produces both; until then, leave the figure where it is.

**2026-09-26, what changed and what did not —** The two jobs now have two names, at the same value, and nothing the desk does changed today. One number became `NOISE_BAND_ATR_MULTIPLE` (how far a holding must move against you before the move counts as real) and the other `BREAK_CONFIRMATION_ATR_MULTIPLE` (how far a day's closing price must sit past a support level before that level counts as broken). Both are still 1.0 and neither was retuned, which this item forbids in the same pass. A separate error turned up in the ledger while doing it: the exit-path band was filed as if it were worked out from the trailing-stop band of 1.25, which it plainly is not, since it reads 1.0 — so it is now recorded honestly as a number with nothing behind it.
**What the published work says, so nobody searches again —** For the first job every published figure is roughly three times today's: Wilder's 1978 volatility system, the Chandelier Exit's standard setting and Kaufman all sit near 3 average daily ranges, though all three measure a stop's distance from a running high rather than a move away from your entry, so they are the nearest published analogue and not the same measurement. Moving the desk from 1 to 3 would make it markedly slower to accept that a loss is real — fewer premature sales, a bigger give-back before it acts — and that is your appetite, not this item's to set. For the second job there is no answer in these units at all: the literature measures a break in PERCENT of price and differently for a major level than a minor one (Edwards & Magee use about 3% and about 1%), or treats a decisive close as sufficient with no distance at all (Bulkowski). The rule around it — two consecutive closes — is properly sourced; only the distance is not.
**Still open —** Both values, and the separate hard floor under every stop, which still has nothing of its own behind it.

**Moved from WORK.md (2026-09-24) —** Same round number, two questions, no source, nothing tying them. The first job blocked 7 of 8 recorded discretionary exits. Settles with, for each independently, a published measurement of the quantity it bounds, or a decision to derive one from the other as a single named constant. How readily the desk should block a sale at all is the owner's appetite, not this item.

## item 74

**Plain language —** The desk is meant to stop itself cutting the same holding twice in a day. It still can, if the reason is a genuine one — a midday cut on bad earnings can be followed by a second cut at the close on those same earnings. The instructions the reviewer reads do say this is allowed. What is not settled is whether it should be.
**Example —** A holding is trimmed at midday on a poor earnings report. At the close the reviewer reads the same report again and trims again. One piece of news, two cuts.
**The decision —** None for you yet. There is a fair case both ways: a second look at the same report can genuinely find it worse, and forbidding that would be its own mistake. It is on the board so it gets thought through rather than left as a warning in the log.
**Recommendation —** Decide whether a reason is used up once it has been acted on that day, and if not, what separates a worse reading from the same reading used twice.

**Moved from WORK.md (2026-09-24) —** Nothing deduplicates by event; the exit claim check never tests earnings. A later session may legitimately read the filing as worse; the 2026-05-04 AMZN double cut is the harm on the other side; frequency is unmeasured. Settles with a decision on whether a hard trigger is spent once acted on for a symbol that day, and if not, what distinguishes a worse reading from a repeat.

## item 75

**Plain language —** When the desk bought Oracle on 2 September, the chart analyst set a profit target of $159.52 — a price Oracle had already failed at twice. Every seat saw that number: the portfolio manager used it to justify buying, the risk manager saw it, and the position reviewer was shown it every session with the words "soft — you manage exit". But nothing ever used it to sell. The reviewer is told it manages the exit, while its sell rules refuse "taking profit" as a reason, and the "past target" warning only appears at 150% of the way there. Oracle traded above the target on 4 and 8 September. Selling at target would have made about 9%; the desk would have ended slightly below its purchase price.
**Example —** A target is written down before the trade opens, used to say the trade is worth taking, then ignored for the rest of the trade's life.
**The decision —** You asked for each position's target to be shown on the Mission Control chart, and for using it to be debated rather than dropped. Paper-trading "sell at target" for a week was argued against: too few trades to tell luck from skill, one market mood, and it would muddy the restart with a new manager model at the same time.
**Recommendation —** Show the target on the chart. Then track, without placing orders, what four exit rules would have done on every trade — sell all at target; sell half at target and trail the rest; hitting target tightens the trailing stop instead of selling; and today's desk — with the rules fixed before anyone looks and no tuning afterwards. Nothing is built on one trade.

**Moved from WORK.md (2026-09-24) —** **Every number, narrative and blind spot is in `docs/BOARD_NOTES.md` ("item 75") and `docs/INCIDENT_HISTORY.md` (2026-09-14). Read them before working this; do not re-derive.** Short version: the TA target justified the trade, was seen by the Risk Manager and shown to the reviewer as "soft — you manage exit", while the reviewer's sell gate refuses profit-taking. Selling at target was +9.1%; the only automatic exit the code allows filled below entry. The target never reaches the broker, the breach flag needs >150% progress, an 8-K results release is invisible, and six trail constants are unsourced (in item 90's list). Profit-taking, rejection off a high and upcoming earnings are not allowed SELL reasons, and only the news seat emits state changes, so a chart breakdown cannot unlock an exit. Structural finding carried out of the same work: **the desk never sees today's bar**, so every intraday indicator and level describes yesterday's close. Answered 2026-09-14 [measured]: no single standard technical sell rule gave a timely ORCL exit that also beat chance on a 10-name basket — it supports multi-signal confirmation, nothing more. **Settles with a fix, never fitted to ORCL:** exits confirmed by several independent signals; trail tightness read off the instrument or a cited source; a ruling on whether target-plus-confirmed-breakdown may exit; the 8-K gap closed. One number changed alone is the patch this item exists to prevent.

## item 78

**Plain language —** You locked a standing rule: if something the desk needs is missing, find why and make that step actually produce it. Do not invent the missing words. Do not make "drop this name and trade the rest" the standing answer. The current case is a blank "I'll sell if". A temporary patch currently drops that name after we already asked twice, so one blank cannot veto the rest of the book. That patch is not the fix. The real path is: the seats write a real "I'll sell if" before a buy or short can be ticketed; a sentence the model already wrote is put back if a later wipe blanked it; the seat is asked once more; never invent the words. If it is still blank, that name is refused. A catalyst note stays optional.
**Example —** A buy on a chip stock arrives with prices and a stop but the "I'll sell if" box is empty. The desk does not make up a sentence, does not let that blank name veto the rest of the book, and does not ticket it. After one re-ask still blank, that name is refused and the others can proceed. The standing design is that the box is filled, not that the name is dropped.
**The decision —** You locked the standing rule. The temporary drop-the-name patch stays until a live session proves the seats actually fill the box. The rule is not only about "I'll sell if" — any missing required field is the same class of defect.
**Recommendation —** Keep the never-blank path. Keep the drop-the-name patch labelled temporary. Do not treat skip-and-continue as the product.

**Moved from WORK.md (2026-09-24) —** Plain-language account: `docs/BOARD_NOTES.md` ("item 78"). Permanent fix: heal, then one paid retry; still blank → refuse before the book. Never invent. Delete the isolate when a live session proves never-blank.

## item 86

**Plain language —** The live feed that tells the desk instantly when an order has filled has never once worked. The trading process deliberately holds a fake key; a local helper swaps in the real one for ordinary requests, but the live feed does not go through that helper — it dials the broker directly and offers the fake key. Two separate things block a quick fix: the library the desk uses cannot be pointed at that helper at all, and the broker checks the key inside the conversation rather than in the connection header, which the helper cannot reach.
**Example —** Five rounds of work were spent making this path faster before anyone checked whether it had ever worked.
**Nothing is at risk —** Orders are placed over the ordinary connection, which works, and fills are detected by asking the broker every few seconds instead. The feed is now switched off. The only loss is a few seconds of speed. Today's failure count was about 45, not the 147 first quoted — that figure counted log lines, several per failure.
**The decision — no longer yours to wait on; the orchestrator decides after an adversary run, and the decision and its reason are recorded before anything is built.** Five options, best fit first: (1) have the machine hold the key in an encrypted store — **this turns out NOT to be possible on this machine** (the service cannot read the decryption key, there is no security chip, and the installed system software lacks the feature); an earlier answer of "encrypted and tied to the machine" was wrong. (2) A small local relay that holds the real key and rewrites the login message: the only option that keeps the key out of the trading process, but it is custom credential-handling code, which this project has previously refused. (3) Get the helper taught to do this properly, upstream: correct, does not exist, slow. (4) Make the library able to use the helper: does not fix the login problem on its own. (5) Leave the feed off and keep asking the broker — no credential change at all, costs a few seconds of fill latency, and stops about 150 error lines a day.
**Recommendation —** Option 5 today, since it is already in place and costs almost nothing. What remains achievable for protecting the key on this machine is file-permission protection, not encryption. **Standing exception:** if the eventual choice is option (2) or anything else that amounts to new credential-handling code, that is a credential redesign — one of the categories still escalated to the owner directly, regardless of who chose it.

**Moved from WORK.md (2026-09-24) —** Cause: the process holds a 29-character placeholder credential, the credential-injecting gateway rewrites HTTP headers only, and this socket authenticates with an in-band MESSAGE; the installed client has no proxy support either. `execution.fill_stream_enabled` is true on main and NO attempt has been logged since. Trading is not harmed — REST placement works and fill detection falls back to polling. Options, the corrected episode count (~45, not 147) and the credential research: `docs/BOARD_NOTES.md` ("item 86"); do not re-derive it. If a future fix is itself a credential redesign, that stays a standing escalation to the owner.

## item 90

**Plain language —** About thirty numbers that govern real trades were never read off anything — they were chosen because they sounded sensible. They were catalogued on 11 September and then filed as "an inventory, not a job", with a note saying never to re-audit. Nothing was assigned, nothing had a date, and a week later all thirty were still live. There is no mechanical check of any kind that would catch the next one.
**Recommendation —** Two halves. Read each number off the instrument it is meant to describe. And build a check that fails the build the next time an unsourced trading number is added, so this cannot happen again by filing.

**The second half is built, 2026-09-18, and was then torn apart and rebuilt the same day. The first half is still yours.** Every number of this kind now has to be written down in one file next to a sentence saying where it came from, and the build refuses to pass if one appears that is not. Nobody has to remember the rule: the check works out for itself which numbers count, and the only way to add one is to write the sentence. It insists on six things — that the number is written down at all; that the figure in the file is still the figure in the code, so changing a number puts the reason for it back in front of whoever is changing it; that the figure in the file is also the figure in the settings file the desk actually runs on; that a claimed source is a link or a file and line somebody else can open, not a confident paragraph; that a number with nothing behind it also states the question that would settle it and what the desk pays meanwhile; and — the one worth knowing about — that a number worked out FROM another number fails the build if that other number later moves. A figure can be perfectly well justified when it is written and quietly meaningless a month later because the thing it was measured against changed; that now shows up as a broken build instead of a respectable-looking comment.

**Why it was rebuilt, and it matters more than the check itself.** The single entry the first version held up as its showcase — the 0.50% minimum risk per trade — was wrong in four separate ways. It said the number appeared nowhere in the settings file, nowhere in any document, and in no record of you approving it. All three were false: it is in the settings file, it is in your own ratified risk table in `docs/OUTCOME.md`, and you approved it on 27 August. It also said the number existed in only one place while the same file listed it twice, and a third copy of it was invisible to the check altogether. Every one of those was a thirty-second look away and nobody looked. **So read the honest limit of this thing: it proves a reason has been WRITTEN, never that the reason is TRUE.** That is why a source must now be something openable rather than prose, and why seven corrected entries carry the correction in writing rather than being quietly tidied.

**The count.** 178 numeric sites are now watched, up from 122, after four more modules were brought in — including the ATR period, which every stop and every noise band on this desk is a multiple of, and which the first version watched all the multipliers of while ignoring the unit. **86 distinct numbers decide how this desk trades and have nothing behind them.** That is fewer than the 87 first reported despite the wider net, because mirrored numbers — the same figure written in two files — are now recorded as one number rather than two, and three turned out to be genuinely approved by you and were reclassified. Every value was left exactly as it was: changing one is your call, not an agent's.

**What it still cannot do, so it is never oversold.** It cannot tell a true source from a plausible-sounding sentence; that is the failure above and it is structural, not an oversight. The list of watched modules is written by hand — there is now a second check that counts the numbers in every module NOT on the list and fails if that count grows, so a new one cannot slip in unseen, but the list itself is still a judgement call. It cannot see numbers written into the seats' instruction sheets as prose, which is a separate item. And it does not make any of the 86 sourced — it only stops the 87th arriving unnoticed.

**One factual correction, because it was being repeated.** The story that the range stop-width scaler 0.90 was derived against a stop base of 1.5 which later became 2.5, leaving the derivation stranded, does not match git. The base went from 3.0 to 2.5 on 2026-09-10, and 0.90 was introduced by that same change — it was never derived from anything, and the code beside it says as much ("not a specific measured number"). The class of defect is real and the check now catches it; this particular example is not an instance of it.

**Moved from WORK.md (2026-09-24) —** Full account of what was built and the gate's own honest limit (it proves a justification was WRITTEN, never that it is TRUE): `docs/INCIDENT_HISTORY.md` (2026-09-18). The counts quoted there are a snapshot, already known stale by the next day — read `config/number_ledger.yaml` directly rather than trusting a number here. **Half two, STILL OPEN:** read each arbitrary entry off its instrument. Each states in the ledger the question that would settle it and what the desk pays meanwhile, which is what makes half two prioritisable rather than a list. `MAX_ARBITRARY_ENTRIES` is an EQUALITY, not a ceiling: as a ceiling it rewarded deleting a row.

## item 99

**Plain language —** A second review, of the prompts that brief the analysts (the seats that read the market and write reports, one layer below the decision-makers), found the prompts are full of numbers and claims nothing in the code actually enforces. The desk already bans numbers that were invented rather than read off real data; a number that lives only in a brief is exactly that, and it was invisible because nobody had looked in the briefs. (Filed twice, as items 99 and 105; diffed and collapsed into this one 2026-09-18.)
**What it found —**
  - About 55 numbers exist only as text in a prompt, with no code checking or producing them.
  - About 20 claims about how markets behave are stated as fact with no source.
  - The technical analyst is told it gets 20 days of price history; it actually gets 40, plus five whole categories of data the prompt never mentions it has.
  - The evening report's briefing hasn't been touched since before this project started, and still describes a completely different strategy — a 77-symbol quarterly value approach — while the technical seat's own briefing describes a 5-to-15-day swing-trading window. Both feed the same decision seat. They cannot both be the desk's real strategy — **this is now a pending decision for you, in `docs/WORK.md`.**
  - Roughly a third of the portfolio manager's briefing, and a quarter of the risk manager's and the position reviewer's, is prose describing machinery the model doesn't actually use. That dead weight is where almost every stale or wrong claim above was found living.
  - Separately: a check already exists that fills prompts with numbers straight from the code so they can't go stale, but it only covers 2 of the 10 prompt files. Scanning prompt text for suspicious numbers doesn't work either — there are about 1,825 numbers in there, mostly just dates and list numbering. Neither of the two confirmed mistakes above (item 98) was even sitting in a prompt file — both were assembled by Python code into a string. What would actually have caught the worst one: when code that a prompt describes gets deleted, search the prompts for its name at that moment.
**Recommendation —** Decide the evening-vs-technical mandate question first (it changes what "fix the prompt" even means); then strip the dead weight, since that's where the false statements cluster; build the deletion-site check as ongoing insurance rather than trying to scan for numbers. Not yet placed in your priority order.

**Technical detail, moved from `docs/WORK.md` 2026-09-24 —** (a) About 55 numbers exist only as prompt prose with no code behind them, and about 20 market-structure claims are asserted with no cited source. (b) The technical seat receives five data blocks its prompt never mentions; its separate bar-count defect was fixed and retired 2026-09-20 (`docs/INCIDENT_HISTORY.md`). (c) Dead weight — prose describing machinery the model does not perform — is roughly a third of the portfolio manager's prompt and a quarter of the risk manager's and the position reviewer's, and is where almost every stale claim clustered. (e) Order of work: settle the mandate question first, then strip the dead weight; the deletion-site check is insurance, not the first move. (f) Measured 2026-09-19: PM sizes sit on the prompt's own 0.25 grid — 36 of 37 recorded `risk_allocation_pct` on-grid, max ever 3.0 vs the 5.0 ceiling — and "2+ oversized → cut every BUY 25%" is unenforced prose, same class as (a).

**Moved from WORK.md (2026-09-24) —** Do NOT build a prompt-text number scanner. **(g) Folded in from item 171 (retired 2026-09-23):** (d)'s deletion-site grep alone does not catch 98 or 168, both value-drift, not deletion. DONE WHEN, added to (d)'s: every prompt sentence stating a code/config-controlled fact is either rendered from that value or pinned by a drift test, per item 168's own DONE WHEN pattern — not a blanket prose/number scanner, still rejected per (d).

**Mandate/horizon drift fixed 2026-09-25 (item stays open for the rest of the audit) —** The evening-vs-technical mandate question from `docs/WORK.md`'s DECIDE BY line is resolved: SWING (days to weeks), decided by the orchestrator after an adversary run, per `docs/OUTCOME.md:75` and `config/prompts/tech_analyst.md:3`'s own existing treatment of hold length as PM/position_reviewer's call, not the analyst's. Holding period is an OUTPUT of thesis health, not a setting.

Changed, `config/prompts/evening_analyst.md`:
- Lines 8-14 (was): `This trading book is a **medium-long-term value + mispricing capture** mandate. The 77-symbol universe was hand-curated by a human operator who cares about catching era-level secular trends, identifying high-potential companies early, and spotting resource misallocations. **It is not a day-trading book.** Your review should reflect that lens: weekly → quarterly horizons for thesis work, with daily P&L only as accountability noise.` → (now): swing mandate — days to weeks, not day-trading and not a quarterly value hold; holding period is an OUTPUT owned by `portfolio_manager`/`position_reviewer`; the same curation criteria (secular trends, high-potential names, resource misallocations) are kept as why the universe was picked, not as the holding horizon; daily P&L is now framed as a real signal, not noise to wave off.
- Line 135 (was): "THE most important step for a medium-long-term book." → (now): "THE most important step: holding period is an output of this call, not a calendar setting."
- Line 429 (was): "no micro-caps in a medium-long book" → (now): "no micro-caps in this book" (the liquidity criterion itself is unchanged; only the false horizon label was removed).

Changed, `config/prompts/meta_reflector.md`:
- Line 192 (was): "Realized timeframe vs intended medium-long-term mandate?" → (now): "Realized timeframe vs the swing mandate (days to weeks, held only as long as the thesis stays intact — hold length is an output, not a target)?"
- Lines 202-204 (was): IDEAL state named as "medium-long-term value + mispricing capture across broad themes, 77-symbol curated universe" → (now): "swing mandate — days to weeks, holding period an output of thesis health rather than a setting, broad secular-theme coverage across the 77-symbol curated universe."
- Worked example block (lines ~335-341, marked "reference only — DO NOT copy" but still teaches the mandate by example): the sample self-portrait previously scored a 7.2-day average hold as a violation of "the medium-long-term mandate." Under the corrected swing mandate that hold length isn't a gap, so the example's third "top gap" was swapped from execution_style to loss_discipline (using data already present in the same example: 3 wrongs ridden ~8 days past their own thesis-break trigger), and `style_self_portrait` / `persistent_blindspots` were brought in line with the same correction.

Kept untouched (real machinery, not drift): the enum literals `trend_timing_miss` / `fundamentals_mispricing` / `value_entry_missed` / `theme_blindspot` and the `⚠ VALUE_ENTRY_CANDIDATE` marker in `evening_analyst.md` — these are read by `src/models.py` and `src/pipeline.py`, not free prose; the meta_reflector's own "quarterly" audit CADENCE (it runs once a quarter — `quarterly_digest`, `quarterly system-level audit`) — that is the review's own schedule, not the trading horizon, and was never part of the drift; the 77-symbol universe-size figure itself, which is a separate, narrower staleness question (production settings put the real universe at ~101 symbols) not in scope for this fix and not touched here.

Not done here: the other ~55 unsourced prompt numbers, ~20 unsourced market claims, and the PM/RM/position_reviewer dead-weight prose this item's audit also found — those remain open under this same item number.

## item 112

**Plain language —** When the automatic de-lever trims the book and the trims still leave it over the limit, nothing tells you. The system writes a warning to its own internal log, but that is as far as it goes — it does not reach a message to you, the end-of-session summary, or anything that gets checked.
**Recommendation —** Add an alert for the case where a de-lever pass finishes and the book is still over its limit.

**Moved from WORK.md (2026-09-24) —** Full account: `docs/INCIDENT_HISTORY.md`, 2026-09-19 entry.

## item 109

**Plain language — not yours to wait on any more; the orchestrator decides after an adversary run, and nobody may settle it by editing code first.** The desk has a big-picture seat that reads the whole market: interest rates, credit, volatility, the general mood. It also has seats that read one company at a time. When the desk counts up how much evidence supports a single trade, it currently counts the big-picture read as one of those votes, for or against that individual company. The instructions given to the trade-picking seats said the opposite — that the big-picture read never counts toward that tally. One of the two has been wrong all along, and the code is the one that has actually been deciding.

**Why it is not theoretical —** On 17 September a bullish read on the market as a whole cancelled out a bearish filing about one specific company, and the trade died. A second company passed with full support on the strength of the market read plus a filing, with no read of its own chart at all.

**Why we stopped rather than fixing it —** The obvious "fix" is to correct the instructions so they match the code. That would quietly make the current behaviour official, and the desk's own standing doctrine points the other way: it is the trade-picking seats' own prompt (not `docs/OUTCOME.md`, which says nothing on this beyond a line about cash deployment) that states the market read is the regime the book is built in, not a fact about one company. Making the instructions match the code would have ratified a rule you never agreed to. So the wording is being made neutral — it says the tally does count it, that this is disputed, and not to lean on it either way — and the counting rule itself is untouched. Filed independently twice, hours apart, as this same question; the two are now one item.

**Recommendation —** None yet; decide after an adversary run, and record the decision and its reason before anything is built on it. This is a mandate question, not an engineering one, but it is no longer one that waits on him.
**Moved from WORK.md (2026-09-24) —** **(a) To be decided by the orchestrator after an adversary run (2026-09-18 ruling) — does macro count as a seat in the agreement gate?** `count_aligned_sources` counts macro ±1 alongside technical/news/earnings/smart_money and flips macro's polarity for inverse ETFs, so the code treats a regime read as per-name evidence **by design**; the PM's sheet said twice, emphatically, that it never counts. On 2026-09-17 a name was killed when a bullish regime read cancelled a bearish filing, and another scored full agreement on macro plus a filing with no technical read at all. The sheet now states the truth and flags the disagreement; the GATE is unchanged because changing it moves trades. The regime-not-evidence side is the PM prompt's own provenance rule, **not** `docs/OUTCOME.md` — that miscitation has already been made twice. Either macro is per-name evidence or it is the regime the book sits in; decide, then make one of the two match. **(c) Dead weight.** ~35% of the PM's sheet, ~26% of the risk manager's and ~24% of the reviewer's is recitation of machinery the model does not perform. Deleting it is the durable fix; done only where a claim was false, because deleting a load-bearing recitation changes behaviour (the reviewer's trigger vocabulary is the clear case — a seat that does not know the words has every exit silently dropped).

## item 180

**Plain language —** Before the desk will buy anything it checks how long the stock has been listed, and refuses outright if it has fewer than 200 trading days of history. That 200 is not a separate safety number: it is the same 200 used for the 200-day average line on a chart (`LONGEST_INDICATOR_WINDOW`, one constant doing two jobs). A young name is therefore turned away for failing a calendar count rather than for missing anything the trade itself needs — at 89 sessions the only measurement that does not compute is that one average; the volatility reading needs 14 bars, the support/resistance scan needs 14, and the 20- and 50-day averages compute normally. That average is used in exactly three places outside the file that computes it — the data record, the text handed to the technical analyst, and the exit guard — and in none of the sizing, stop, target or ranking maths [verified by grep, 2026-09-23]. The history really is short, not merely unfetched: the desk asks for 1,800 calendar days of bars.
**Example —** CBRS was refused on 2026-09-23 with "only 89 completed session(s) of history against the 200-session window" [production log, read-only]. Across 2026-09-17 to 09-23 the refusal touched two symbols only, CBRS and DRAM, about 2% of the universe [measured, production DB agent logs, read-only, 2026-09-23; 13 refusal records — the count is sensitive to whether a record or a session is counted, so treat it as "a handful, two names"]. The desk does not go looking for new listings anyway: `universe_screen.enabled` is false. On every CBRS refusal the book was already at roughly 199% of its 2.0x gross ceiling, so there was no capacity to buy it regardless. The one refusal on a day with capacity was DRAM on 2026-09-17, which then rose 8.7% over the following four sessions — whether it would have out-ranked what was actually bought, or cleared the stop-width gate, was NOT checked, so that is an upper bound on the cost, not the cost.
**Why it matters —** the desk is incoherent about this across three different numbers: an out-of-universe candidate is admitted at 20 days of history (`smart_money.min_external_history_days`), the constructor refuses under 200, and the universe screen uses 210. Moving any one of them does not make the desk coherent. There is also a precedent: the owner's "no floor, no trade" rule was falsified by research and deleted in September 2026 (PR #331), and this narrow refusal survived that deletion on different grounds with no literature check of its own. That same write-up's closing lesson is that the rule went from a sentence to shipped code in one pass without one. This gate has never been separately ratified.
**The decision —** None for you; the owner has already given his framing and his priority. He does not want the number invented or lowered — in his words, if it does not have 200 days of data, you cannot make it up — he wants a way to capture the opportunity that does not rest on a made-up number. His priority call: on the board, not high priority, but a real priority.
**Recommendation —** Refuse on the MISSING INPUT the trade's own plan actually requires, rather than on a calendar count. A trade whose thesis or exit condition references the 200-day average is honestly refused until that history exists; a trade that never mentions it is judged on the measurements that do compute. This is a proposed resolution, not a decision.
**What must ship with it —** a hard prerequisite, not a nicety: the exit guard returns UNPARSEABLE for a `thesis_invalid_if` that references MA200 when the average is not computed (`src/risk/exit_guard.py`, the MA-reference branch). Admitting young names without handling that ships a position whose exit condition the desk cannot evaluate. Any fix must close it in the same change.
**Ruled out, do not repeat —** lowering 200 to another conventional number: Brock, Lakonishok and LeBaron (1992) chose 1-200 and the other pairs explicitly for their POPULARITY, on the DJIA index, not as a data-sufficiency test. Also ruled out: any youth-based size-shrink factor, which would be a new unsourced number arriving as the remedy for an unsourced number.

**Moved from WORK.md (2026-09-24) —** At 89 bars only `ma_200` is None; it feeds no sizing, stop or target. Owner: a real priority, not high.

## item 127

**Technical detail, moved from `docs/WORK.md` 2026-09-24 —** Measured once, 2026-09-16: an `intra_check` and a `run` started 5 ms apart, both ran the same fractional stop repair, and both submitted an identical DAY stop 23 ms apart. The broker rejected the second (`held_for_orders`), the retry burst exhausted, and the desk declared failure and alerted the owner on an order that had actually landed — the false alert is the real cost, not a double fill. Occurrence count across all six log rotations 2026-08-14 to 2026-09-18: one, plus a 2026-09-17 near-miss closed by timing (item 84), not by a lock. The partial mitigation that shipped for this (`_submit_stop_leg_retrying`, PR #432) has never once executed. The disease behind it: every write in `intra_check`'s preamble runs before any lock (`src/pipeline.py`: the seven `_drain_*`/`_reconcile_*`/`_release_*` calls in that preamble) and only the paid opportunity scan is lock-protected. The same exposure is open on other shared broker writes: `cancel_open_entry_orders()` and the cancel-stops-then-sell path in `src/pipeline.py`, `replace_stop_loss` in `src/execution/stop_records.py`, the repeg and fill-handling writes in `src/pipeline_stages.py`, `src/execution/stop_repair.py`, and `replace_missing_stops` in `src/coverage_watchdog.py` — a separate process entirely. `_scale_in_skip` already guards the worst pairing exactly for the watchdog; nothing guards it for `intra_check`. The mechanism exists in-repo and needs no new constant: `_intraday_scan_process_lock` takes `fcntl.flock(LOCK_EX | LOCK_NB)` on a local file, released by the kernel on process death, and on the incident tick it correctly detected contention and skipped.

## item 76

**Moved from WORK.md (2026-09-24) —** Settles with a before/after benchmark of whether the PM uses `reasoning_chain.macro_audit`, which needs the owner's go. Do NOT reopen as a size problem.

## item 77

**Moved from WORK.md (2026-09-24) —** Blocked by owner decision 2026-09-15.

## item 81 — RETIRED 2026-09-24

**Moved from WORK.md (2026-09-24) —** `SUBFLOOR_SIZE_CAPPED_STATUS` (`src/agents/portfolio_manager.py:59`) is assigned nowhere and asserted only by `tests/test_subfloor_catalyst_gate.py:841`. Keeping a key so a silent rename cannot resurrect a threshold is a real argument, so decide once and record it — keep with a ledger note, or delete. NO LIVE NUMBER'S VALUE CHANGES either way.

**Retired 2026-09-24** — decided: delete. `SUBFLOOR_SIZE_CAPPED_STATUS`, `RiskConfig.min_reward_risk_after_widening` and `ConstructorConfig.min_reward_risk_after_widening` were removed as zero-reader dead code, each re-verified against the current tree first. `REWARD_RISK_FLOOR` itself was NOT deleted — `ops/model_policy/deterministic_selection.py` still reads it in a real comparison for the model-selection benchmark. Full writeup in `docs/INCIDENT_HISTORY.md` (2026-09-24 entry).

## item 173

**Moved from WORK.md (2026-09-24) —** Inside the 7-day lookback the next pass writes it back; past ~2026-09-28 it turns unexplained, and `send_owner_alert` has NO dedup or throttle, so it pages CRITICAL at all five session entries, daily. Nothing shows the exit was a stop, so writing it back as one stamps an unevidenced cause onto owner P&L. **(b)** `_reconcile_stop_out_fills` runs BEFORE `_reconcile_fills` at every session entry, so the desk's own unreconciled sale pages a false CRITICAL (NUE 2026-09-21). **(c)** Signed from the action name, so a COVER — and a filled buy-to-cover TRAIL_STOP — subtracts from a short instead of retiring it (36 short, fully covered, reads -72). Silent today; pinned by a test saying it is wrong.

**Update (2026-09-25) — (c) FIXED, (b) verified as already-handled, (a) still open.** (c): `get_symbols_with_open_ledger_qty` now signs a share-moving row by the SIDE of the position it acts on. A COVER-family action (COVER / EMERGENCY_COVER / PARTIAL_COVER, `(pct)` label normalised) is a buy-to-cover and adds; a FILLED TRAIL_STOP has no side in its name so its side is read from the running net, and one resting on a short adds too. A 36-share short covered in full now reads 0, not -72; both routes fixed together and the pinning known-defect test was deleted per its own instruction. Long-side signs are unchanged (SELL/REDUCE/STOP_OUT/SWEEP_SELL and a long's fired TRAIL_STOP still subtract). No live behaviour changed: the caller is LONG-only and skips negatives — this only makes the ledger's own belief correct for the day shorts are enabled. (b): all four call sites were re-checked against origin/main. Intraday and evening already run `_reconcile_fills` first (PR #697), which closes the false-CRITICAL gap. Morning and the midday/close position review deliberately keep the old order — their `_reconcile_fills` runs later and only over THIS session's own just-submitted / FORCE_DELEVER rows, so no stale 'submitted' SELL exists to raise a false page there (both sites carry the Item 173(2) rationale in-code). No false-alarm site remains in the old order, so NOTHING was changed for (b). (a): the EQNR share-count data gap is a live-data issue and stays open — item NOT retired.

## item 107

**Moved from WORK.md (2026-09-24) —** Three gaps, none a wording fix: **(a) Behaviour that changed without being deleted** — nothing is registered, so nothing is scanned, and the shipped check is blind to the whole class. The live instance is item 108. **(b) Numbers that exist ONLY in prompt prose** — invisible to a code audit and to any drift check: the PM's whole sizing arithmetic (bases 3.0/1.75/0.75, +0.25 R/R bonus, ±0.20/±0.10 evening tilt, 0.5 stale halving at age ≥8d) and Tech's "3+ aligned signals", 1-3/4-7/8+ freshness tiers and forward-PE 40/60 + P/S 15/25 levels. Trade-governing, unsourced; source, derive or delete each. A DIFFERENT shape added 2026-09-18: the reviewer's `weight_pct > 12%` escalation and the `DRIFT` flag's matching 12 are bare inline literals in `src/pipeline.py` and `src/agents/portfolio_manager.py`, with no settings key and no named constant, so the number has three homes and the rendering mechanism can reach none of them. Name it, or move it to settings, before it can be rendered. **(c) Rendering coverage** — `prompt_limits.py` renders limits from live settings in 2 of 10 prompt files; the other eight hand-type every number. Mechanical where a number has a settings key; otherwise it is (b). It raises at agent construction, so a bad placeholder halts the desk — fail-closed, and a new way a settings edit stops trading.

## item 119

**Moved from WORK.md (2026-09-24) —** On 2026-09-17 the morning open brought back 8 of the 15 required FRED series; the other 7 were never requested at all and the log said the deadline was exceeded. That is not a St. Louis outage: the observation calls and the due-date metadata calls share the same worker slots under the existing 90s ceiling, so a healthy batch spends nearly the whole clock and one slow series starves the rest. PR #435 built an observations-first fix (one attempt per series, then one bounded re-ask of the misses inside whatever budget remains, metadata on leftover budget, unknown freshness named rather than invented, ceiling NOT lengthened). **That code was never merged and the item does not inherit its verdict.** Its measurements — isolated series 1.5-6.7s, a clean full batch 15/15 in ~85s — were taken MID-MORNING, when FRED was already healthy. The defect is at the open. Mid-morning numbers are a starting point, not merge-readiness. Do not lengthen the timeout and do not invent a missing value; an incomplete set is a lost economics seat.

## item 125

**Moved from WORK.md (2026-09-24) —** That auditability is a real strength and must not be traded away. The gap is that nothing checks whether the emitted enum is any good, while the desk's own cited literature flags exactly this (`src/verdicts.py:54-59`, Benhenda 2026 — cited in-repo, unverified). **REJECTED, with the evidence:** Loughran-McDonald or a similar lexicon. Kirtac & Germano (arXiv 2412.19245; 965,375 articles 2010-2023) measure LM at 0.501 accuracy against 0.744 for an LLM, and find LM adds nothing incrementally (t = 1.871). Its licence is academic-only; it would need at least three unsourceable constants; it could only REPLACE the shown-arithmetic derivation; and no instance of an accounting word misread as negative is recorded anywhere — it fixes an error class this desk does not produce. That evidence is one study, on news rather than filings.

## item 139

**Moved from WORK.md (2026-09-24) —** The cost is disk and an unreadable registry, not dangling refs.

**Sweep shipped 2026-09-25 —** `scripts/prune_worktrees.py` is the mechanical guard: it clears registrations whose scratch directory is gone (`git worktree prune`) and removes worktrees that are merged into `origin/main`, clean, unlocked, owned by us and idle >= 7 days (`git worktree remove`, never `--force`). Scoped by construction to this repo's own registered worktrees and refuses any path owned by another uid, so it can never touch another tenant on this shared box. Read-only by default; `--prune` acts. The swept-on-a-schedule rule is `scripts/systemd/quant-agent-worktree-prune.{service,timer}` (daily 04:10 ET, off-hours because it is the one maintenance sweep that writes), run through `scripts/run_worktree_prune.sh`. Detection logic is unit-tested against injected git/fs stubs plus a real-git integration test (`tests/test_prune_worktrees.py`).

## item 147

**Moved from WORK.md (2026-09-24) —** An inexact day blocks the quota rearm, so a provider omitting usage costs budget never spent.

**Investigated 2026-09-25, STILL OPEN, no code change — the honest fix is out of `cost_circuit.py`.** Since item 14 (2026-09-02) removed reservations, a null-usage success is no longer "charged the reserve": `complete_call` books $0 but increments `unknown_cost_rows`, marks the day inexact, and HARD-latches `unknown_actual_cost` (operator-only). Doctrine (`cost_circuit.py` ~112-115, added 2026-09-22) keeps that on purpose: "real tokens were generated; the unknown is real spend." Adversary-verified 2026-09-25: `complete_call` sees only `cost is None` and conflates TWO cases `src/agents/base.py` produces — (a) genuine zero-token success (`base.py` ~2340, no telemetry, real unknown spend, hard latch defensible) and (b) known non-zero tokens but the model is absent from the pricing table so `estimate_cost` returns None (`base.py` ~2374, `cost_table.py` `estimate_cost`). Case (b) is a pricing-data defect, not unbounded spend, and the hard latch over-punishes it. BUT `cost_circuit` cannot book a measured figure for (b) either — the missing thing is the per-token RATE, and the pricing table is the same one that failed, so there is no rate to multiply by. A real fix must (1) pass token counts from `base.py` into `complete_call` to route (a) vs (b) — which breaks the breaker's deliberate provider-independence — and (2) source a rate for the unpriceable model (add it to `cost_table`, needs a real number). Neither is verifiable/safe on the paper/free-data setup, and whether case (b) has EVER fired in production is unconfirmed (needs a prod-DB read of `llm_budget_days`/`agent_logs`; the "three null-cost rows on 2026-08-31" are not classified (a)-vs-(b)). Left for owner decision; no behaviour changed.

## item 148

**Moved from WORK.md (2026-09-24) —** Separately, the correlation-cluster window silently moved from 120 days to 5 years with no recorded reason; the 0.7 threshold itself is already in the ledger as arbitrary and is not re-filed here.

**RESOLVED 2026-09-25.** Two level-ranking literals: already closed on main — the `/ 10.0` strength divisor is the named, ledgered `LEVEL_STRENGTH_DISTANCE_DIVISOR_PCT` (PR #684) and the flat 40% max-distance cap was removed 2026-09-12 (ATR `horizon_reach`, already ledgered). Correlation window: cannot carry a ledger row — it rides `trading.lookback_days`, which has no numeric default and so is not a definition site the scanner can attach an entry to. Took the DONE-WHEN "recorded reason" branch instead: the reason is now written at `_ensure_correlation_matrix` and cross-referenced beside `CLUSTER_CORRELATION_THRESHOLD`. The honest reason is that the 5y window is INHERITED from the structural-level fetch (settings.yaml records the 320→1800 raise as "purely for structure"), never chosen for clustering, which needs only 20 overlapping returns. No number-ledger count change; no value or behaviour changed.

## item 152

**Moved from WORK.md (2026-09-24) —** The technical seat now parses its answer row-by-row and salvages every well-formed stock instead of discarding the whole answer (`docs/INCIDENT_HISTORY.md`, 2026-09-19).

**News-seat half closed (2026-09-25, #695).** The news answer is a single nested report, not a list of rows, so its per-entry salvage is `_drop_invalid_state_changes` / `_drop_invalid_stock_news` (a bad state-change or stock-news bullet is dropped alone, the rest of the report survives), and its whole-answer failures are the two the retained forensic dumps actually showed — genuine non-answers ("I need more context…") with nothing to salvage. #695 gave the whole-answer non-JSON path the same one paid heal retry the schema-validation path already had, removed the retry-flag leak on the long-lived instance that had disabled that retry for every later failure of the run, and reworded the final exhausted-retry lines so `log_health` classifies them under `seat_answer_unreadable`; every exhausted failure persists its raw payload rather than being paid-and-discarded. Recovery-on-retry, no cross-call flag leak, and payload persistence are each pinned by tests in `tests/test_news.py`.

## item 154

**Marked short-handed (2026-09-25) —** Verified first: an unreachable seat already sets `data_status[seat]="failed"`, which the evidence-freshness disclosure ("no answer at all: …"), the "degraded:" line and the unsilenceable data-quality alert all surface owner-facing and durably. What was still missing was the MARK: on a refusal (item 20) the owner sees "DECISION SKIPPED — NOTHING WAS TRADED", but on the proceed-anyway case the absence read as neutral freshness with nothing saying the desk went ahead short-handed. Added the mirror — a `DECIDED SHORT-HANDED` block that names the absent seat(s) in plain words and states the decision was made without them — rendered on every proceed (morning/midday/close/once/intra) and suppressed on a skip so the two banners never double up. Disclosure only, no threshold, no `data_status` key added; reuses the existing `evidence_freshness.absent_seats` already carried in the result. Reproduction proof: the pre-fix disclosure never contained "SHORT-HANDED"; `tests/test_evidence_gate.py` covers the mark, the fully-staffed no-mark case, the no-verdict rule and the proceed-vs-skip split.

**Moved from WORK.md (2026-09-24) —** Distinct from item 20, which refuses a decision when a seat's answer is LOST entirely — this is the proceed-anyway case, and nothing tracks it.

## item 157

**Moved from WORK.md (2026-09-24) —** Per that write-up, constrained output needs a wrapper object (answer is a bare list, strict schema needs an object), a separate model-facing schema (eight desk-filled fields), `strict=false` (one free-form map field), and a live call to confirm the Google route actually enforces a sent schema — untried.

## item 165

**Moved from WORK.md (2026-09-24) —** Three related gaps remain, found reviewing that closure, not reopening it: (1) the evening thesis-health reviewer renders an unlabelled calendar `"{days}d held"` (`src/agents/evening_analyst.py:89`) to a model being asked to judge trade progress against a prompt otherwise written in sessions — #493 called this figure "purely informational," but a progress judgement is not an informational use; (2) the `sessions_held < max(1, pinned_horizon / 3)` floor's `1/3` constant was derived back when the base was calendar days and was never re-examined after the base changed to sessions, so in sessions it now fires strictly more often than intended, suppressing pace warnings; (3) the session/holiday calendar used throughout is Mon-Fri only and does not account for market holidays, so holiday weeks still overstate sessions held, despite a broker calendar existing elsewhere in the codebase.

## item 174

**Pairing added 2026-09-26 —** the resume alert is now tied to a suspension alert the owner actually received. When the "SUSPENDED" send fails, the auto-clear wipes `suspended` and `alert_state`, so that suspension alert becomes permanently undeliverable; sending "RESUMED" anyway reported a recovery from an incident he was never told about. The auto-clear now records the suspension's alert state at the last moment it is knowable, and an unpaired resume is resolved without sending. Also: the offline harness could not reach this path at all (its 503 is pre-generation, hence provably free, hence never latches), so a `server_error_mid_stream` kind was added and a morning rehearsal reproduced latch → suspension alert → auto-expiry → paired resume alert end to end. Detail in `docs/INCIDENT_HISTORY.md`.

**Alert shipped 2026-09-25 —** the auto-expiry now sends the owner the same-surface Telegram alert the suspension does (🟢 RESUMED, naming the forgiven trigger and that it auto-expired), keeping the `auto_reset` DB event and log; delivery is durable/retryable with the same claim state machine the quota-recovery alert uses. Still open: the two unledgered constants below remain unmeasured.

**Moved from WORK.md (2026-09-24) —** Also unmeasured: the 15-min cooldown (midpoint of the 30-min paid-run gap) and the 19/day allowance (one per paid run) have not met a real occurrence, and neither is covered by the number-ledger check.

**Ledger gap is STRUCTURAL, not an oversight (verified 2026-09-25).** Both constants live in `LLMCostCircuitConfig`, which is DELIBERATELY outside the ledger's `SCOPED_CONFIG_CLASSES` (the scanner's own comment names "LLM cost circuits" as settings with "nothing to do with a trade"). Closing the gap would mean scoping the whole cost-circuit class and ledgering every numeric field in it — a scope-policy change, not bookkeeping — and even then `max_transient_latch_auto_clears_per_day` uses a `default_factory` (`_paid_run_count()`), which the scanner structurally cannot see (its own docstring lists this as uncatchable). So there is no clean two-row ledger add here; left for the owner/scope call, not fixed in the item-148 pass. **Separate finding, not mine to fix:** `intra_check` is the desk's LARGEST model spender — 72% of spend on 2026-09-22, 90% on 09-21, 13-14 paid runs a day [measured] — while its own code comment said "no LLM"; comment corrected, but whether a 30-min tick should be spending that is untouched.

## item 177

**Moved from WORK.md (2026-09-24) —** Questions: the 3 `IntradayScanConfig` ledger entries. Cadence: 3 code sites, unledgered, untested. Stop coverage is separately scheduled and free; the intra preamble (fills, stop-outs, loss check, drains) is not.

## item 179

**Moved from WORK.md (2026-09-24) —** Three findings, none acted on: the heal sets the seat from `model_dump()`, and the nomination collector reads `.nominations` only when the value is NOT a dict, so a healed macro contributes zero nominations silently by design-comment; the heal writes nothing to the macro store, so the next tick re-reads the superseded snapshot; and the only place a macro answer IS stored swallows its own failure as a warning. Also `mechanical_heal_macro` is reachable from tests only [verified, no `src/` caller].

**Store-write half FIXED (2026-09-25).** A successful paid macro heal now persists its answer to the macro store (`_persist_healed_macro_store`, mirroring the news heal's `_cover_healed_news_wire`), the same `save_last_state(payload, series_prints=...)` the scheduled morning read uses, so the next tick's carry-forward, the evening thesis-health read and the 7-day history all see the fresher regime the desk paid for instead of the stale snapshot. Reproduction test in `tests/test_seat_heal_wiring.py` fails pre-fix.

**"Zero nominations" finding CORRECTED — not a defect.** The `model_dump()` shape does NOT lose nominations in the live flow: `ctx.macro_analysis` is canonically a dict (its type comment; PM reads it with `.get()`), and macro nominations are collected exactly once inside `MorningResearchStage._collect_seat_nominations`, which completes BEFORE `_heal_lost_research_seats` runs. So the shape cannot change any nomination outcome, and changing the heal to emit a model object would fix nothing. What remains true is a deeper SEQUENCING limitation — because collection precedes the heal, a healed macro's nominations are never collected at all — which is a separate question, not the shape bug the original finding described.

**`mechanical_heal_macro` dead code REMOVED (2026-09-25).** Verified no `src/` caller: the coercion it wrapped (`coerce_macro_shape`) is already wired into every live macro consumption point (`macro_analyst`, `portfolio_manager`, `pipeline_stages`, `pipeline`), and the live heal orchestration (`_heal_lost_research_seats`) uses paid retries, not this unpaid HealResult wrapper — there was no intended fallback for it to be wired into. The function and its four test-only cases were deleted; the still-live helpers and their tests were kept.

**Still OPEN on 179:** the scheduled-save warning-swallow finding (pre-existing, report-only).

## item 181 — RETIRED 2026-09-24

**Moved from WORK.md (2026-09-24) —** For a SHORT the risk path uses `stop - entry`, so a higher entry NARROWS it and INFLATES `qty_by_risk`: when `entry` sits ABOVE the today `print` the short exceeds its budget by ~`(stop - print)/(stop - entry)`. Bounded by `qty = min(qty_by_alloc, qty_by_risk)` so not unbounded, but real and reachable (the 5% freshness skip measures entry against the mid, not the print), and UNTESTED (item 120's short test disables the risk path and sets `entry == print`). Fix: size a short's `risk_per_share` off the print, not `max(print, entry)`. Two adjacent out-of-scope findings recorded in `docs/INCIDENT_HISTORY.md` (2026-09-23 item-120 entry), NOT to fix here: the cash-sweep SGOV park sizes off a mid-capable price, and `resolve_live_price`'s today-session-bar acceptance now makes the unconfirmed Alpaca daily-bar-date assumption load-bearing for sizing (fails safe).

**Retired 2026-09-24** — `src/pipeline_stages.py` now computes a separate `risk_sizing_price = sizing_print if is_short else sizing_price` for the `_qty_by_risk_budget` call, proven by a reproduction test in `tests/test_item_181_short_risk_budget_sizing.py` that fails pre-fix and passes post-fix; full writeup in `docs/INCIDENT_HISTORY.md` (2026-09-24 entry). The two adjacent out-of-scope findings above (cash-sweep SGOV park price, `resolve_live_price` daily-bar-date assumption) are NOT closed by this fix and remain open findings, not re-filed here.

