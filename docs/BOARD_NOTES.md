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


## item 18

**Plain language —** The AI that picks the trades reads a long briefing built from every other seat's work. Two separate things were wrong with it. The first was bulk: it opened with a wall of raw earnings-report text before ever reaching the list of stocks worth buying. That is fixed — the earnings step now hands over a short conclusion and keeps the full detail on file. The second was subtler and is fixed as of today: one section of the briefing told the AI to check the economics seat's reasoning for mistakes in logic, but there was no box anywhere in its answer sheet where it could report finding one. It was being asked to do a job and given nowhere to write the answer.
**Example —** Before the first fix, of about 200,000 characters read each session, 70% was raw earnings text and the buy-worthy list was buried in under half a percent of it. That 70% was re-counted from the real briefing and was exactly right. It is now about a fifth of a briefing that is well under half the original size — re-counted again today, from scratch, before anything was changed, so the figure is this desk's own and not a quote.
**Today (2026-09-14) —** the missing box now exists, and the risk-checking seat sees whether it was filled in or left blank. An honest caveat, in your own terms: this is a change to what the AI is *asked*, and this desk has no way to test whether a change in wording makes it decide better — the replay rig would pass either version. So the case for this rests entirely on the plain fact that the box did not exist, which anyone can check, and not on any measured improvement. Nothing was switched off to get there: the option of simply deleting those paragraphs would have dropped the question instead of answering it.
**Also worth knowing —** the briefing measured 1,000-odd characters longer than the last count, and that is a good sign, not a slip: the extra text is the new record of *why* each rejected idea was rejected, which is the opposite of filler.
**The decision —** Whether two more pieces of evidence, reward-to-risk and net evidence, should be folded into the scoring system used to rank ideas.
**Recommendation —** Hold off until the reward-to-risk fix above is fully re-measured; folding in a number still being corrected risks baking the same distortion into the ranking.


## item 19

**Plain language —** Given the exact same information twice, the AI gives a strikingly consistent answer, which is useful: the desk can use repeat runs to prove a code change actually reached the AI, potentially skip paying for repeats where the answer never varies, and mathematically correct a known, repeatable bias instead of arguing it away with wording changes. All secondary to the bigger prompt fix already underway elsewhere.
**Example —** Five runs with stock names hidden and five with them shown produced answers identical to four decimal places; a later batch of five runs failed the same check four times out of five, always flagging the same two stock names.


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

**The decision —** Still yours, and now the only thing left in this item:
whether *partial* evidence should also stop a decision, and if so, where the
line sits. Not "did the seat answer" — that is settled and built, morning
and the later scan alike — but "the seat answered about 40 of 65 companies,
is that enough?". Nothing published answers that, so it either gets a
number from you or a ruling that partial coverage should never stop a
decision at all.


## item 32

**Plain language —** The desk has two separate ways of noticing it is losing money: one watches how much the account has fallen over a rolling window, the other watches how far it is below its best-ever level. They were built independently and had never been written down side by side. That comparison is now done, and the answer is that they do not fight each other — neither can block the other, they cannot both sell the same shares, and neither can be asleep past the point the other acts. What the comparison turned up is that the alarm which goes off soonest was taking the most drastic action of the two: it sold the entire book and abandoned the day, while the mechanism that only triggers after a much worse fall merely halves how much the desk may own. Shallowest trigger, most violent action.

**That has been fixed, later the same day, and you should know what changed —** the "sell everything" response is gone. It had never once run, and had it ever run on the kind of day it was built for it would have made things worse rather than better: the sell orders it placed could not have filled on a sharply falling market, and the way it was written it cancelled every protective stop first and then put them back. So it would have left you briefly unprotected and sold nothing. It filled only on ordinary days — it worked only when it was not needed. The alarm now stops the desk from taking any new risk for the rest of the day, cancels anything waiting to buy, keeps every position, and — this is the important part — asks the broker, one holding at a time, whether each really has a live protective stop on it. If any does not, you get a message naming it. Nothing gets sold.

**Why the checking matters —** a "stop and hold" is only safe if the stops are genuinely there, and the desk cannot answer that from its own records. Earlier the same day the Visa case that looked like a failed stop (item 35) turned out to be the opposite: the stop was live all along and it was our stored *record* of it that was four days out of date. That makes the checking more important, not less — our own notes are not evidence about stop coverage either way, so the check asks the broker directly rather than believing anything we wrote down. Two real reasons the answer can still be "no" survived that same audit: a stop can be moved by a maintenance action outside the desk's own trading code, leaving no trade row behind at all (three were moved that way on 2026-08-31 to bring grandfathered stops up to the minimum distance), and a part-share of a position genuinely cannot keep a protective stop overnight at this broker. So the check is part of stopping, not a report written afterwards. It has three answers, not two — protected, not protected, and *could not be asked*. The third exists because the older version of this check quietly skipped anything it could not read, which would have meant assuming protection at the exact moment that assumption is most dangerous. If a position turns out to have no live stop, the desk first tries to put the stop back at the level your own analysts set when it was bought; if that fails it tells you by name and leaves the position alone. It will not sell it to solve the problem — selling on a bad day is the behaviour being removed.

