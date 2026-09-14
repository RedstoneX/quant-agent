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


## item 1

**Plain language —** Before placing a trade, the desk checks that the potential reward is large enough compared to the risk. That check compared the two using two different, inconsistent ways of measuring the stop distance, so it wrongly rejected about a quarter of all trade ideas. It also had a loophole: a trade could skip the check if a news story could be cited as justification, and one can be found for almost any well-known name.
**Example —** One live signal came through at a reward-to-risk of 1.28 when the best the arithmetic could possibly produce was 1.29 — the stop had been pushed out to a fixed minimum distance instead of sitting where the chart said, so the trade was refused for failing a bar it was mathematically incapable of clearing. Separately, a trade in a very famous stock was let through below the required ratio because a citable news story was available — something true of nearly every big name.


## item 3

**Plain language —** Sometimes an order sits with the broker but the price moves away before it fills, so the desk cancels it rather than chase a worse price. This is working as designed, but it's a real cost — the idea is lost even though nothing malfunctioned.
**Example —** About one in eleven trade ideas end this way: the limit order waits, the stock drifts off the target price, and the order is cancelled unfilled, with the opportunity gone.


## item 4

**Plain language —** There are two separate reward-to-risk minimums enforced at two different points, using two different numbers, and neither is backed by real research. Both work as designed, but two different rules checking the same thing is confusing, and this one should be merged into the fix already underway for the main floor above.
**Example —** A trade could clear an earlier 1.5-minimum check and still be blocked later by this separate 1.2-minimum check — two different numbers guarding essentially the same question.


## item 8

**Plain language —** A couple of ideas were rejected because the protective stop ended up on the wrong side of the entry price. Confirmed this isn't a bug: the stop is calculated correctly when the idea is created, but the market can move before the order is actually built, making the original stop look wrong by the time it's used.
**Example —** An idea forms when a stock is at $100 with a stop at $98; moments later, by the time the order is assembled, the price has already dropped to $97 — the same stop now sits above the price, and the trade is correctly refused.


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

**One correction to your own note —** it assumed a skipped round costs half
an hour because the desk re-checks every thirty minutes. The thirty-minute
check does not redo the research or the decision; the only thing that can
decide again is a narrow scan that only looks at stocks which have moved 3%
or more that day, at most five of them. On a calm day a refused morning is
closer to a lost day than a lost half-hour. That does not change the rule —
a made-up decision is worse than none — but the price of refusing is higher
than the note assumed.

**The decision —** Still yours, and now the only thing left in this item:
whether *partial* evidence should also stop a decision, and if so, where the
line sits. Not "did the seat answer" — that is settled and built — but "the
seat answered about 40 of 65 companies, is that enough?". Nothing published
answers that, so it either gets a number from you or a ruling that partial
coverage should never stop a decision at all.


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

## item 60

