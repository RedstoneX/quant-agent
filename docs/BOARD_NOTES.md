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

**Plain language —** The desk kept suggesting the same stock ideas repeatedly even though they never turned into real trades, using up a limited number of trade slots each session. The AI can now see its own track record of which names it keeps proposing and how often they actually go through, but nothing yet stops it from proposing a repeat name again.
**Example —** A stock proposed five times in three weeks with zero fills now shows that history plainly to the decision-maker, but nothing currently blocks a sixth proposal of the same name.
**The decision —** Whether to add a rule that actually blocks or limits re-proposing a name with a poor fill history, rather than only showing the AI its own record.
**Recommendation —** Wait for a few more weeks of real data before setting a hard block; there isn't yet enough evidence to know where a fair cutoff belongs.


## item 15

**Plain language —** The desk couldn't always tell whether a price was current or stale, risking decisions made on outdated information. The piece covering stocks it already owns is fixed — it now honestly says "unknown" freshness instead of pretending a price is live when the broker never actually confirms that. The bigger remaining piece is doing the same for live quotes and historical price data, which needs a choice between two different competing ways to build it.
**Example —** A held stock's price used to be treated as fresh by default even though the broker never confirms when it last updated; it's now correctly labelled freshness "unknown" instead of falsely marked current.
**The decision —** Which of two competing technical approaches to use for tagging whether a live quote or historical price is actually fresh.
**Recommendation —** Have both approaches laid out side by side with trade-offs before this goes to the owner; not enough is settled yet to recommend one over the other.


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


## item 28

**Plain language —** An automated test meant to check the desk's cost-safety limit was marked fixed, but checking again, three separate times against a clean copy of the current code, shows it still fails every time. Whatever change was believed to fix it did not, and nobody re-verified the claim before writing it down as solved.
**Example —** The fix was believed to be done because settings the test depended on were removed during an unrelated rewrite; three fresh re-runs since then all fail the same way, meaning the real cause hasn't actually been found yet.


## item 30

**Plain language —** When ranking which stock ideas look best, the desk recently stopped treating every AI specialist's opinion as equally important and started weighting some more heavily based on research. The same change has deliberately NOT been made to the separate step that decides how much money to put into a trade — doing that properly means redesigning a related scoring scale too, a second real decision, not a copy-paste fix.
**Example —** Two AI specialists can currently count equally toward how big a position gets sized, even though one of them already counts for more when the desk is only ranking which ideas look best — the two steps disagree with each other on whose opinion matters more.
**The decision —** Whether and how to extend the new weighting to the money-sizing step, including how to redesign the scoring scale it would affect.
**Recommendation —** Scope the sizing-path change and the scoring-scale redesign together in one proposal; they can't be decided separately without creating a second mismatch.


## item 31

**Plain language —** Until now, only the technical chart-reading AI's opinion actually counted toward ranking which stock ideas look best; the other four specialists weren't wired in at all. All five now feed into the ranking. One known simplification remains: the economic-outlook specialist gives one broad opinion applied the same way to every stock, rather than adjusting it per sector the way a related calculation elsewhere already does.
**Example —** Previously, a stock could rank highly purely on its price chart even if the news, economic, earnings and institutional-buying specialists all disagreed with it, because only the chart-reading opinion counted toward the score.


## item 32

**Plain language —** The desk was approved to risk 5% of the account on each trade, but an old, never-approved size limit was quietly capping real trades at about 1% instead. That's fixed. Two related points remain open: whether to widen the trade-sizing bands back to their original range now that the bug is fixed, still awaiting the owner's actual sign-off; and whether the desk's two separate loss-alarm systems, one based on account swings and one on peak-to-trough drawdown, should ever be merged into one — nobody has decided that.
**Example —** A trade sized to risk 5% of the account was actually only risking about 1% in practice because of the old cap — a fifth of the approved risk, on every single trade, until fixed.
**The decision —** Restore the wider trade-sizing bands now that the bug is fixed, and separately, whether the two loss-alarm systems should ever be unified.
**Recommendation —** Approve restoring the wider bands; they were only narrowed to compensate for a bug that no longer exists. Leave the two-alarm-system question for later since both work independently without conflicting today.
**Why only you —** Both halves change how much of the account can be put at risk on a trade and how the desk reacts to a loss — risk-appetite calls, not engineering defaults.


## item 35

**Plain language —** Old trading records show a protective stop on one stock being cancelled and replaced with a looser one — a real event, not a display error. This happened during active development, before the account was deliberately wiped clean to start fresh, so the owner chose not to dig into this one old case now, and to simply watch for a repeat once the desk runs on stable, finished code.
**Example —** A stop on a Visa position bought in late August was found cancelled and replaced with a wider, less protective one a few days later, during a period of heavy in-progress changes — not treated as reliable evidence of how the desk behaves today.


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

