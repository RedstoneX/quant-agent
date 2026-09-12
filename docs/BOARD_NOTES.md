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

The label must start the line, the surrounding `**` is optional, and the
separator may be an em dash, a hyphen or a colon. A block ends at the next
label, the next heading, or a blank line — one paragraph per label. Nothing
is mandatory: an item can carry only a `Plain language` line and nothing
else, and any field left out renders as absent, never guessed.

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

## decision due 2026-09-16

**Plain language —** The seat that actually decides trades runs on a paid AI model, and it drives almost the whole AI bill. The desk wants to compare cheaper models fairly, but the last comparison ran before the seat's own instructions were rewritten, so those numbers are now known to be invalid.
**Example —** This one seat already accounts for roughly 93 cents of every dollar spent on AI in a week. A fair re-test means spending more real money on fresh comparison runs before anyone can honestly compare cost against a cheaper option.
**The decision —** Whether to spend real money now on a fresh, fair model comparison, and only then whether to switch away from the current one.
**Recommendation —** Approve a small, capped spend for one fresh comparison before the due date. Do not switch models based on the old, invalidated numbers.


## item 48

**Plain language —** A check meant to catch a shift in the broader market mood relies on government economic data, which always arrives a couple of days late. The rule used to refuse anything more than one day old, so ordinary reporting lag made it fail almost every time regardless of what was actually happening in markets. Rather than pick a looser day-count, the day-count itself was removed: the check now asks whether the number on hand is the newest one that has actually been published, and separately, whether a newer one was due and never showed up.
**Example —** Inflation and jobs figures only come out once a month. A 20-day-old inflation reading is not stale — it is the only one that exists. The old rule would have refused it anyway for being "too old." The new check asks the right question instead: is this the latest published figure, yes or no.
**Recommendation —** None needed; this is resolved, not a decision. Worth knowing plainly: this was expected to clear roughly half of all runs that were previously refusing to make a call for no real reason, though that hasn't been re-measured against live trading yet since the fix ships forward, not backward.


## item 1

**Plain language —** Before placing a trade, the desk checks that the potential reward is large enough compared to the risk. That check compared the two using two different, inconsistent ways of measuring the stop distance, so it wrongly rejected about a quarter of all trade ideas. It also had a loophole: a trade could skip the check if a news story could be cited as justification, and one can be found for almost any well-known name.
**Example —** One live signal came through at a reward-to-risk of 1.28 when the best the arithmetic could possibly produce was 1.29 — the stop had been pushed out to a fixed minimum distance instead of sitting where the chart said, so the trade was refused for failing a bar it was mathematically incapable of clearing. Separately, a trade in a very famous stock was let through below the required ratio because a citable news story was available — something true of nearly every big name.


## item 2

**Plain language —** About a fifth of all trade ideas once appeared to vanish from the record with no explanation. Investigated fully: none of them actually vanished. Every one had a real, ordinary reason — not enough cash yet, a safety check correctly aborting a plan, a sector limit, a stock with no supporting research that day. The desk explained itself every time; the report reading those explanations back just hadn't been taught to look in the right place yet. That's now fixed.
**Example —** Nine cases looked like an order was being built and then the trail went cold, as if something crashed mid-way. It didn't. All nine traced back to four ordinary, already-logged reasons — the report just wasn't reading that log yet.
**Recommendation —** None needed; this is resolved. One thing worth knowing: the original explanation for one of the four reasons (insufficient cash) was itself corrected — it was never a settlement delay, it was a since-fixed bug where the system counted a sale's proceeds before confirming the sale had actually gone through.


## item 3

**Plain language —** Sometimes an order sits with the broker but the price moves away before it fills, so the desk cancels it rather than chase a worse price. This is working as designed, but it's a real cost — the idea is lost even though nothing malfunctioned.
**Example —** About one in eleven trade ideas end this way: the limit order waits, the stock drifts off the target price, and the order is cancelled unfilled, with the opportunity gone.