**Your loss limit did not move.** Two separate measurement mistakes were corrected in the same change, and neither is a change of risk appetite. Cash the desk had parked in a money-market fund was being counted inside "how much does my book normally move in a day" — it was 78% of what that measurement was taken over. Taking it out makes the alarm slightly MORE sensitive, roughly -0.75% to -0.70% of the account: the safe direction, and small because parked cash barely moves. Separately, the alarm was comparing the whole account's daily change against a threshold built only from the shares actually held; those now measure the same thing. No number you have set was touched.

**Example —** A roughly 3% loss in one day on a normal book used to empty the account into cash. Being 20% below the best-ever level — far worse — only cuts the allowed exposure in half. In thirteen days of recorded results the worst single day was -0.46%, against a limit that reconstructs to about -0.7%, so the mechanism that has now been replaced never ran once.

**The decision —** Should the two be merged into one loss response, or deliberately kept as two? The second half of this question — should the "sell everything" response really sit on the shallowest of the alarms — is now ANSWERED and needs nothing from you: there is no "sell everything" response any more.

**Recommendation —** Keep them as two, and the case got stronger. They no longer do the same KIND of thing: one stops the desk, the other trims excess exposure. Two measurements pointing at two different responses is much easier to justify than two measurements racing to sell the same book. Merging still means re-deciding trip points already set once — worth doing when there is real evidence of how each behaves, not before.


## item 39

**Plain language —** When the desk has run out of risk budget and has to turn away a good new idea, it now compares that idea against the weakest thing it is already holding. Two different things can happen. If the holding merely ranks lower, the desk only shows the comparison and does nothing. If the holding would flatly not be bought today — it fails the desk's own entry rules, the same rules a brand-new buy has to pass — the desk can now sell it itself to make room. That selling half is switched ON, at your instruction, rather than shipped switched off. It has never actually happened yet: nothing has been running since 3 September, so the first one will also be the first proof it works end to end, and you will get a message the moment it does.

**The safety conditions, all five, all measured from the desk's own records and never from an opinion —** the book must genuinely have no room left; the holding must fail the entry rules today; the reason you were holding it must have ALREADY broken (anything whose original argument is still standing is never sold this way); nothing may be part-done or in mid-flight on it; and the decision-maker must itself have asked to buy the replacement — the desk never invents the buy side. The sale then goes through exactly the same checks as any other sale, including the risk reviewer's power to refuse it. If the sale happens but the replacement purchase then doesn't, you get a second, separate message saying so.

**Example —** On the one real day with good records, the desk found 25 trades it was allowed to take, wanting to risk about 48% of the account against a 25% ceiling. That is the "no room left" condition, measured, not imagined — so on a day like that the door to rotation is open. What comes through it is deliberately narrow: only a holding that fails today's entry rules AND whose original reason for existing has already broken. Nothing has passed all of that yet, so there is no real swap to show you, and I am not going to invent one.

**One thing you should know —** the tier that only ranks things uses a "must be 25% better" bar that is a made-up number. It is honestly labelled as made-up in the code and it decides nothing — it only controls whether a comparison gets printed for the AI to read. The tier that can actually sell doesn't use it at all. It stays a research question, not a decision for you.


## item 52

**Plain language —** The desk watches company insiders buying their own stock, and throws away any purchase below a flat dollar figure — $100,000 for names we already follow, $250,000 for everything else. Nobody knows where those two figures came from; they are not in any research. This was previously written up as a question for you about buying insider-holdings data. That framing was wrong, and it has been corrected: the paper we were leaning on measures a filer's whole quarter added up in one stock, not one purchase at a time, so it cannot tell us where to draw a per-purchase line at all — relative or absolute. No size gate was built on 13 September for exactly that reason, and that restraint was right. One part of the old framing has since been disproved outright: the desk does NOT need to buy insider-holdings data. Every filing it already downloads states how much the person still owned afterwards, so the desk can work out what share of their own stake they traded — and since 13 September it does, on every insider trade, for buys and sells alike, and shows it to the analyst. What is left is a research question, not a decision for you.
**Example —** An officer whose entire stake is worth $200,000 puts another $90,000 in — a huge vote of confidence in proportion to what they already own. The desk discards it for being under $100,000. Meanwhile someone sitting on $80m of stock buys $300,000, barely a rounding error to them, and the desk keeps it. Whether that trade-off costs us anything is genuinely unknown — which is the problem worth fixing, rather than either number itself.
**The decision —** None for you. It stays open as an investigation until either a study measures what a single insider purchase's size actually predicts, or we conclude size should gate nothing and delete both figures.

## item 55

**Plain language —** A "level" is a price the stock has bounced off before, and the desk uses them for almost everything — where to put a stop, whether a trade is worth taking, how big it can be. Three things define one. On 13 September the popular trading-software documentation was read and answered none of them. Later the same day the ACADEMIC work was found, and it changes the picture in three ways. First, it settles one of the three: a level needs at least two bounces, and a study of 733 US stocks over twenty years measured that demanding three or more makes no difference to how often price actually turns there. That number is now sourced rather than assumed, and locked so nobody quietly raises it. Second, it confirms that the desk's whole method — find the bounces, group the ones at similar prices, treat the group as a band — is the same method the academic work uses, so the design is not home-made. Third, on the two numbers still open, it does not give an answer but it does say where the desk is standing: the same study checked band widths from 2% up to 5% and found the results did not change, and the desk's band is 2% — the very tightest they looked at. Nobody has measured anything narrower.

