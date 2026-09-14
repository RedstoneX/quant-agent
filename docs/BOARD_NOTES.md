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


## item 10

**Plain language —** This started as "the desk keeps re-suggesting stocks that never actually get bought, using up trade slots." Two parts of that turned out to be wrong, and one real problem was found underneath.

First, there are no trade slots. The desk has no limit on how many positions it can hold — how many it takes is decided by how much risk it can afford, and an idea that never becomes a trade costs no risk at all. So a repeat suggestion was not taking anything away from a fresh one. (Whether a repeat crowds out a better idea on the decision-maker's own shortlist has not been measured, and we are not claiming either way.)

Second, and more useful: when we counted why ideas die, they are overwhelmingly not dying at the market. Of all the ideas that fail, only about one in seven fails because of a real price or broker event. The rest fail inside our own machinery. Under the current code the single biggest cause is an idea that simply never becomes an order at all — no reason recorded, it just stops. That is the real defect, and it is now what this item is about.

**Example —** One stock was suggested eight times, turned down seven times across six different causes — then on the eighth it was bought, at the largest size and the highest confidence in the whole record. A "three strikes and you're out" rule would have killed the desk's best-conviction trade of the period. Another name has two of its three strikes from a pricing bug we fixed the same afternoon — the fix was written for that exact stock. It would have been blacklisted for a fault that no longer exists.

**The decision —** None. This is no longer yours. You said the blocking proposal was a hack rather than a solution and told us to settle it without you; it is settled and closed: **no blocking rule will be added.** Not postponed, not switched off — answered. The reasoning is on file so nobody re-opens it. The short version: how often a stock converts is not a fact about the stock, it is a portrait of our own broken plumbing, so blocking on it means blacklisting a company for our bug. And the list of "repeat offenders" changes completely from one day to the next, so there is nothing stable there to block on anyway.

**Recommendation —** Nothing to approve. What stays open is the machinery defect: most ideas die inside the pipeline without saying why. We shipped reason-recording for exactly that, but it has never been measured because the desk has produced no new ideas since. It gets re-measured as soon as it has, and we will tell you what it says.


## item 17

**Plain language —** If the desk's own record-keeping breaks, a safety switch can shut down all further AI-based decisions completely, and it stays off until a person manually clears it — working as intended. The real problem, observed live, was that the alert meant to warn someone about it also failed to send, so the desk could sit switched off for a full day or a weekend with nobody aware, looking exactly like an ordinary quiet market.
**Example —** This happened for real: the safety switch tripped because a data file couldn't be opened, and the message meant to warn the owner about it failed to deliver too, so both the shutdown and the warning about it went unnoticed at once.
**The decision —** None for you right now. Already decided, 2026-09-03: not now, bigger problems to solve first. No due date; revisit only at your discretion. This line used to describe it as open — it wasn't kept in sync with your own ruling, corrected 2026-09-13.
**Recommendation —** Nothing to approve right now. Bring it back yourself when you want to revisit it.


## item 18

**Plain language —** The AI that makes trade decisions was being handed a huge wall of raw earnings-report text before it ever reached the actual list of stocks worth buying — most of what it read wasn't useful for deciding. That's now substantially cut by having the earnings-reading step hand over a short conclusion instead of the full raw extraction, while keeping full detail on file for later review.
**Example —** Before the fix, of about 200,000 characters of text read each session, 70% was raw earnings-filing text and the actual buy-worthy list was buried in under half a percent of it. That 70% figure has now been re-counted from the real briefing and was exactly right. After the first fix, total text roughly halved and the earnings share dropped to about a third.
**Since then (2026-09-13) —** counting the briefing properly turned up a second, different problem: for 38 of the 65 companies, the analyst had read the filing and reached no view at all, but each still got four lines of space to say so — a fifth of everything the trade-picking AI reads was filler formatted to look like analysis. Same thing in the stock-chart section for 21 stocks with no tradeable setup. Those are one line each now. Nothing was thrown away: every company is still named, with the reason it has no view next to its name. Earnings is no longer the biggest section.
**The decision —** Whether two more pieces of evidence, reward-to-risk and net evidence, should be folded into the scoring system used to rank ideas.
**Recommendation —** Hold off until the reward-to-risk fix above is fully re-measured; folding in a number still being corrected risks baking the same distortion into the ranking.


## item 19

**Plain language —** Given the exact same information twice, the AI gives a strikingly consistent answer, which is useful: the desk can use repeat runs to prove a code change actually reached the AI, potentially skip paying for repeats where the answer never varies, and mathematically correct a known, repeatable bias instead of arguing it away with wording changes. All secondary to the bigger prompt fix already underway elsewhere.
**Example —** Five runs with stock names hidden and five with them shown produced answers identical to four decimal places; a later batch of five runs failed the same check four times out of five, always flagging the same two stock names.


## item 20

**Plain language —** Right now, if part of the research behind a decision is thin or missing, the AI still makes a full buy or sell call anyway. The plan is to check cheaply, before the expensive decision step runs, whether there's actually enough real evidence to decide at all — if not, skip that round loudly and try again well within the hour at the next scheduled check.
**Example —** If most of a day's earnings reports come back flagged as too truncated to read, that fact already exists in the record; the fix would use it to skip that session's decision entirely and announce the skip clearly, instead of letting the AI guess.
**The decision —** Where to set the minimum evidence bar below which the desk skips a decision entirely.
**Recommendation —** This is designed but not yet built. Have a concrete number proposed with its reasoning shown before shipping anything, rather than picking one casually.


## item 30

**Plain language —** The desk allows a bigger position when more of its AI specialists agree: one specialist behind an idea allows 3% of the account at risk, two allows 4%, three or more allows 5%. This item used to ask whether that ladder should start counting some specialists as worth more than others, the way the separate ranking step recently started doing. Reading the code turns it into a different question. Nobody ever derived the ladder's steps from anything — the measurement quoted beside them counted how often each step gets used, not what any step should be. And as things are set up today, four of the five steps cannot actually reduce anything, because they sit at or above limits the desk already enforces elsewhere. Separately, weighting specialists differently in the money-sizing step is already ruled out by a standing rule of yours — a weight there may only come from the desk's own measured track record, which barely exists yet — so that half is not something anyone can just change.
**Example —** The three-, four- and five-specialist steps all allow 5%, which is the desk's absolute per-trade ceiling anyway, so they never bite. The two-specialist step allows 4%, and the decision-maker is already instructed never to ask for more than 4%. Exactly one step — the single-specialist 3% — can ever shrink a position, and only for requests between 3% and 4%.
**The decision —** Not "which weights". Whether a five-step ladder that nobody derived should be pricing position size at all, given four of its steps do nothing.
**Recommendation —** Decide the ladder's existence before its weights. Copying the ranking weights across would mean inventing a rule for looking up a table whose entries were already invented, and it would not remove the mismatch you'd be trying to fix — the two steps disagree about what they are measuring, not just about the numbers.


## item 32

**Plain language —** The desk was approved to risk 5% of the account per trade, but an old never-approved size limit was quietly capping real trades at about 1%. That is fixed, and so is the trade-sizing band question — the wider bands were restored and merged on 10 September, though this board wrongly kept calling them undecided until 13 September. One thing genuinely remains: the desk has two separate loss-alarm systems, one watching how much the account has fallen over a rolling window and one watching how far it is below its best-ever level. They were set up independently and nobody has decided whether the desk should have one loss response or two.
**Example —** The two systems are currently pinned together only by a shared -20% alert point. That is a floor on how far apart they can drift, not evidence that they agree.
**The decision —** Should the two loss-alarm systems be merged into one, or deliberately kept as two?
**Recommendation —** Leave them as two for now. Both work, neither conflicts with the other today, and merging them means re-deciding trip points you have already set once — worth doing when there is live evidence about how each behaves, not before.


## item 35

**Plain language —** Old trading records show a protective stop on one stock being cancelled and replaced with a looser one — a real event, not a display error. This happened during active development, before the account was deliberately wiped clean to start fresh, so the owner chose not to dig into this one old case now, and to simply watch for a repeat once the desk runs on stable, finished code.
**Example —** A stop on a Visa position bought in late August was found cancelled and replaced with a wider, less protective one a few days later, during a period of heavy in-progress changes — not treated as reliable evidence of how the desk behaves today.
**New evidence on the same position, 2026-09-14 —** the archived records show that same Visa position, on the evening of 1 September, trading at $373.70 against a recorded stop of $374.27 — below its own stop, and still held. That is one of two things and the desk's own records cannot tell them apart: either the stop was widened again, which is the recurrence this item was deferred pending, or the stop was never actually live at the broker. **This needs a broker check that nobody has done** — the account's own order history for that stop between 27 August and 1 September. Until that is looked up, neither explanation should be believed over the other.


## item 39

**Plain language —** When the desk has run out of risk budget and has to turn away a good new idea, it now compares that idea against the weakest thing it is already holding. Two different things can happen. If the holding merely ranks lower, the desk only shows the comparison and does nothing. If the holding would flatly not be bought today — it fails the desk's own entry rules, the same rules a brand-new buy has to pass — the desk can now sell it itself to make room. That selling half is switched ON, at your instruction, rather than shipped switched off. It has never actually happened yet: nothing has been running since 3 September, so the first one will also be the first proof it works end to end, and you will get a message the moment it does.

**The safety conditions, all five, all measured from the desk's own records and never from an opinion —** the book must genuinely have no room left; the holding must fail the entry rules today; the reason you were holding it must have ALREADY broken (anything whose original argument is still standing is never sold this way); nothing may be part-done or in mid-flight on it; and the decision-maker must itself have asked to buy the replacement — the desk never invents the buy side. The sale then goes through exactly the same checks as any other sale, including the risk reviewer's power to refuse it. If the sale happens but the replacement purchase then doesn't, you get a second, separate message saying so.

**Example —** On the one real day with good records, the desk found 25 trades it was allowed to take, wanting to risk about 48% of the account against a 25% ceiling. That is the "no room left" condition, measured, not imagined — so on a day like that the door to rotation is open. What comes through it is deliberately narrow: only a holding that fails today's entry rules AND whose original reason for existing has already broken. Nothing has passed all of that yet, so there is no real swap to show you, and I am not going to invent one.

**One thing you should know —** the tier that only ranks things uses a "must be 25% better" bar that is a made-up number. It is honestly labelled as made-up in the code and it decides nothing — it only controls whether a comparison gets printed for the AI to read. The tier that can actually sell doesn't use it at all. It stays a research question, not a decision for you.


## item 52

**Plain language —** The desk watches company insiders buying their own stock, and throws away any purchase below a flat dollar figure — $100,000 for names we already follow, $250,000 for everything else. Nobody knows where those two figures came from; they are not in any research. This was previously written up as a question for you about buying insider-holdings data. That framing was wrong, and it has been corrected: the paper we were leaning on measures a filer's whole quarter added up in one stock, not one purchase at a time, so it cannot tell us where to draw a per-purchase line at all — relative or absolute. No size gate was built on 13 September for exactly that reason, and that restraint was right. One part of the old framing has since been disproved outright: the desk does NOT need to buy insider-holdings data. Every filing it already downloads states how much the person still owned afterwards, so the desk can work out what share of their own stake they traded — and since 13 September it does, on every insider trade, for buys and sells alike, and shows it to the analyst. What is left is a research question, not a decision for you.
**Example —** An officer whose entire stake is worth $200,000 puts another $90,000 in — a huge vote of confidence in proportion to what they already own. The desk discards it for being under $100,000. Meanwhile someone sitting on $80m of stock buys $300,000, barely a rounding error to them, and the desk keeps it. Whether that trade-off costs us anything is genuinely unknown — which is the problem worth fixing, rather than either number itself.
**The decision —** None for you. It stays open as an investigation until either a study measures what a single insider purchase's size actually predicts, or we conclude size should gate nothing and delete both figures.

## item 53

**Plain language —** The desk buys stocks in fractions of a share, so a position can be 5.3089 shares of Oracle. The broker will only hold a long-lasting protective stop on whole shares; a stop on the fraction has to be a one-day order that expires at the close, and something has to put a fresh one on each morning. That something used to be the desk's own morning session, which has been switched off since 3 September — so for six trading days the 0.3089-share slice of Oracle had no stop at all. You said fix it rather than choose between the three options that were sitting here, so those options are gone and this is what was built instead.
**What it now does —** A check that runs every half hour, seven days a week, whether or not the desk is switched on. When it finds shares the broker is not watching AND the market is genuinely open, it puts the protective stop back by itself, at the price your own analysts set when the position was bought. It asks the exchange's own calendar whether the market is open — it never assumes, and if it cannot get an answer it does nothing rather than guess. It can only ADD a protective order: there is no path in it that sells, shrinks or closes anything. If it tries and fails, you get told; that message is separate from the daily exposure message so one cannot hide the other.
**What it does NOT do, and nothing can —** The fraction is still unprotected overnight. This broker will not hold a stop on a fraction of a share past the close, full stop. Every stop this puts back lives for one session and then expires like the last one. So: protected during the day, exposed overnight, on the sub-share slice only. Oracle's slice is about $46 of a $798 position. On a position entirely under one share it would be the whole position — the thing you corrected on 2 September. The only real cures are holding whole shares (you declined that, for good reason: it locks a $10,000 account out of the expensive names) or not holding the fraction.
**Still needed from the box, not from you —** The half-hourly check has to be switched on once on the trading machine before any of this is real. Until then it is code that never runs, which is exactly how the silence alarm sat dead for ten days. The desk's own unit-drift check reports it as undeployed every morning until it is installed, so this cannot be quietly forgotten. Nothing here needs a decision from you.

## item 55

**Plain language —** A "level" is a price the stock has bounced off before, and the desk uses them for almost everything — where to put a stop, whether a trade is worth taking, how big it can be. Three things define one. On 13 September the popular trading-software documentation was read and answered none of them. Later the same day the ACADEMIC work was found, and it changes the picture in three ways. First, it settles one of the three: a level needs at least two bounces, and a study of 733 US stocks over twenty years measured that demanding three or more makes no difference to how often price actually turns there. That number is now sourced rather than assumed, and locked so nobody quietly raises it. Second, it confirms that the desk's whole method — find the bounces, group the ones at similar prices, treat the group as a band — is the same method the academic work uses, so the design is not home-made. Third, on the two numbers still open, it does not give an answer but it does say where the desk is standing: the same study checked band widths from 2% up to 5% and found the results did not change, and the desk's band is 2% — the very tightest they looked at. Nobody has measured anything narrower.

**Example —** On a $200 stock the desk's band is $4 wide. Two bounces $1.90 apart are "the same level"; bounces $2.10 apart are two different levels. That single call decides whether a stop counts as sitting on real structure — and a stop that does gets honoured as-is, while one that does not gets pushed wider, which shrinks the position. So the width is quietly sizing trades, and the desk is running it at the edge of the only range anyone has tested.

**The decision —** None for you. It is a chart-structure question, so it goes to research, not to your judgement. It is on the board so that it gets answered rather than sitting in a code comment forever.

**Recommendation —** There is now a specific, runnable experiment rather than a wish. The academic study's own test — count how often price entering a band leaves the way it came, and compare that against bands drawn at random — has never been run on this desk's own stocks at this desk's own settings. Run it, and sweep the width and the bounce definition across a range. Either the desk's setting shows a real effect, or the effect is flat everywhere, in which case the honest answer is that the width does not matter and this closes. If it is flat, the better prize is still available: drop the percentage entirely and let the band be the actual height of the bars that made the bounces, so the stock states the width and the desk states nothing.

## item 56

**Plain language —** When there is no obvious place on the chart to put a stop, the desk invents one at 2.5 times the stock's average daily range. A trader we cite elsewhere says a stop should never be wider than one day's range — which would rule our own stop out. Rather than settle which of the two is right, a third and looser limit was used: the furthest the stock could plausibly travel over the life of the trade. That limit refuses almost nothing, so in practice the disagreement was stepped around rather than resolved. There is a second unsourced number hiding inside it: the "plausibly travel" figure borrows a multiplier that was written for working out price targets, not for refusing stops.
**Example —** Measured across 101 real names on 13 September, the width limit refused four of them on a three-week trade and none at all on a three-month one. So the safety check is doing almost no work. If the one-day-range rule is the right one, every stop we invent is roughly two and a half times too wide, and every one of those positions is correspondingly too small — a permanent, invisible drag rather than a single bad trade.
**The decision —** None for you. Both candidate rules are published trading doctrine, so this is settled by research or by measurement, not by preference.
**Recommendation —** One measurement answers both halves: how often a stop at each width actually survives a multi-day hold. Past some width, a wider stop stops buying survival and only buys a smaller position — find that point and the argument is over.

## item 57

**Plain language —** The desk has five analyst seats, and it lets a trade take more risk when more of them agree. The schedule is 3% risk with one seat behind it, 4% with two, and 5% with three, four or five. Two problems. The 5% is already the hard ceiling, so the last three rungs narrow nothing — they are decoration. And in the only measured window, no trade ever had more than three seats agreeing, and two thirds had exactly one. So a five-tier design is really a one-tier design in practice. The measurement written beside it in the config is real, but it counts how often each rung gets used; it never says what the risk figure at each rung should be.
**Example —** Two thirds of every trade this desk sizes is capped at 3% because only the technical seat backed it. That single invented number is doing almost all the sizing here — not the 5% envelope you ratified. Nobody can say why it is 3 rather than 2 or 4.
**The decision —** Not yet. If research shows agreement between independent seats does not actually predict anything, the honest answer is to delete the schedule and let the 5% ceiling stand alone — and that would be a real change to how the desk sizes, so it would come back to you then. Right now there is nothing to rule on.
**Recommendation —** Answer the underlying question first: does more agreement mean a better trade? If it cannot be shown, collapse the schedule rather than tune it.

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


## item 66

**Plain language —** The desk scores a stock idea by adding up what each specialist that looked at it says. That means a stock that several specialists happen to be covering right now scores higher than an identical stock only one is covering — which is deliberate, because agreement across independent sources is the desk's whole edge. But coverage comes and goes: a company only has an earnings filing to analyse for a few weeks after it reports. So a stock the desk already owns can quietly score lower a month later purely because its earnings coverage lapsed, with nothing about the company having changed. That matters because the desk uses this same score to ask "should I sell what I hold to make room for something better?"
**Example —** You buy a stock when three specialists like it: the chart, its fresh earnings filing, and confirmed institutional buying. Six weeks later the filing is stale and the institutional flow is old news, so only the chart still covers it. Its score has dropped by roughly a third, purely on coverage. A brand-new idea with three current specialists behind it now clears the "20% better" bar to displace it — even if the original stock is doing exactly what it was bought to do.
**The decision —** Nothing to decide yet, and nothing is broken today: the swap feature is not switched on. This is a note so it is not discovered live. If it turns out to matter, the fix is to compare the two stocks only on the specialists that cover BOTH of them, rather than on their raw totals.
**Recommendation —** Leave it. Switch the swap feature on as planned, and once it has run for a while, count how many swaps happened because the held stock's coverage lapsed rather than because its own signals got worse. That is a real measurement rather than a guess, and it costs nothing but waiting.


## item 68

**Plain language:** when two people edit the job board at the same time, the tool that merges their edits has twice thrown away live items instead of keeping both. Once it deleted five open questions and marked them closed; once two workers happened to give their new findings the same number and it deleted both of them rather than renumbering one; a third time this very item collided on number 65 with another branch's unrelated finding.

**Why it matters:** nothing looks wrong afterwards. The file is tidy, nothing is flagged, and a question that vanished looks exactly like a question that was answered. That is the opposite of the rule that a question stays on the board until it is actually settled.

**The decision:** none for you. This is a tooling repair.


## item 69

**Plain language —** In one archived review the desk's own reviewer reasoned about a Disney position using a "distance to the stop-loss" of 4.5%, while the desk's trade records for that same moment imply 0.4% — more than ten times apart. That figure is not decoration: it is one of the numbers the desk checks a "this position is deteriorating" claim against. A reviewer working from 4.5% thinks the position has comfortable room; the records say it was almost touching its stop.
**Example —** Disney was bought at $108.09 with a stop at $105.80 and was trading at $106.21. That is 0.4% of headroom. The review written at that moment states 4.49%.
**The decision —** None for you yet. Three explanations fit and the archive cannot separate them: the reviewer was handed a different stop than the one on file, it measured from the purchase price instead of the current one, or the number came from nowhere. Guessing between them would be exactly the kind of confident story this desk keeps getting wrong.
**Recommendation —** Settle it with two lookups before treating any of the three as likely: what stop the broker actually had on Disney at that timestamp, and what the reviewer was actually shown in that run. The first needs the account; the second needs the archived logs for that run. Only one archived instance is known, so this is a discrepancy to explain, not yet a pattern.


## item 70

**Plain language —** The same made-up number, 1.0, is quietly doing two unrelated jobs in the selling path. In one place it decides how far a stock has to move against you before the move counts as real rather than ordinary daily wobble. In the other it decides how tight a stop-loss is allowed to be before the desk refuses it as too close. Both are expressed as "one average day's range", both were picked as a round figure, and neither has a source or a calculation behind it. That they happen to be the same number is a coincidence, not a design — nothing in the code links them, so if either is ever changed the other silently drifts away from it.
**Example —** A stock whose average daily range is $4 has to move $4 against you before the desk stops calling it noise, and separately, its stop is refused if it sits closer than $4 away. Those two rules constrain each other in a way nobody chose, and the reason they line up is that somebody typed 1.0 twice.
**The decision —** Nothing to decide right now. This was found while fixing something else in the same file and deliberately left alone rather than swept into that change. It is here so the duplication is on the record instead of being rediscovered a third time.
**Recommendation —** Treat them as two separate questions, because they are. Each needs either a published measurement of the thing it claims to bound, or a decision that the limit should not exist. Making them one shared constant would be tidier code and no more justified.