## item 4

**Plain language —** There are two separate reward-to-risk minimums enforced at two different points, using two different numbers, and neither is backed by real research. Both work as designed, but two different rules checking the same thing is confusing, and this one should be merged into the fix already underway for the main floor above.
**Example —** A trade could clear an earlier 1.5-minimum check and still be blocked later by this separate 1.2-minimum check — two different numbers guarding essentially the same question.


## item 5

**Plain language —** A trade could get sized down to less than one whole share and effectively disappear. This used to happen for real, but the desk can now buy fractional shares, which already prevents it — the three recorded cases all predate that fix.
**Example —** Sizing a small account's 3% risk into an expensive stock might come out to 0.6 shares; before fractional shares were allowed, that rounded down to zero and the trade silently never happened.


## item 6

**Plain language —** Sometimes there's no clear real price level, like a past high or floor, to set a profit target from, so an idea can't be built into a trade. All three known cases happened within one day of a related change shipping, so it's too soon to tell whether this is a new problem or a coincidence.
**Example —** All three cases so far cluster around a single day right after an unrelated change went live, rather than being spread out — the reason this is flagged as too new to judge yet.


## item 7

**Plain language —** An AI reviewer checks a whole day's trading plan for internal consistency, and if it finds the plan doesn't hang together, it can reject every trade in that plan at once, not just the one that's actually the problem. This has happened again even after an earlier attempt to stop it.
**Example —** One flawed piece of reasoning in one part of a day's plan can throw out every other, unrelated trade idea proposed in that same session — a small problem with an outsized cost.


## item 8

**Plain language —** A couple of ideas were rejected because the protective stop ended up on the wrong side of the entry price. Confirmed this isn't a bug: the stop is calculated correctly when the idea is created, but the market can move before the order is actually built, making the original stop look wrong by the time it's used.
**Example —** An idea forms when a stock is at $100 with a stop at $98; moments later, by the time the order is assembled, the price has already dropped to $97 — the same stop now sits above the price, and the trade is correctly refused.


## item 9

**Plain language —** A handful of trades were rejected for ordinary, sensible reasons: not enough cash available, a broken-looking price quote correctly thrown out, and one outright rejection by the broker itself. None of this needs fixing.
**Example —** One rejected idea involved a price quote that was 14.6% off the reference price the desk expected — a clear sign of bad data, correctly ignored rather than traded on.


## item 10

**Plain language —** The desk kept suggesting the same stock ideas repeatedly even though they never turned into real trades, using up a limited number of trade slots each session. The AI can now see its own track record of which names it keeps proposing and how often they actually go through, but nothing yet stops it from proposing a repeat name again.
**Example —** A stock proposed five times in three weeks with zero fills now shows that history plainly to the decision-maker, but nothing currently blocks a sixth proposal of the same name.
**The decision —** Whether to add a rule that actually blocks or limits re-proposing a name with a poor fill history, rather than only showing the AI its own record.
**Recommendation —** Wait for a few more weeks of real data before setting a hard block; there isn't yet enough evidence to know where a fair cutoff belongs.


## item 11

**Plain language —** On one day, the desk produced zero trade ideas because of outright technical failures, and nobody noticed at the time — total silence looked exactly like a normal quiet market on every screen available. A watchdog has since been built that tracks how often the desk comes back empty and alerts the owner directly when that happens too often to be coincidence.
**Example —** Ten of the fourteen recorded failures happened on a single day, producing a completely empty result indistinguishable, at the time, from an ordinary quiet market — the new watchdog exists specifically to catch a repeat of that day.


## item 12

**Plain language —** The desk's own internal tally of why trades got blocked used to credit the wrong step for a rejection. That bookkeeping error is fixed — a rejection is now counted against whichever step actually killed the idea.
**Example —** An idea dropped by an earlier build step used to be counted as if a later risk review had vetoed it; it's now correctly counted against the step that actually dropped it.