**Example —** On a $200 stock the desk's band is $4 wide. Two bounces $1.90 apart are "the same level"; bounces $2.10 apart are two different levels. That single call decides whether a stop counts as sitting on real structure — and a stop that does gets honoured as-is, while one that does not gets pushed wider, which shrinks the position. So the width is quietly sizing trades, and the desk is running it at the edge of the only range anyone has tested.

**The decision —** None for you. It is a chart-structure question, so it goes to research, not to your judgement. It is on the board so that it gets answered rather than sitting in a code comment forever.

**Recommendation —** There is now a specific, runnable experiment rather than a wish. The academic study's own test — count how often price entering a band leaves the way it came, and compare that against bands drawn at random — has never been run on this desk's own stocks at this desk's own settings. Run it, and sweep the width and the bounce definition across a range. Either the desk's setting shows a real effect, or the effect is flat everywhere, in which case the honest answer is that the width does not matter and this closes. If it is flat, the better prize is still available: drop the percentage entirely and let the band be the actual height of the bars that made the bounces, so the stock states the width and the desk states nothing.

## item 56

**Plain language —** When there is no obvious place on the chart to put a stop, the desk invents one at 2.5 times the stock's average daily range, and a separate safety check refuses the trade if the stop is too wide. This item was filed on the belief that two published trading rules flatly contradicted each other about how wide is too wide, and that the desk had dodged the argument. On 13 September that turned out to be wrong, and the reason is simple: a stop's width in "average daily ranges" means nothing until you say how long you intend to hold. A stop one day's range away is loose for a three-day trade and very tight for a three-month one. Once both rules are restated as the thing they are really about — the chance the stop actually gets hit before the trade ends — they land in almost the same place: about 36% for the other trader's rule over the few days he holds, about 37% for ours over the three weeks we hold. There was never a contradiction. The genuinely open question is smaller and sharper than the one filed: how unlikely does a stop have to be to hit before it stops counting as a stop at all.

**Example —** The safety check turns out to be almost pure decoration, and now we can say exactly how much. It refuses a stop only when that stop has under a 2% chance of ever being hit during the trade — and, as a matter of arithmetic rather than opinion, it can never refuse the desk's own invented stop at any holding period of three days or more. So the direction of its error is settled: it is not blocking good trades, it is letting wide ones through. That is the safer of the two ways to be wrong, and it means nothing needs to change before trading resumes.

**Also fixed today —** one number was quietly doing two unrelated jobs: working out how far a stock could travel toward a price target, and deciding whether to refuse a trade for a wide stop. Those are now two separate settings with the same value, so nobody can move one and silently move the other. And the desk now records, on every single trade it sizes, the chance that trade's stop gets hit — so the evidence that would settle the remaining question accumulates by itself from here on instead of having to be gone looking for.

**The decision —** None for you, now or before the restart.

**Recommendation —** Two ways out, both real, and the second is not a cop-out: either find a published measurement of how likely a stop should be to get hit, or accept that no such number exists and delete the check entirely — the desk already answers a wide stop by buying fewer shares, which is the response the published literature actually prescribes.

## item 63

**Plain language —** When a company insider sells shares, the desk wants to know whether that's a real opinion about the stock or just someone raising cash. The best measure is how much of their own pile they sold. The research that measures this found something counter-intuitive: an insider selling a *small* slice of what they hold is actually a mildly *good* sign — they need money, they're keeping the rest, they still like the company. Selling more than half is the only case that reliably means bad news. The desk was doing the opposite of reading that correctly: it treated small sales as meaningless and threw them out of the ranking entirely. That's now fixed — nothing is thrown out, and every insider trade arrives at the analyst carrying how big it was relative to what the person held, plus what the research says that size means. What's still missing is narrower: the desk's internal "how much does this matter" score is a single dial from 0 to 1, and a dial cannot say "this matters, and it points the *other* way." So the analyst reads the direction in the notes, but the automatic ranking underneath it doesn't.
**Example —** An executive holding 100,000 shares sells 1,000 of them. Research says that's worth about +0.68% over the next quarter — a small positive. Another sells 80,000 of 100,000; that's worth about −0.81% — a real negative. Today both arrive at the analyst with the same "importance" score of 1.0, distinguishable only by the written note attached. Before this change the first one scored 0.0 and the analyst never saw it at all.
**The decision —** None needed from you right now, and deliberately so. The obvious move — invent a number that scores the bullish case lower or higher — would be exactly the kind of made-up figure this desk refuses. Two sources were checked for a signed scoring scheme and neither has one. This item exists so the gap is on the record rather than quietly papered over, and it gets picked up when either a published source or enough of the desk's own trading history can settle it.

## item 64

**Plain language —** The desk's practice runs against historical data still ask for the same risk on every trade they consider. When the risk ceiling runs out on a busy day, the tie is still broken by ticker spelling. That has not been fixed. What changed is the printout: every practice-run result now says how many of its days the ceiling ran out, and that the tie-break is alphabetical, so nobody reads those numbers as evidence about how the live desk picks among trades. The live desk still spends its budget on the best-ranked ideas first. The practice run still has no ranking, and making up a score to stand in for one is still refused.

**Example —** A practice-run day where the candidates together want more risk than the ceiling allows: Apple gets funded, Nvidia does not, purely because A comes before N. The result now prints that this happened. Nothing about either chart is consulted, and nothing about the live ranking is either.