**Plain language —** The desk buys stocks in fractions of a share, so a position can be, say, 5.3089 shares of Oracle. The broker will only hold a long-lasting protective stop on whole shares; a stop on the fraction has to be a one-day order that expires at the close, and the desk puts a fresh one on each morning. That works as long as the desk is actually running each morning. It has been switched off since 3 September. Nobody realised that switching it off also switched off the morning re-cover, so for six trading days the 0.3089-share slice of Oracle had no stop at all, and every report still described that as a normal overnight state. Nothing lost money this time — the stock went up — but the desk was blind to it. There is now a daily check, at 6:15 each morning, that runs whether or not the desk is switched on: if any position has less stop coverage than shares held AND no session ran the previous trading day to put it back, you get a message with the dollar amount. It only reads; it never places or cancels anything.
**Example —** Oracle: 5.3089 shares at about $150, worth $798. The stop covers 5 shares. The 0.3089 left over is about $46 with no stop. If Oracle gapped down 20% overnight, that slice would lose about $9 before anything could react. Small here — but the same rule applies to a position that is entirely under one share, like a single slice of a $1,500 stock, where the WHOLE position is the uncovered part. You corrected exactly that "it's less than one share so it's negligible" thinking on 2 September, and it still holds.
**The decision —** While the desk is paused, what should happen to the uncovered fraction? Three real options. One: close the fraction now (sell the 0.3089 shares, about $46) and make that the standing rule whenever the desk is paused with fractional holdings. Two: leave it, accept the exposure, and rely on the new daily message to keep you informed. Three: stop buying fractions altogether — you already turned that down on 2 September because it locks a $10,000 account out of the expensive names the analysts keep picking, and nothing about that reasoning has changed.
**Recommendation —** Option one as a rule, not a one-off: pausing the desk is a deliberate act, and it should include tidying the fractions, because the protection design assumes the desk is running. Today that means selling 0.3089 Oracle, which I have NOT done — no order has been placed, changed or cancelled. Option two is honest and now visible, but it means a paused desk carries an exposure nobody is managing. If you want the fraction sold, say so and it gets done by hand; the code shipped here is only the alarm.
**Why only you —** Real money sits with no protective stop right now, and choosing to sell it, accept the exposure, or change the standing rule for a paused desk is a risk-appetite call, not a technical fix.

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

## item 58

**Plain language —** Before the analysts look at a chart, the desk describes it to them — whether the stock has gapped, whether it has been going sideways. Four numbers decide what gets described: a gap must be at least 2% to be mentioned; "going sideways" means a total range under 8% across 15 sessions with a small net move. Every one of those is a round figure someone picked. They are not trading rules and they refuse nothing, which is why this is filed as research rather than a fault — but they shape what every seat is told, so they bias every decision without ever appearing in one.
**Example —** A 2% gap on a sleepy utility is a genuine event. A 2% gap on a high-volatility name is an ordinary Tuesday. Both get reported to the analysts in exactly the same words, and the seats have no way to tell which is which.
**The decision —** None for you. Chart-description question, so it goes to research.
**Recommendation —** Same fix as the level width: replace the flat percentages with the stock's own normal movement, so a gap is "unusual for this name" rather than "over 2%". That needs no number at all and is probably a small job.

## item 59

**Plain language —** After 13 September the desk can no longer go quiet for a day without telling you — every way it can produce nothing now has an alarm, and each kind of empty day has its own distinct wording. What it still cannot do is notice a PATTERN. If a fault caused it to refuse every single idea, every single day, it would report that truthfully each morning and never once raise its voice. Nobody has decided how many identical empty days should set something off, and no number was invented for it.
**Example —** In the measured window, 6 sessions out of 11 placed no trades at all. So a run of empty days is completely normal here, which is exactly what makes a broken run so easy to miss — a fortnight of "no trades today" messages looks the same whether the market is dull or a gate is jammed shut.
**The decision —** Possibly yours later, but not yet. If we can tell a jam from a quiet market by its shape, no decision is needed. Only if that fails does it become a question of how long you are prepared to be flat without being told.
**Recommendation —** Try the shape test first. The desk already writes down WHY each idea was refused; if every refusal for days on end carries the identical reason, that is a jam, and it can be alarmed without counting days at all.

## item 60