## item 14

**Plain language —** A cost-safety mechanism used to reserve far more money than an AI call actually used, which could stop the desk over money it was never really going to spend. That over-cautious reservation is gone, replaced by a check against real settled cost plus a simple cap on how many calls can happen in one session. That call-count cap is a first real-data number, not yet confirmed after a longer stretch of live use.
**Example —** The old guard held back roughly 2.6 times the money a call was expected to cost, so a handful of calls could look like they'd exhausted a day's whole budget even though only a fraction of it had actually been spent.


## item 15

**Plain language —** The desk couldn't always tell whether a price was current or stale, risking decisions made on outdated information. The piece covering stocks it already owns is fixed — it now honestly says "unknown" freshness instead of pretending a price is live when the broker never actually confirms that. The bigger remaining piece is doing the same for live quotes and historical price data, which needs a choice between two different competing ways to build it.
**Example —** A held stock's price used to be treated as fresh by default even though the broker never confirms when it last updated; it's now correctly labelled freshness "unknown" instead of falsely marked current.
**The decision —** Which of two competing technical approaches to use for tagging whether a live quote or historical price is actually fresh.
**Recommendation —** Have both approaches laid out side by side with trade-offs before this goes to the owner; not enough is settled yet to recommend one over the other.


## item 16

**Plain language —** An unfinished idea to hold back extra spending in the afternoon is no longer relevant. The whole system it would have plugged into was removed when the budget guard above was replaced, so there's nothing left to build.
**Example —** Work toward this was started but never connected to anything; when the surrounding budget system was rebuilt, the piece it belonged to disappeared along with it.


## item 17

**Plain language —** If the desk's own record-keeping breaks, a safety switch can shut down all further AI-based decisions completely, and it stays off until a person manually clears it — working as intended. The real problem, observed live, was that the alert meant to warn someone about it also failed to send, so the desk could sit switched off for a full day or a weekend with nobody aware, looking exactly like an ordinary quiet market.
**Example —** This happened for real: the safety switch tripped because a data file couldn't be opened, and the message meant to warn the owner about it failed to deliver too, so both the shutdown and the warning about it went unnoticed at once.
**The decision —** Whether the desk needs a completely separate backup alert channel, in case its current one goes down too.
**Recommendation —** A cheap backup channel closes a real gap at low cost, but the record disagrees on urgency elsewhere — worth a direct check with the owner on timing before treating this as due soon.


## item 18

**Plain language —** The AI that makes trade decisions was being handed a huge wall of raw earnings-report text before it ever reached the actual list of stocks worth buying — most of what it read wasn't useful for deciding. That's now substantially cut by having the earnings-reading step hand over a short conclusion instead of the full raw extraction, while keeping full detail on file for later review.
**Example —** Before the fix, of about 200,000 characters of text read each session, 70% was raw earnings-filing text and the actual buy-worthy list was buried in under half a percent of it. After the fix, total text roughly halved and the earnings share dropped from about 70% to about a third.
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


## item 25

**Plain language —** The rule that a protected position shouldn't be closed without a genuine reason used to rely purely on the AI's own wording. Now the two most common claimed reasons for exiting are actually checked against real data before being accepted, and a claim that turns out false blocks the exit and raises an alert.
**Example —** If the stated reason for selling is that a price target was already hit, that claim is now checked against real recorded prices; if it's false, the sale is blocked and someone is alerted, instead of the sale going through on the AI's word alone.


## item 28

**Plain language —** An automated test meant to check the desk's cost-safety limit was marked fixed, but checking again, three separate times against a clean copy of the current code, shows it still fails every time. Whatever change was believed to fix it did not, and nobody re-verified the claim before writing it down as solved.
**Example —** The fix was believed to be done because settings the test depended on were removed during an unrelated rewrite; three fresh re-runs since then all fail the same way, meaning the real cause hasn't actually been found yet.