**The decision —** None for you yet. The reporting half is done. The remaining question is whether practice runs should score their own candidates off something that is actually how the live desk ranks — or whether they simply cannot evaluate rationing. Do not read this item as the ranking having been fixed.

## item 65

**Plain language —** When the desk decides which stock ideas look best, each of its five specialists contributes two things: which way it leans, and how sure it is. Only the chart specialist actually publishes a "how strongly" number — it has strong-buy and buy as separate ratings. The other four have nothing of the kind, so for a while the desk quietly made one up for them, three different ways. Those made-up numbers are now gone, and those four specialists count purely on how sure they are. That is honest, but it leaves a real question nobody has answered: should those four be able to say "strongly" at all, or is "which way, and how sure" genuinely everything they can tell you?
**Example —** The news specialist reads a headline and says "bearish, low confidence". The chart specialist can say "bearish" or "strongly bearish" — those are two different ratings it publishes. The news specialist has no such distinction available to it. Today the desk takes that at face value and scores the news read on its confidence alone. The alternative is to add a "how strongly" question to what the news specialist is asked, so it has to state one and justify it per story — which is how the chart specialist works.
**The decision —** Do you want the other four specialists asked to rate their own strength, separately from their confidence? It is a change to what each is asked to produce, not a number to pick.
**Recommendation —** Not yet, and not urgent. The current state invents nothing, which is the important part, and the ranking is honestly described as breadth-and-confidence. Adding a strength question to four prompts is cheap to do and expensive to get wrong — every one of them would be a fresh place for a specialist to assert a number nobody can check. Worth revisiting if the ranking ever looks like it is missing an obvious distinction; not worth doing pre-emptively.


## item 70

**Plain language —** One made-up number, 1.0, is doing two different jobs in the selling path, and neither job is read off anything. The first job is deciding how far a stock has to move against you before the move counts as real rather than ordinary daily wobble. The second is deciding how tight a stop-loss is allowed to be before the desk refuses it as too close. Both are expressed as "one average day's range". That they are the same figure is a coincidence — nothing ties them — so changing one would not change the other, and changing neither is not a source. The first job is also the only measured over-refusal on this path: of eight proposed sales the reviewer approved, seven were blocked as "too small a move". Closing the plumbing next door did not answer why.
**Example —** A stock whose average daily range is $4 has to move $4 against you before the desk stops calling it noise, and separately, its stop is refused if it sits closer than $4 away. Those two rules constrain each other in a way nobody chose, because somebody typed 1.0 twice.
**The decision —** None for you on the number yet. How readily the desk should block a sale at all is still yours and is not this item — this item is only the two unsourced 1.0s. Each stays open until it has either a published measurement of the quantity it bounds, or a decision to derive one from the other as a single named constant.
**Recommendation —** Keep them as two questions. Do not retune either number to make sales easier or harder — that would be picking an appetite figure. Do not collapse them into one shared constant just because the digits match. Search for a published measurement of each quantity, or name a single derivation that produces both; until then, leave the figure where it is.

## item 74

**Plain language —** The desk is meant to stop itself cutting the same holding twice in a day. It still can, if the reason is a genuine one — a midday cut on bad earnings can be followed by a second cut at the close on those same earnings. The instructions the reviewer reads do say this is allowed. What is not settled is whether it should be.
**Example —** A holding is trimmed at midday on a poor earnings report. At the close the reviewer reads the same report again and trims again. One piece of news, two cuts.
**The decision —** None for you yet. There is a fair case both ways: a second look at the same report can genuinely find it worse, and forbidding that would be its own mistake. It is on the board so it gets thought through rather than left as a warning in the log.
**Recommendation —** Decide whether a reason is used up once it has been acted on that day, and if not, what separates a worse reading from the same reading used twice.

## item 75

**Plain language —** When the desk bought Oracle on 2 September, the chart analyst set a profit target of $159.52 — a price Oracle had already failed at twice. Every seat saw that number: the portfolio manager used it to justify buying, the risk manager saw it, and the position reviewer was shown it every session with the words "soft — you manage exit". But nothing ever used it to sell. The reviewer is told it manages the exit, while its sell rules refuse "taking profit" as a reason, and the "past target" warning only appears at 150% of the way there. Oracle traded above the target on 4 and 8 September. Selling at target would have made about 9%; the desk would have ended slightly below its purchase price.
**Example —** A target is written down before the trade opens, used to say the trade is worth taking, then ignored for the rest of the trade's life.
**The decision —** You asked for each position's target to be shown on the Mission Control chart, and for using it to be debated rather than dropped. Paper-trading "sell at target" for a week was argued against: too few trades to tell luck from skill, one market mood, and it would muddy the restart with a new manager model at the same time.
**Recommendation —** Show the target on the chart. Then track, without placing orders, what four exit rules would have done on every trade — sell all at target; sell half at target and trail the rest; hitting target tightens the trailing stop instead of selling; and today's desk — with the rules fixed before anyone looks and no tuning afterwards. Nothing is built on one trade.

## item 78

