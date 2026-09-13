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


## item 11

**Plain language —** On one day, the desk produced zero trade ideas because of outright technical failures, and nobody noticed at the time — total silence looked exactly like a normal quiet market on every screen available. A watchdog has since been built that tracks how often the desk comes back empty and alerts the owner directly when that happens too often to be coincidence.
**Example —** Ten of the fourteen recorded failures happened on a single day, producing a completely empty result indistinguishable, at the time, from an ordinary quiet market — the new watchdog exists specifically to catch a repeat of that day.


## item 15

**Plain language —** The desk couldn't always tell whether a price was current or stale, risking decisions made on outdated information. The piece covering stocks it already owns is fixed — it now honestly says "unknown" freshness instead of pretending a price is live when the broker never actually confirms that. The bigger remaining piece is doing the same for live quotes and historical price data, which needs a choice between two different competing ways to build it.
**Example —** A held stock's price used to be treated as fresh by default even though the broker never confirms when it last updated; it's now correctly labelled freshness "unknown" instead of falsely marked current.
**The decision —** Which of two competing technical approaches to use for tagging whether a live quote or historical price is actually fresh.
**Recommendation —** Have both approaches laid out side by side with trade-offs before this goes to the owner; not enough is settled yet to recommend one over the other.


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


## item 28

**Plain language —** An automated test meant to check the desk's cost-safety limit was marked fixed, but checking again, three separate times against a clean copy of the current code, shows it still fails every time. Whatever change was believed to fix it did not, and nobody re-verified the claim before writing it down as solved.
**Example —** The fix was believed to be done because settings the test depended on were removed during an unrelated rewrite; three fresh re-runs since then all fail the same way, meaning the real cause hasn't actually been found yet.


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


## item 39

**Plain language —** When the desk's risk budget is nearly maxed out and it can't take a promising new trade, it now shows the AI a direct comparison between its weakest current holding and the strongest new idea being turned away, so it can weigh whether to make room. It only surfaces this comparison — it never automatically swaps one position for another.
**Example —** If the desk is already near its risk ceiling and a strong new opportunity appears, the decision-maker is shown its own weakest current holding side by side with the new idea, but nothing forces a trade either way.


## item 49

**Plain language —** Removing the rule that was wrongly blocking good trades worked — the desk now finds roughly twice as many trades it's allowed to take. But that rule had been quietly doing a second job nobody noticed: by refusing so many trades, it meant the desk almost never ran out of risk budget. Now it does. On a normal day the desk wants to risk about twice what it's allowed to risk in total, so something has to decide which of the permitted trades actually get the money. Right now nothing decides that deliberately — they're taken in whatever order they happen to come out of the process, which is not a choice anybody made.
**Example —** On the one real day with good records, the desk found 25 trades it was allowed to take. Together they'd risk about 48% of the account, but the ceiling is 25%. So roughly half of them can't happen — and today, which half survives is essentially arbitrary rather than "the best ones."
**The decision —** How should the desk choose between more good trades than it can afford? Four real options: take the best-ranked ones until the money runs out; take all of them but size every one smaller; give the highest-conviction ideas priority; or simply cap how many new trades happen per day.
**Recommendation —** Best-ranked-first is the most defensible starting point — it uses the ranking the desk already computes, and it means the money goes to the strongest ideas rather than whichever happened to be processed first. Sizing everyone smaller sounds fairer but quietly turns every strong idea into a weak one. Worth your judgement though: this genuinely changes what kind of desk this is, so it shouldn't be picked on my say-so alone.
**DECIDED — 2026-09-12, by you.** Best-ranked-first. Your words: *"be ran by the best, why bother with crappy ones if you've got a choice, go with the best."* The money goes to the strongest ideas in order until it runs out; the rest simply don't happen that day, and they are not shrunk down to squeeze in. **Not built yet** — the decision is recorded, the code still spends the budget in arbitrary order. One thing the decision doesn't answer, which will come back to you when it's built: if the next-best idea only half fits in what's left of the budget, does it get taken at half size, or skipped?

## item 53

**Plain language —** The desk buys stocks in fractions of a share, so a position can be, say, 5.3089 shares of Oracle. The broker will only hold a long-lasting protective stop on whole shares; a stop on the fraction has to be a one-day order that expires at the close, and the desk puts a fresh one on each morning. That works as long as the desk is actually running each morning. It has been switched off since 3 September. Nobody realised that switching it off also switched off the morning re-cover, so for six trading days the 0.3089-share slice of Oracle had no stop at all, and every report still described that as a normal overnight state. Nothing lost money this time — the stock went up — but the desk was blind to it. There is now a daily check, at 6:15 each morning, that runs whether or not the desk is switched on: if any position has less stop coverage than shares held AND no session ran the previous trading day to put it back, you get a message with the dollar amount. It only reads; it never places or cancels anything.
**Example —** Oracle: 5.3089 shares at about $150, worth $798. The stop covers 5 shares. The 0.3089 left over is about $46 with no stop. If Oracle gapped down 20% overnight, that slice would lose about $9 before anything could react. Small here — but the same rule applies to a position that is entirely under one share, like a single slice of a $1,500 stock, where the WHOLE position is the uncovered part. You corrected exactly that "it's less than one share so it's negligible" thinking on 2 September, and it still holds.
**The decision —** While the desk is paused, what should happen to the uncovered fraction? Three real options. One: close the fraction now (sell the 0.3089 shares, about $46) and make that the standing rule whenever the desk is paused with fractional holdings. Two: leave it, accept the exposure, and rely on the new daily message to keep you informed. Three: stop buying fractions altogether — you already turned that down on 2 September because it locks a $10,000 account out of the expensive names the analysts keep picking, and nothing about that reasoning has changed.
**Recommendation —** Option one as a rule, not a one-off: pausing the desk is a deliberate act, and it should include tidying the fractions, because the protection design assumes the desk is running. Today that means selling 0.3089 Oracle, which I have NOT done — no order has been placed, changed or cancelled. Option two is honest and now visible, but it means a paused desk carries an exposure nobody is managing. If you want the fraction sold, say so and it gets done by hand; the code shipped here is only the alarm.