## item 29

**Plain language —** A tool for grading the AI analysts' track record was mistakenly written up as missing work. It had already been built and was already running, so the item was withdrawn.
**Example —** Someone proposed building a scorecard to track how good each AI analyst's calls have been, not realising an equivalent one already existed and was already in use.


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


## item 33

**Plain language —** Two different steps both checked whether a trade's reward justified its risk, but used disconnected methods, so a trade could pass one and fail the other on the very same idea. On a real trading day, the two checks agreed on zero trades between them. Both now share the same underlying calculation, and a separately-found stop-distance rule that had been rejecting almost every realistic trade was also fixed.
**Example —** On two real trading days measured before the fix, the stocks passing the first check and the stocks passing the second check had no overlap at all — not one trade cleared both.
**The decision —** Whether to enable margin, so a tight-stop trade isn't capped at putting 100% of the account into one name.
**Recommendation —** Hold off until the desk's own estimate of what margin actually costs is checked against real broker charges, so the owner can see the true cost before turning it on.


## item 34

**Plain language —** The buffer meant to stop the desk bailing out on ordinary noise was almost exactly as wide as the actual stop-loss, so nearly every real exit was just the stop-loss firing on its own — the smarter exit logic was barely doing anything. Separately, tied ideas were mostly being broken alphabetically by ticker, quietly favouring early-alphabet stocks for no real reason. Both are fixed: the buffer now scales with how long a position has actually been held, and ties break on real reward-to-risk quality instead of the ticker's letter.
**Example —** A stock starting with an early letter could win a tie over an equally-good stock starting with a later letter purely because of the alphabet — that bias is now removed.


## item 35

**Plain language —** Old trading records show a protective stop on one stock being cancelled and replaced with a looser one — a real event, not a display error. This happened during active development, before the account was deliberately wiped clean to start fresh, so the owner chose not to dig into this one old case now, and to simply watch for a repeat once the desk runs on stable, finished code.
**Example —** A stop on a Visa position bought in late August was found cancelled and replaced with a wider, less protective one a few days later, during a period of heavy in-progress changes — not treated as reliable evidence of how the desk behaves today.


## item 36

**Plain language —** The desk now pulls in public records of stock trades by members of Congress, from two free, cross-checked sources, as one more piece of supporting evidence — it can only confirm an idea the desk already has, never generate a new one by itself. Built and live, but currently switched off.
**Example —** If a stock the desk is already considering also shows recent buying by members of Congress, that would count as one more piece of supporting evidence for the trade, once switched on.
**The decision —** Whether to switch on congressional trading data as a supporting signal.
**Recommendation —** Turn it on for a trial period; since it can only confirm existing ideas and never invents new ones, the downside of trying it is limited.


## item 37

**Plain language —** On one night, eleven separate pieces of pending work were all ready to merge into the codebase at once. None conflicted logically, but several touched the same underlying parts, so the order they went in mattered to avoid one change quietly undoing another.
**Example —** Two or three of these changes all edited the same part of the system that handles trade decisions, so merging them in the wrong order could have silently reversed one of the fixes.


## item 38

**Plain language —** When looking for a cluster of company insiders buying stock around the same time, the desk used a 14-day window with no real basis. The published research it was supposedly based on actually defines a cluster as about 2 days, so the window was corrected to match.
**Example —** A set of insider purchases spread across two weeks no longer counts as one meaningful cluster; they now need to fall within about two days of each other to count.
**The decision —** Whether to pursue getting ownership-size data so an insider purchase can be judged relative to how much stock that insider already holds, instead of by a flat dollar amount.
**Recommendation —** Low priority. Flag it and revisit only if a good, free data source for insider holdings turns up.


## item 39