**Plain language —** Two separate things stand between the desk and a bad decision to SELL: an AI reviewer that reads the case and says yes or no, and a set of plain automatic rules that run straight afterwards. This item used to ask whether selling deserved its own purpose-built AI reviewer. Two repairs have since answered the buildable part of that — the reviewer is now given its own instructions for selling, and as of today its own answer sheet, so it is no longer being asked to fill in boxes that mean nothing on a sale and no longer offered two levers that were quietly thrown away. What is left is not a building job. It is a contradiction the item was carrying: it complained that the automatic rules are too easy to slip past, and in the same breath warned that blocking a sale is the dangerous mistake because it leaves a broken position on the books overnight. The first complaint asks for MORE blocking. The second asks for less. Nobody can act on both, which is exactly why this sat unanswered.
**Example —** The full recorded history of this path is three sell-reviews covering eight sell decisions — one on 31 August, two on 1 September. The AI reviewer approved all eight and changed nothing. The automatic rules then blocked all eight. So not one of those sales ever happened, and the only behaviour anyone has actually measured here is the plain rules refusing everything — the opposite of the worry the item was written around. There is a second oddity in the same place: if the AI is completely unavailable, the sale goes ahead unreviewed, which you approved on 27 August for a good reason. But if the AI is working fine and simply words its reason without using one of a short list of recognised words, the sale is blocked. The desk currently trusts a sale more when its reviewer is switched off than when it is switched on and phrasing things unexpectedly.
**Also recorded so it is not proposed again —** "wait until more sales come through and then look" is not an answer here. The desk has no way to test whether different instructions would have produced a better decision, so more approvals under the current instructions tell us nothing; and the handful of cases we would be looking at were themselves chosen by the very rules we would be judging. That is the same self-referential trap that caused the largest known money-losing defect this desk has had.
**The decision —** One question is yours and one is not. Yours: how readily should this desk be willing to block a sale at all? That is appetite for risk, and only you can set it — blocking protects against a hasty exit, and failing to block protects against being stuck in a position whose story has fallen apart. Not yours: which layer should own that refusal, and how to stop one layer failing safe in one direction while the layer beside it fails safe in the other. That is plumbing, and it goes to research, not to you.
**Recommendation —** Answer the plumbing first, because it needs no new data: the two halves already have stated, opposite positions on what to do when something goes wrong, and they cannot both be right. Then look at why the automatic rules refused seven of eight sales for being "too small a move to be meaningful" — that is a question with real evidence behind it, unlike the too-easy-to-slip-past worry, which has none, because nothing has ever slipped past.
**Why only you —** The appetite half decides how much the desk is willing to be trapped in a losing position in order to avoid selling something hastily. That is a money-risk preference, not a technical fact, and it cannot be read off anything.

**Update, 2026-09-14 — one of those four automatic checks turned out to have a hole, and it is now closed. Your decision above is untouched and still open.** Two things were measured rather than assumed. First, the "is this move big enough to be real?" check only ever applies to one kind of sell: 21 of the 26 accepted reasons for selling skip it automatically, and the five that don't are all versions of "the reason I bought this has been proven wrong". So that check exists for that one case and nothing else. Second — on that one case, the desk was not looking at the chart. It already works out whether the price has actually closed beyond the level the stop was resting on, two sessions in a row. That is a fact, and it is exactly what a "proven wrong" sell is claiming. It was being skipped. What stood in its place was a rule of thumb about whether the move was bigger than an average day, which is a different question in different units. The chart answer is now consulted, written down against the stock, and visible to the evening review. **Nothing has been loosened:** no sell that would be refused today gets through because of this, and the check's answer cannot by itself allow or block anything — it is recorded, not acted on. Selling *more* readily when the chart says the level is intact was considered and deliberately left alone: that is a real change to how quick the desk is to sell, and it should be your call made on purpose, not a side effect. Also worth knowing: the "big enough to be real" check has never once changed an outcome — all eight sells it has ever blocked were independently blocked by the next check along anyway.

## item 63

**Plain language —** When a company insider sells shares, the desk wants to know whether that's a real opinion about the stock or just someone raising cash. The best measure is how much of their own pile they sold. The research that measures this found something counter-intuitive: an insider selling a *small* slice of what they hold is actually a mildly *good* sign — they need money, they're keeping the rest, they still like the company. Selling more than half is the only case that reliably means bad news. The desk was doing the opposite of reading that correctly: it treated small sales as meaningless and threw them out of the ranking entirely. That's now fixed — nothing is thrown out, and every insider trade arrives at the analyst carrying how big it was relative to what the person held, plus what the research says that size means. What's still missing is narrower: the desk's internal "how much does this matter" score is a single dial from 0 to 1, and a dial cannot say "this matters, and it points the *other* way." So the analyst reads the direction in the notes, but the automatic ranking underneath it doesn't.
**Example —** An executive holding 100,000 shares sells 1,000 of them. Research says that's worth about +0.68% over the next quarter — a small positive. Another sells 80,000 of 100,000; that's worth about −0.81% — a real negative. Today both arrive at the analyst with the same "importance" score of 1.0, distinguishable only by the written note attached. Before this change the first one scored 0.0 and the analyst never saw it at all.
**The decision —** None needed from you right now, and deliberately so. The obvious move — invent a number that scores the bullish case lower or higher — would be exactly the kind of made-up figure this desk refuses. Two sources were checked for a signed scoring scheme and neither has one. This item exists so the gap is on the record rather than quietly papered over, and it gets picked up when either a published source or enough of the desk's own trading history can settle it.