**Plain language —** You locked a standing rule: if something the desk needs is missing, find why and make that step actually produce it. Do not invent the missing words. Do not make "drop this name and trade the rest" the standing answer. The current case is a blank "I'll sell if". A temporary patch currently drops that name after we already asked twice, so one blank cannot veto the rest of the book. That patch is not the fix. The real path is: the seats write a real "I'll sell if" before a buy or short can be ticketed; a sentence the model already wrote is put back if a later wipe blanked it; the seat is asked once more; never invent the words. If it is still blank, that name is refused. A catalyst note stays optional.
**Example —** A buy on a chip stock arrives with prices and a stop but the "I'll sell if" box is empty. The desk does not make up a sentence, does not let that blank name veto the rest of the book, and does not ticket it. After one re-ask still blank, that name is refused and the others can proceed. The standing design is that the box is filled, not that the name is dropped.
**The decision —** You locked the standing rule. The temporary drop-the-name patch stays until a live session proves the seats actually fill the box. The rule is not only about "I'll sell if" — any missing required field is the same class of defect.
**Recommendation —** Keep the never-blank path. Keep the drop-the-name patch labelled temporary. Do not treat skip-and-continue as the product.



## item 79

**Plain language —** The desk has a guard against a typo in a price — a "fat finger" check that refuses anything more than 20% away from the market. It is being applied to the stop-loss price as well as the buy price, and on 17 September it threw away a perfectly good short on FLNC after the desk had already paid for the whole analysis. FLNC moves about 9.6% on a normal day, so a flat 20% cap forbids any stop wider than about two normal days — a volatility question answered with a made-up number.
**Example —** A name that swings 10% a day needs a stop further away than a name that swings 1%. One flat percentage cannot serve both.
**Recommendation —** Apply the typo guard only to the price we are buying or selling at, which is where a typo lands. Do not swap 20% for another invented number. Separately, when a refusal is shown to you it must print the name's normal daily range beside the percentage, or a correct refusal reads like a bug.

## item 86

**Plain language —** The live feed that tells the desk instantly when an order has filled has never once worked. The trading process deliberately holds a fake key; a local helper swaps in the real one for ordinary requests, but the live feed does not go through that helper — it dials the broker directly and offers the fake key. Two separate things block a quick fix: the library the desk uses cannot be pointed at that helper at all, and the broker checks the key inside the conversation rather than in the connection header, which the helper cannot reach.
**Example —** Five rounds of work were spent making this path faster before anyone checked whether it had ever worked.
**Nothing is at risk —** Orders are placed over the ordinary connection, which works, and fills are detected by asking the broker every few seconds instead. The feed is now switched off. The only loss is a few seconds of speed. Today's failure count was about 45, not the 147 first quoted — that figure counted log lines, several per failure.
**The decision — yours, and nobody builds any of it without you.** Five options, best fit first: (1) have the machine hold the key in an encrypted store — **this turns out NOT to be possible on this machine** (the service cannot read the decryption key, there is no security chip, and the installed system software lacks the feature); an earlier answer of "encrypted and tied to the machine" was wrong. (2) A small local relay that holds the real key and rewrites the login message: the only option that keeps the key out of the trading process, but it is custom credential-handling code, which this project has previously refused. (3) Get the helper taught to do this properly, upstream: correct, does not exist, slow. (4) Make the library able to use the helper: does not fix the login problem on its own. (5) Leave the feed off and keep asking the broker — no credential change at all, costs a few seconds of fill latency, and stops about 150 error lines a day.
**Recommendation —** Option 5 today, since it is already in place and costs almost nothing. What remains achievable for protecting the key on this machine is file-permission protection, not encryption.

## item 87

**Plain language —** When the book gets too big relative to the account, the desk automatically trims it. That is now the only thing on the desk that sells by itself. Nobody has ever checked whether it cancels the protective stop-losses first in order to free the shares — and if it cancels them and then fails to sell, the holdings are left with no protection at all. That exact flaw is why the older "big loss today" liquidation was deleted on 14 September.
**Recommendation —** Audit it before changing anything. Find out what it actually does, then decide.

## item 89

**Plain language —** An audit on 17 September of everything the desk sends you found nineteen defects. Six of them can mislead you into a decision. Thirteen are clarity problems — the message is correct but hard or impossible to act on. You are choosing which get fixed.
**The six that can mislead you —**
  1. You are told you hold a name that was sold that same morning.
  2. A message asserts every thesis is intact, and then lists three that are missing.
  3. "Thesis unavailable" is shown for any position held overnight. The reason IS in the records; the lookup only searches today.
  4. A rule is cited to you by number, and the rule at that number says the opposite of what it is cited for.
  5. Raw internal error text is passed through to you word for word.
  6. A whole trade plan was discarded over two tenths of a percentage point, and no message was sent at all.