**Plain language —** When the desk's risk budget is nearly maxed out and it can't take a promising new trade, it now shows the AI a direct comparison between its weakest current holding and the strongest new idea being turned away, so it can weigh whether to make room. It only surfaces this comparison — it never automatically swaps one position for another.
**Example —** If the desk is already near its risk ceiling and a strong new opportunity appears, the decision-maker is shown its own weakest current holding side by side with the new idea, but nothing forces a trade either way.


## item 41

**Plain language —** If the broker simply can't return live data for one stock, the scan used to quietly and permanently skip it forever, indistinguishable from that stock just not moving, with no way for the owner to know. Now the desk counts consecutive misses per stock and sends a direct alert once a stock has failed three times in a row, without repeating the alert more than once a day while it stays broken.
**Example —** A stock the broker can never return live data for used to vanish from every scan forever with zero visibility; now, after about three misses in a row (roughly 90 minutes), the owner gets a specific alert naming that stock.


## item 42

**Plain language —** The desk used to repeatedly ask the broker, once a second, whether an order had filled yet, for up to a fixed maximum wait — and that wait had already been stretched twice after real trades were cancelled unfilled while still genuinely working. The desk now watches the broker's real-time fill notifications instead, catching a fill or cancellation the instant it happens, with the old repeated-asking approach kept only as a backup if the real-time connection itself fails.
**Example —** Two real trades were previously cancelled as unfilled purely because they took longer than the fixed wait allowed, even though they were still actively working — the new approach removes that guesswork in the normal case.


## item 43

**Plain language —** A stale insider or Congressional trade older than a week used to be automatically dismissed as merely "historical," regardless of whether it still lined up with what's happening now. The owner's direction: age alone shouldn't decide its weight, agreement with other current evidence should. Real published research backs this — a good share of the value in an insider purchase can still show up weeks later, and major buyout rumours often build months in advance. The lookback window such evidence is even considered from was also widened, from one week to about three months.
**Example —** An insider purchase from two months ago used to be automatically written off as too old to matter; it's now judged instead on whether at least one other current, independent source agrees on the same direction — if it does, it still counts as real support for a trade.



## item 44

**Plain language —** To sell a position it is supposed to keep holding, the desk's AI must name a real reason, and only a short list of reasons is accepted. Two of them — the market regime flipped, or bad news changed the story — are now checked against real data before the sale goes through. The third, "correlation breach", is accepted on the wording alone, because nothing anywhere in the system ever works out whether correlations actually broke. The words are the whole test.
**Example —** The desk holds a position it is meant to keep for another week. The AI writes "exiting on correlation breach" and the sale executes. Had it written "exiting on regime shift" instead, the system would have checked whether the regime really shifted that day and blocked the sale when it had not. The correlation wording is never checked, so it always works. It is the one phrase that reliably opens the door.
**The decision —** Whether to build something that can actually detect a correlation breach, or to stop accepting the phrase as a reason.
**Recommendation —** Stop accepting the phrase. Building a detector means first deciding which correlation, measured over how many days, and how large a change counts as broken — three numbers nobody can derive for you, exactly the position you were in with the drawdown sensitivity. A reason that cannot be checked should not be an accepted reason, and removing it costs nothing because the two checkable reasons still cover a genuine change of circumstances.

## item 45

**Plain language —** Two parts of the desk look for the same thing on a chart: a swing low, meaning a dip with higher prices on both sides of it. One part requires three higher days on each side before it counts. The other requires five. A note written beside the first one claims the two match, so that a swing low means the same thing everywhere. They do not match, and that note has been wrong the whole time.
**Example —** A stock dips on a Monday with four higher days either side of it. The trailing stop counts that as a genuine swing low and ratchets the stop up to sit just under it. The part of the system that finds support levels does not count it at all, because it wanted five. So one half of the desk is protecting a floor that the other half does not believe exists.
**The decision —** Which definition is correct, or whether the two are meant to differ on purpose.
**Recommendation —** Research what published swing-trading work actually uses before choosing, the same way the stop floor was settled. If both turn out defensible, make them the same number and record it as your own dial — what should not survive is two different answers with a note claiming they agree.