## item 64

**Plain language —** The desk's practice runs against historical data ask for the same risk on every trade they consider. When the risk ceiling runs out on a busy day, the tie between all those identical requests is broken by ticker spelling — so the practice run funds trades in alphabetical order. Your best-first decision was built into the live desk, and deliberately not into the practice runs: those work off price signals and never produce the analyst ratings the ranking is made of, so there is no ranking to work down, and making up a score to stand in for one is exactly the kind of invented number that keeps causing problems here.

**Example —** A practice-run day where the candidates together want more risk than the ceiling allows: Apple gets funded, Nvidia does not, purely because A comes before N. Nothing about either chart is consulted.

**The decision —** None for you. Either the practice runs learn to score their own candidates off something real, or every practice-run result gets reported alongside how many of its days had the budget running out, so nobody reads a result as evidence about how the desk picks between trades when it isn't.

## item 65

**Plain language —** When the desk decides which stock ideas look best, each of its five specialists contributes two things: which way it leans, and how sure it is. Only the chart specialist actually publishes a "how strongly" number — it has strong-buy and buy as separate ratings. The other four have nothing of the kind, so for a while the desk quietly made one up for them, three different ways. Those made-up numbers are now gone, and those four specialists count purely on how sure they are. That is honest, but it leaves a real question nobody has answered: should those four be able to say "strongly" at all, or is "which way, and how sure" genuinely everything they can tell you?
**Example —** The news specialist reads a headline and says "bearish, low confidence". The chart specialist can say "bearish" or "strongly bearish" — those are two different ratings it publishes. The news specialist has no such distinction available to it. Today the desk takes that at face value and scores the news read on its confidence alone. The alternative is to add a "how strongly" question to what the news specialist is asked, so it has to state one and justify it per story — which is how the chart specialist works.
**The decision —** Do you want the other four specialists asked to rate their own strength, separately from their confidence? It is a change to what each is asked to produce, not a number to pick.
**Recommendation —** Not yet, and not urgent. The current state invents nothing, which is the important part, and the ranking is honestly described as breadth-and-confidence. Adding a strength question to four prompts is cheap to do and expensive to get wrong — every one of them would be a fresh place for a specialist to assert a number nobody can check. Worth revisiting if the ranking ever looks like it is missing an obvious distinction; not worth doing pre-emptively.


## item 70

**Plain language —** The same made-up number, 1.0, is quietly doing two unrelated jobs in the selling path. In one place it decides how far a stock has to move against you before the move counts as real rather than ordinary daily wobble. In the other it decides how tight a stop-loss is allowed to be before the desk refuses it as too close. Both are expressed as "one average day's range", both were picked as a round figure, and neither has a source or a calculation behind it. That they happen to be the same number is a coincidence, not a design — nothing in the code links them, so if either is ever changed the other silently drifts away from it.
**Example —** A stock whose average daily range is $4 has to move $4 against you before the desk stops calling it noise, and separately, its stop is refused if it sits closer than $4 away. Those two rules constrain each other in a way nobody chose, and the reason they line up is that somebody typed 1.0 twice.
**The decision —** Nothing to decide right now. This was found while fixing something else in the same file and deliberately left alone rather than swept into that change. It is here so the duplication is on the record instead of being rediscovered a third time.
**Recommendation —** Treat them as two separate questions, because they are. Each needs either a published measurement of the thing it claims to bound, or a decision that the limit should not exist. Making them one shared constant would be tidier code and no more justified.