**The thirteen clarity defects —** bare ticker symbols with no company name after the twelfth name in a list; blocked trades explained in jargon and prices rather than in words; a missing broker reason on a rejection; internal status codes shown as-is; percentages with no denominator, so you cannot tell percent of what; a reward-to-risk figure with no unit; detail truncated mid-sentence; a "TRADED" header on a run that only sold; a "data degraded" warning that names internal components; run identifiers; and an unscaled risk rating.
**Recommendation —** Fix the six first; they are the ones that can cost money. The thirteen are worth doing but nothing turns on them.
**Progress on the six, 18 September — four fixed, one could not be found, one left alone.** Nothing about what the desk trades, or when, or how much, was touched; this is about what you are told.
  - **Fixed — a whole trade plan discarded with no message (the dangerous one).** The desk had already written down, for every plan it dropped, which name it was and why, in plain words. Nothing ever read those notes, so a session could end reading "orders: 0" with no explanation and you had no way to know a decision had been made. Your messages now carry them. The reason you will see in the case that was found reads: "the desk decided to open this but the position it asked for was 0.20% of the account, and the desk does not place a new trade smaller than 0.50% of the account. The whole plan for this name was dropped on size alone — nothing was judged wrong with the idea. Nothing already held was touched." **The 0.50% is not a sourced number** — it is written down in one place in the code, appears in no settings file and no document, was never ratified, and the note beside it justifies a different figure entirely. It has NOT been changed: what size is too small to trade is your call, and it belongs with the other thirty unsourced numbers.
  - **Fixed — "thesis unavailable" on anything held overnight.** The reason was always on file. The lookup that fetched it only searched the current day, so everything bought before today came back empty and the seat that reviews your positions wrote "unavailable" into its notes, which is what reached you. It now reads the entry record whatever its date, using the same lookup the evening review and the cockpit's Why tab already use. Deliberately limited to the REASON: the same record also holds the stop and the target as they stood on the day of purchase, and those must keep coming from live figures, not from history.
  - **Fixed — a message asserting one thing and then listing another.** The position-review message stated a number of holdings taken before that session's own selling, above a list of holdings taken after it. On any day something was sold the two disagreed and you could not tell which was your book. The number is now counted off the list printed beneath it, so they cannot differ; when the book changed during the session, it says so.
  - **Fixed — a rule cited by number that said the opposite.** When a trade was refused because the evidence did not net out in favour of it, you were pointed at a numbered section of the design document. That section is about agreement earning a LARGER position, says nothing about refusing anything, and the part it does say was retired on 14 September. The rule is now written out in the message instead of pointed at. That is the real fix: a section number is a promise that another document still says a particular thing, and nothing checked that promise, so it could go stale again silently.
  - **Fixed — raw internal error text.** Internal code-words and broker status tokens no longer reach you; the plain-English wording for them already existed and one line was skipping it. Where a fault message is genuinely the only record of what broke it is kept, but labelled as machine text with a note that there is nothing in it for you to do — inventing a friendly paraphrase of a fault would be inventing a fact. A token nobody has plain wording for is reported as exactly that, never guessed at.
  - **Could not be found — being told you hold a name sold that morning.** Every message that names your holdings reads one table, and that table is refreshed from the broker at the start of every session, after every order, and on every half-hourly check, so it is never more than half an hour behind. We could not produce the message from the code and have not fixed a defect we cannot reproduce. The board's own note on the discarded stop-out reconciliation (item 101) points at the likeliest mechanism and is still open. If you have the message, it would settle this in one look.
**Progress, 18 September —** The evening message was redesigned against the owner's own review of the live 17 September copy. Three of the thirteen are now fixed THERE (run identifiers, internal status codes, the unscaled risk rating) and remain open in the other messages. Also removed from the evening message: the provider-request count, and the nightly "overnight fractional unprotected by design" line, which now speaks only when a holding of under one whole share has nothing protecting it overnight. The near-zero cost was verified truthful, not broken — every seat the evening session runs is on a free model and the one expensive seat does not run in the evening — and now renders as a sentence. Two things the desk knew and never said were added: which holdings sit within one ordinary day's move of their stop, and which report earnings inside the three-session window.

## item 90

**Plain language —** About thirty numbers that govern real trades were never read off anything — they were chosen because they sounded sensible. They were catalogued on 11 September and then filed as "an inventory, not a job", with a note saying never to re-audit. Nothing was assigned, nothing had a date, and a week later all thirty were still live. There is no mechanical check of any kind that would catch the next one.
**Recommendation —** Two halves. Read each number off the instrument it is meant to describe. And build a check that fails the build the next time an unsourced trading number is added, so this cannot happen again by filing.

## item 91

**Plain language —** The desk counts how long it has held something in calendar days, but the rules that read that number expect trading days. A weekend therefore makes a holding look two days older than it is, against every rule about how long a trade should take.
**Verified 2026-09-18, and it is worse than the general case —** the position reviewer's own pace check already has the right number sitting next to the wrong one. A weekend-aware trading-session count is computed a few lines above the pace math and used correctly elsewhere in the same file (widening the noise band). The pace math itself reads the calendar-day count instead, so the exact fix this item asks for already exists in scope and is simply not being read.

## item 92

**Plain language —** The "we have lost too much today" alarm compares today's loss against how much the book would normally move. If a holding's normal movement cannot be measured, that holding is left out of the sum — so the book looks calmer than it is and the alarm trips earlier than it was designed to.
**Correction you should have —** This alarm does NOT sell anything. The selling version was deleted on 14 September and replaced with a halt: it stops new risk, cancels resting entry orders, checks every holding still has its stop, and alerts. An earlier answer saying it sells everything was read off an out-of-date comment in the code and was wrong.
**Recommendation —** Treat a holding whose movement cannot be measured as normally volatile, rather than dropping it. That needs no new number.

## item 93

**Plain language —** The file that records what has gone wrong and been fixed is merged automatically when two sessions edit it at once. Eleven entries written since 2 September use the wrong heading style, so the merge tool cannot see them, and branches collide over nothing.
**Recommendation —** Fix it with the tool's own machinery and add a check. Never by hand — hand-editing that file is how live items were once deleted.