## item 46

**Plain language —** When the AI puts a stop near a price level the stock has bounced off before, the desk checks whether the stop is close enough to that level to count as genuinely backed by it. If it counts, the stop stays where it was placed. If it does not, the stop gets pushed wider, which automatically shrinks the trade. The setting that decides "close enough" was justified in writing as covering a one percent zone around the level. Do the arithmetic at this book's own typical daily range and it actually covers about two thirds of that.
**Example —** A stock trades at one hundred dollars with a floor it has bounced off at ninety-seven. The AI places its stop at ninety-six eighty, just under the floor and well inside the one percent zone the written rule says is allowed. Because the real tolerance is narrower than the rule claims, that stop can be judged unbacked and pushed wider anyway — and the position is sized smaller than it should have been, for a reason that only exists on paper.
**The decision —** Whether the setting is wrong or the justification beside it is wrong. They cannot both be right.
**Recommendation —** Treat this as arithmetic rather than taste, because it is. Establish the real width of a level zone from published work first, then set the tolerance from that figure — rather than leaving a number in place and a sentence next to it that the number does not satisfy.

## item 47

**Plain language —** A rule let a single stock be sized up to 100% of the account. The note explaining why said it was safe because the account could not borrow money to buy more than it held in cash. Borrowing had been switched on two days before that note was written. The note stood, describing a safety net that no longer existed, for a week.
**Example —** Luckin Coffee, April 2020: the company admitted it had faked its sales and lost three quarters of its value in a single day. Nothing in any analysis could have shown that coming that morning. If one stock is the entire account and that happens, it costs three quarters of everything in a day. At the new 65% cap, the same disaster costs about half — serious, but survivable, and the desk keeps trading.
**The decision —** Already made. Real industry research found no standard notional concentration limit exists for a desk like this, so a number was derived instead from the desk's own -20% emergency alarm: the largest single bet where even a severe real-world disaster stays under that line came to 33%. The owner reviewed that derivation and set his own number instead — 65% — after confirming that avoiding penny stocks does not remove this risk (all five reference disasters were liquid, well-known companies, not cheap stock).
**Recommendation —** None needed; this is resolved. Worth knowing plainly: at 65%, a severe disaster (about a 60% single-day loss) now exceeds the desk's emergency alarm line rather than staying under it, the way 33% would have guaranteed. That trade-off was made with the real numbers in front of the owner, not by accident.

## item 49

**Plain language —** Removing the rule that was wrongly blocking good trades worked — the desk now finds roughly twice as many trades it's allowed to take. But that rule had been quietly doing a second job nobody noticed: by refusing so many trades, it meant the desk almost never ran out of risk budget. Now it does. On a normal day the desk wants to risk about twice what it's allowed to risk in total, so something has to decide which of the permitted trades actually get the money. Right now nothing decides that deliberately — they're taken in whatever order they happen to come out of the process, which is not a choice anybody made.
**Example —** On the one real day with good records, the desk found 25 trades it was allowed to take. Together they'd risk about 48% of the account, but the ceiling is 25%. So roughly half of them can't happen — and today, which half survives is essentially arbitrary rather than "the best ones."
**The decision —** How should the desk choose between more good trades than it can afford? Four real options: take the best-ranked ones until the money runs out; take all of them but size every one smaller; give the highest-conviction ideas priority; or simply cap how many new trades happen per day.
**Recommendation —** Best-ranked-first is the most defensible starting point — it uses the ranking the desk already computes, and it means the money goes to the strongest ideas rather than whichever happened to be processed first. Sizing everyone smaller sounds fairer but quietly turns every strong idea into a weak one. Worth your judgement though: this genuinely changes what kind of desk this is, so it shouldn't be picked on my say-so alone.