**Plain language —** The AI that double-checks trades before they go out also double-checks the desk's decisions to SELL out of a position it already holds — but it was built and tuned only for the morning buying decision, not the selling one. A repair landed today after this reviewer was found telling itself, on every single sell it ever reviewed, that two mandatory safety checks had been skipped — when those checks don't exist for a sell at all and never did. The repair stopped it lying to itself, but it did not give selling its own reviewer. Instead, four of its normal checklist items are now switched off for a sell as not relevant, a fifth is flipped in meaning (a stock about to report earnings is a reason to refuse a purchase, but a reason to get out of a sale), and it turns out two of its three ways of actually intervening on a trade — nudging one position, or scaling back the whole plan — do nothing at all when it's reviewing a sell. All it can really do there is say yes or no to the whole thing.
**Example —** Only three sell-decisions have ever gone through this reviewer in the recorded history, and every one was approved with no changes made. That is not proof the reviewer is doing its job — it is too small a sample to prove anything either way. What is provable is that the handful of automatic checks standing behind it each have a real gap: one of them does nothing unless the position is already flagged as vulnerable; another does nothing unless there is prior data to compare against; a third can be talked past just by citing an outside reason; and the last one only checks that the wording sounds right, not that the claim is true. A confidently-worded, clean-looking, wrong reason to sell could walk through all four untouched.
**The decision —** Should selling get a purpose-built reviewer of its own — its own instructions, its own checklist — instead of the buying reviewer wearing a list of exceptions for it? This matters more than it sounds: a wrong "keep buying" that gets refused costs nothing, but a wrong "don't sell" that gets approved leaves a broken position sitting on the book overnight with only the ordinary stop-loss behind it, not a second layer of judgement.
**Recommendation —** Not made. Today's fix was deliberately the smaller, safer move — make the shared reviewer honest about what it can and can't see on a sell, rather than building it a replacement, while the bigger question of whether selling deserves its own reviewer is put to you rather than assumed either way.
**Why only you —** It decides how much independent scrutiny a decision to sell out of a position is allowed to skip — a risk-appetite call about the desk's own safety net, not an engineering default.

## item 61

**Plain language —** After a test run of the trading system, a summary report prints a line saying how many trades "the portfolio manager proposed." That number turns out not to be tied to whether the portfolio manager actually ran that session — it's counted a different way, and on one real test it printed "1" even though the portfolio manager never ran at all that session. It doesn't affect any real trading; it's a label on an after-the-fact report a person reads to judge whether a rehearsal run behaved the way it should have.
**Example —** In the test used to reproduce a known cost-limit problem, the report said "1" order was proposed by the portfolio manager, when in fact that seat was never called during the run.

## item 63

**Plain language —** When a company insider sells shares, the desk wants to know whether that's a real opinion about the stock or just someone raising cash. The best measure is how much of their own pile they sold. The research that measures this found something counter-intuitive: an insider selling a *small* slice of what they hold is actually a mildly *good* sign — they need money, they're keeping the rest, they still like the company. Selling more than half is the only case that reliably means bad news. The desk was doing the opposite of reading that correctly: it treated small sales as meaningless and threw them out of the ranking entirely. That's now fixed — nothing is thrown out, and every insider trade arrives at the analyst carrying how big it was relative to what the person held, plus what the research says that size means. What's still missing is narrower: the desk's internal "how much does this matter" score is a single dial from 0 to 1, and a dial cannot say "this matters, and it points the *other* way." So the analyst reads the direction in the notes, but the automatic ranking underneath it doesn't.
**Example —** An executive holding 100,000 shares sells 1,000 of them. Research says that's worth about +0.68% over the next quarter — a small positive. Another sells 80,000 of 100,000; that's worth about −0.81% — a real negative. Today both arrive at the analyst with the same "importance" score of 1.0, distinguishable only by the written note attached. Before this change the first one scored 0.0 and the analyst never saw it at all.
**The decision —** None needed from you right now, and deliberately so. The obvious move — invent a number that scores the bullish case lower or higher — would be exactly the kind of made-up figure this desk refuses. Two sources were checked for a signed scoring scheme and neither has one. This item exists so the gap is on the record rather than quietly papered over, and it gets picked up when either a published source or enough of the desk's own trading history can settle it.

## item 64

**Plain language —** The desk's practice runs against historical data ask for the same risk on every trade they consider. When the risk ceiling runs out on a busy day, the tie between all those identical requests is broken by ticker spelling — so the practice run funds trades in alphabetical order. Your best-first decision was built into the live desk, and deliberately not into the practice runs: those work off price signals and never produce the analyst ratings the ranking is made of, so there is no ranking to work down, and making up a score to stand in for one is exactly the kind of invented number that keeps causing problems here.

**Example —** A practice-run day where the candidates together want more risk than the ceiling allows: Apple gets funded, Nvidia does not, purely because A comes before N. Nothing about either chart is consulted.

**The decision —** None for you. Either the practice runs learn to score their own candidates off something real, or every practice-run result gets reported alongside how many of its days had the budget running out, so nobody reads a result as evidence about how the desk picks between trades when it isn't.

## item 65

**Plain language:** when two people edit the job board at the same time, the tool that merges their edits has twice thrown away live items instead of keeping both. Once it deleted five open questions and marked them closed; once two workers happened to give their new findings the same number and it deleted both of them rather than renumbering one.

**Why it matters:** nothing looks wrong afterwards. The file is tidy, nothing is flagged, and a question that vanished looks exactly like a question that was answered. That is the opposite of the rule that a question stays on the board until it is actually settled.

**The decision:** none for you. This is a tooling repair.