## item 95

**Plain language — your decision.** The account can borrow. Today the portfolio manager is not even shown that, which is a separate defect. Once it is shown, the question is whether it may PLAN to spend borrowed money. Borrowing costs about 6.25% a year on the borrowed balance, so anything bought with it has to beat 6.25% just to break even, not zero.
**Recommendation —** None until you rule. Nobody builds it either way.

## item 96

**Plain language — your decision.** The risk manager can approve a sale whose stated reason is provably false against the desk's own records — nothing checks the reason before the sale goes through.
**Recommendation —** None until you rule.

## item 97

**Plain language — your decision.** The desk judges whether a trade is moving too slowly against a holding period the model simply states rather than reads off anything. That is the kind of unverifiable number the desk has already banned from sizing trades; whether it may stay in this one test is your call.
**Recommendation —** None until you rule.

## item 98

**Plain language —** A review of the prompts that brief the decision-making seats (the ones that actually pick and size trades) found seventeen statements in those briefings that are flatly wrong about what the code does today. Eight of the seventeen can change which trade happens or how big it is.
**The eight that can change a trade —**
  1. A sizing rule tied to a seat-counting scheme that was retired is still capping position size.
  2. The briefing says the macro view never counts toward whether other seats agree; the actual rule counts it as plus-or-minus-one, and that difference killed a real trade on 17 September.
  3. A 75% sector limit is described as a hard wall; it is really advisory and can go as high as 90%.
  4. A sizing formula is described as mandatory that multiplies by a number deleted from the code 1 September.
  5. The risk manager is briefed on the wrong quantity when it checks position size.
  6. A minimum reward-to-risk ratio is described as required; it was abolished.
  7. Reasons the executor gives for exiting a trade are silently thrown away rather than recorded.
  8. Any position held overnight shows "thesis unavailable" because the lookup that fetches the reason for holding it only searches today — the reason exists, it just isn't found. Same defect as item 89's clarity issue #3, but here it affects the decision, not just the message to you.
**The other nine —** clarity-only mismatches between what the prompt claims and what the code does; listed in the audit, not trade-affecting on their own.
**In progress —** PRs #464 (a prompt-drift check) and #467 (fixes sixteen of the seventeen), stacked, not yet merged; confirm which one is left out and why before treating this item as closed.
**Recommendation —** Fix the eight trade-affecting ones first, in the order above (the agreement-ladder and sector-cap ones are the most likely to have already cost or blocked a trade). Not yet placed in your priority order — flagging so it doesn't get lost.

## item 99

**Plain language —** A second review, of the prompts that brief the analysts (the seats that read the market and write reports, one layer below the decision-makers), found the prompts are full of numbers and claims nothing in the code actually enforces.
**What it found —**
  - About 55 numbers exist only as text in a prompt, with no code checking or producing them.
  - About 20 claims about how markets behave are stated as fact with no source.
  - The technical analyst is told it gets 20 days of price history; it actually gets 40, plus five whole categories of data the prompt never mentions it has.
  - The evening report's briefing hasn't been touched since before this project started, and still describes a completely different strategy — a 77-symbol quarterly value approach — while the technical seat's own briefing describes a 5-to-15-day swing-trading window. Both feed the same decision seat. They cannot both be the desk's real strategy — **this is now a pending decision for you, in `docs/WORK.md`.**
  - Roughly a third of the portfolio manager's briefing, and a quarter of the risk manager's and the position reviewer's, is prose describing machinery the model doesn't actually use. That dead weight is where almost every stale or wrong claim above was found living.
  - Separately: a check already exists that fills prompts with numbers straight from the code so they can't go stale, but it only covers 2 of the 10 prompt files. Scanning prompt text for suspicious numbers doesn't work either — there are about 1,825 numbers in there, mostly just dates and list numbering. Neither of the two confirmed mistakes above (item 98) was even sitting in a prompt file — both were assembled by Python code into a string. What would actually have caught the worst one: when code that a prompt describes gets deleted, search the prompts for its name at that moment.
**Recommendation —** Decide the evening-vs-technical mandate question first (it changes what "fix the prompt" even means); then strip the dead weight, since that's where the false statements cluster; build the deletion-site check as ongoing insurance rather than trying to scan for numbers. Not yet placed in your priority order.

## item 100

**Plain language —** Three pieces of cockpit and reporting work are already in progress, and your instructions for each are recorded here so they survive if the session restarts.
  1. **Cockpit panel scrolling.** Panels can be dragged around the screen, so a scrolling rule tied to "top of screen" or "bottom of screen" breaks the moment a panel moves. Scroll behaviour should belong to each panel itself. Holdings and Positions scroll internally; the tabbed detail panels should grow to fit their content and let the whole page scroll instead.
  2. **"Why do we hold this" view.** When you click through to see why a position is held, the fix replaces what's already there rather than adding a new panel: one plain sentence at the top giving the real reason with actual numbers in it, then the rest of the decision-relevant detail written for a human to read, with no fixed line count — you rejected a "keep it to four lines" rule; the test is whether something is relevant, not how long it is. Internal ID numbers get tucked behind a toggle rather than shown up front.
  3. **Evening report.** Drop run identifiers and provider-request counts (nobody-facing plumbing); state the LLM cost in a sentence instead of a row of zeros; only mention "overnight fractional position unprotected by design" when something is actually abnormal that night, not every night; put today's and total profit/loss at the very top; keep the winners and underwater-positions lists; rewrite the risk/bias section so it reads in plain language.
**Recommendation —** No action needed from you; recorded so the in-progress work has something to be checked against when it lands.
## item 104

**Plain language —** The three seats that actually pick and size your trades are each given a written brief, in plain English, describing the desk's rules. Seventeen statements in those briefs are no longer true — the code underneath them was changed and nobody changed the brief. Eight of the seventeen can change a trade. One of them killed a name on 17 September: the brief tells the seat that the macro view never counts toward how many sources agree, while the code counts it as a vote either way.
**How confident to be —** Two were re-checked independently against the code on 18 September and both held up: the brief calls 75% "the sector limit" when the code treats 75% as a soft warning and 90% as the real ceiling, and the technical seat is told it is being shown the last 20 days of prices when it is actually shown 40.
**Why it kept happening —** Nothing ties the English in a brief to the code it describes, so prose rots silently. Treat a brief as code that can go stale.
**Recommendation —** Two pieces of work are already open and stacked, and between them they correct sixteen of the seventeen. Before anyone calls this finished, confirm which one is being left out and why.

## item 105

**Plain language —** The analyst seats' briefs contain about 55 numbers that exist nowhere but in the brief itself — no code behind them, nothing that checks them. The desk already bans numbers that were invented rather than read off real data; these are exactly that, and they were invisible because nobody had looked in the briefs. About 20 further statements about how markets behave are asserted with no source at all. Roughly a third of the portfolio manager's brief, and a quarter of two others', describes machinery the model does not actually operate — and that dead prose is where nearly every stale claim in item 104 was hiding.
**One seat is a bigger question than the rest —** The evening seat's brief has not been touched since before this project began and still describes a hand-picked 77-stock value book held over quarters. The technical seat's brief describes a 5 to 15 day swing book. Both advise the same decision seat. They cannot both be right, and choosing between them is your call, not ours — it is now a dated decision on the board.
**What was ruled out, so nobody rebuilds it —** Automatically scanning the briefs for numbers does not work: there are about 1,825 number-like tokens in them and most are dates and list numbering. More importantly, neither of the two confirmed cases in item 104 was even in a brief file — both were text the code assembles as it runs, so a file scanner would have caught neither.
**Recommendation —** Build the check at the point of DELETION instead: when a mechanism is removed from the code, search for its name across every brief and every assembled string. That would have caught the worst case; nothing else proposed would.

## item 106

**Plain language — these are your own instructions, written down so the work lands against them.** Three pieces of work are in flight and each must match what you asked for, not an agent's taste: how the dashboard panels scroll, what the "why do we hold this" view shows, and how the evening report reads.
**One requirement is not yet in any work at all —** You asked that when the reason to hold a stock rests on an insider or institutional purchase, the view show the DATE and the PRICE of that purchase — your example was Republic Services and when Cascade Investment actually bought. Somebody is working on it, but it is not committed anywhere yet, so it is recorded here as a requirement rather than as done.
**The blocker on the rest of (b) is gone as of tonight —** the read-only endpoint behind this view is merged, and it was held back only until the panel-layout work landed; that has now also merged. Nothing stands between this and a working view: fetch the endpoint when a held symbol is opened, show its one-sentence reason and its labelled detail up front, and put the machine identifiers and the existing step-by-step trace behind one toggle. Nothing else about the page changes.

## item 101

**Plain language —** Twice a session the desk works out something important and then throws the answer away. One check finds exits the broker made on its own that the desk's books never recorded; the other counts positions it has just put protection back onto. Both compute the answer and then discard it at every one of the five places they are called. The answers do reach the log file, so this is not invisible — but they can never reach a Telegram message, a session summary, or anything that would actually tell you.
**How we know it is wrong rather than deliberate —** The sister check sitting on the very next line does the opposite: it keeps its answer and carries it through to the session summary, which is how a missing stop reaches you today.
**Recommendation —** Carry both answers through the same way the stop-coverage check already does. Do this alongside the Telegram work in item 89 — the "you were told you hold a name you sold that morning" defect is the same information going missing.

## item 102

**Plain language —** If an order fills only part-way and stays open, the desk never writes down how much of it filled. That is fine while the order eventually finishes, because the next pass picks it up. It stops being fine in a case the code itself already warns about: the broker deletes its order history after a few days, and after that an unrecorded order gets treated as having filled completely. So a half-filled order can end up counted as a whole one, with the cash and position figures wrong behind it.
**Age —** This is not new and not from tonight's changes; it has been true at every one of these checks since they were written.
**Recommendation —** Decide what a part-fill should record, then make that branch write it instead of skipping.

## item 103

**Plain language — your decision, and we are waiting on it.** You asked for the internal scrollbars to come off the detail panels, and they have. The consequence is that the Trades tab, which holds your full live trade history, now makes the page about 6,000 pixels tall. That is the cost of the change, not a fault, and moving to any other tab puts it back to normal. You were offered that one tab's scrollbar back and have not answered, so nothing has been changed.
**Recommendation —** None until you rule. Your ruling was that scrolling belongs to a panel rather than to a position on the screen, so making one tab an exception is yours to decide, not ours.
