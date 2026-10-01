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

## item 18 — RETIRED 2026-09-30, the prompt-bulk defect it was opened for is resolved and re-measured; its three unrelated residuals moved to item 208

**Plain language —** The AI that picks the trades reads a long briefing built from every other seat's work. Two separate things were wrong with it. The first was bulk: it opened with a wall of raw earnings-report text before ever reaching the list of stocks worth buying. That is fixed — the earnings step now hands over a short conclusion and keeps the full detail on file. The second was subtler and is fixed as of today: one section of the briefing told the AI to check the economics seat's reasoning for mistakes in logic, but there was no box anywhere in its answer sheet where it could report finding one. It was being asked to do a job and given nowhere to write the answer.
**Example —** Before the first fix, of about 200,000 characters read each session, 70% was raw earnings text and the buy-worthy list was buried in under half a percent of it. That 70% was re-counted from the real briefing and was exactly right. It is now about a fifth of a briefing that is well under half the original size — re-counted again today, from scratch, before anything was changed, so the figure is this desk's own and not a quote.
**Today (2026-09-14) —** the missing box now exists, and the risk-checking seat sees whether it was filled in or left blank. An honest caveat, in your own terms: this is a change to what the AI is *asked*, and this desk has no way to test whether a change in wording makes it decide better — the replay rig would pass either version. So the case for this rests entirely on the plain fact that the box did not exist, which anyone can check, and not on any measured improvement. Nothing was switched off to get there: the option of simply deleting those paragraphs would have dropped the question instead of answering it.
**Also worth knowing —** the briefing measured 1,000-odd characters longer than the last count, and that is a good sign, not a slip: the extra text is the new record of *why* each rejected idea was rejected, which is the opposite of filler.
**The decision —** Whether two more pieces of evidence, reward-to-risk and net evidence, should be folded into the scoring system used to rank ideas.
**Recommendation —** Hold off until the reward-to-risk fix above is fully re-measured; folding in a number still being corrected risks baking the same distortion into the ranking.

**Moved from WORK.md (2026-09-24) —** (b) Whether reward:risk (today only a within-tier tiebreak) and net evidence (not used at all) join the composite score — not an owner call; per-seat sizing weights stay refused. (c) A spend cap on the OpenRouter key itself, the one unbuilt leg of the cost-circuit replacement, outside our code.

## item 19 — RETIRED 2026-10-01, all three uses settled: (a) already built as the stored exact input plus prompt version, (b) nothing to drop and no variance measurable offline, (c) barred as a fitted correction

**Plain language —** Given the exact same information twice, the AI gives a strikingly consistent answer, which is useful: the desk can use repeat runs to prove a code change actually reached the AI, potentially skip paying for repeats where the answer never varies, and mathematically correct a known, repeatable bias instead of arguing it away with wording changes. All secondary to the bigger prompt fix already underway elsewhere.
**Example —** Five runs with stock names hidden and five with them shown produced answers identical to four decimal places; a later batch of five runs failed the same check four times out of five, always flagging the same two stock names.

**Moved from WORK.md (2026-09-24) —** (a) Use it as a test instrument — any change in its answer proves a pipeline change reached the model. (b) Stop paying for repeats where the answer does not vary; measure first. (c) Subtract the stable famous-name bias arithmetically in the ratified weighted composite (three prompt-wording fixes measured no-change).

**Result 2026-10-01 — all three uses judged separately; none needed new code.** Source for every number: the production database, read-only, `agent_logs` (713 rows, 2026-08-14 to 2026-10-01) and `specialist_evidence` (13,816 rows); no production row is quoted.
- **(a) Repeat-run check — ALREADY BUILT, in a form that needs no model call.** A repeat run cannot prove a pipeline change reached the model on the current free model: of the identical-input groups in `agent_logs`, none held a real repeat (the only 2 groups were the 22-character placeholder `[cached evidence hash]`, and one of them already gave 2 distinct answers in 5 calls), so the "answer is stable" premise is not measured on this model and was only ever shown on the paid one. What proves a change reached the model is the stored record: 713 of 713 rows keep the exact `input_message` sent, and 697 of 713 carry a `prompt_version` (66 distinct). `scripts/replay_decision.py` and `tests/test_replay.py` already re-run a stored input on demand; running it is paid and stays owner-gated.
- **(b) Stop paying for repeats — NOTHING TO DROP.** No code path repeats a call (no repeat-count setting or loop exists in `src`, `config` or `scripts`), so there is no spend to remove. The no-variance measurement against the five-run pairs cannot be redone: those pairs are not in the database, and re-making them is a paid run the owner forbade. Recorded decision: repeats do not exist, so none is dropped.
- **(c) Subtract the famous-name bias — BARRED in that form, not built.** The bias is only observable as a blinded-versus-named difference, which needs paid paired runs; and any subtraction constant read off this desk's own history is fitting, which `docs/OUTCOME.md` bars, plus "consistent enough to correct" is a threshold nobody can source. Three wording fixes already measured no-change. What would settle it: a per-name blinded/named paired record taken when the owner next authorises a paid comparison, with the correction read off that pair per name, never a global constant. Until then the weighted composite is left untouched; this changes no trade decision.

## item 208

**Plain language —** Item 18 was about the trade-picking AI's briefing being stuffed with raw earnings text. That is fixed and re-counted. Three odds and ends were still filed under it, none of which have anything to do with earnings text, and leaving them there was holding up a separate piece of work. They now live here on their own.

**What the three are —** (a) Whether two more pieces of evidence, reward-to-risk and net evidence, should be folded into the scoring system that ranks ideas; this changes what the ranking seat decides, so it needs a recorded decision rather than a quiet code change. (b) A spending cap set on the AI-provider account itself, which lives in that provider's console and not in our code, so it can only be closed by someone looking at the console. (c) Reordering the buy-eligibility section of the briefing, which cannot be judged without paying for a benchmark run the owner has forbidden unless he asks for it — so it stays untouched.

**Why it was split (2026-09-30) —** Item 19 carried a "do not start before item 18" blocker. The only part of item 18 that item 19 ever depended on was the briefing-bulk work, which merged on 2026-09-04 and was re-measured on 2026-09-30 (earnings share 18.6%, not the original 70%). The three residuals above share no subject with item 19, so the blocker was removed and item 18 retired.

**Update 2026-10-01 —** (a) is decided: neither extra signal joins the ranking score, because reward-to-risk rests on a target (a made-up number by your ruling) and net evidence already gates and sizes. (b) needs someone to look at the provider console; the repo cannot see it. (c) stays blocked on the forbidden paid benchmark. The item stays open for (b) and (c).

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

**The counting half, settled 2026-10-01 — it is a RECORD, not a bar.** We
tried twice to source the number and failed twice, and both failures are
written down in the code: nothing published says what share of a candidate
list a research seat must cover, and our own record cannot say either,
because the news seat has never once written down WHICH companies it looked
at, and the macro seat covers at most half. Fitting a bar to that would be
fitting it to a hole. So the question was not answered with a number, it was
dissolved the same way the first half was: asked once per company instead of
once per list. "Did this seat answer about THIS company" is a yes-or-no
fact. The desk now writes that down for every candidate on every decision —
which seats spoke about that name, which said nothing about it, and which
seats are about the market rather than any one name. It refuses nothing and
holds no minimum. The one per-name coverage rule you already have still
bites: a company with no chart read cannot be bought or shorted.

**What that leaves for you —** nothing on counting; the paragraph below is
superseded and kept only so the history reads straight. One unrelated
question is still open in this item: whether the thirty-minute scan's chart
seat should be able to report a lost answer at all. Today it cannot, and
since the chart seat is the only one that can stop the desk, that scan can
never be stopped by missing evidence. That is a consequence of your own
"only technical analysis can stop the desk" ruling, so no agent may widen
it.

The superseded question, kept for the record: Not "did the seat answer" — that is settled and built, morning
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

**Re-verified against live code, 2026-09-30 — nothing here has rotted, and no agent may close either box.** Both halves were checked on `origin/main`, not from the note above. The CATEGORICAL half is live and firing: `TradingPipeline._evidence_gate_skip` calls `evidence_gate.evaluate` before the paid decision step on morning and on the intraday scan, and the production log line "EVIDENCE GATE — decision skipped: N blocking seat(s) were asked and their answers were unusable" is that call and no other mechanism. The COUNTING half exists as DATA and as REPORTING only: per-seat freshness (`evidence_gate.freshness`, read-this-tick vs carried vs absent) is computed, attached to every decision and named to the owner through the feed and the alerts, and per-symbol coverage is counted in `RunContext.tech_bars_coverage` and `TechAnalysisResult.levels_coverage`. NOTHING COMPARES ANY OF THOSE COUNTS AGAINST A BAR, deliberately — `partial` classifies as an answer that ARRIVED, so partial coverage cannot refuse a decision today. So the state is "the counting exists, nothing acts on it", which is exactly what the two open boxes say. The unsourceable number, named precisely so it is not re-derived a third time: the minimum fraction of a seat's own intended scope that must come back usable before that seat's answer may be leaned on (the "40 of 65 companies" case). Nothing published gives it, the desk's own history cannot supply it without fitting, and it is a risk-appetite dial. Deliberately NOT done in this pass: no threshold invented, no seat added to `BLOCKING_SEATS`, and the existing refusal left exactly as strong as it was.

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

## item 63

**Plain language —** When a company insider sells shares, the desk wants to know whether that's a real opinion about the stock or just someone raising cash. The best measure is how much of their own pile they sold. The research that measures this found something counter-intuitive: an insider selling a *small* slice of what they hold is actually a mildly *good* sign — they need money, they're keeping the rest, they still like the company. Selling more than half is the only case that reliably means bad news. The desk was doing the opposite of reading that correctly: it treated small sales as meaningless and threw them out of the ranking entirely. That's now fixed — nothing is thrown out, and every insider trade arrives at the analyst carrying how big it was relative to what the person held, plus what the research says that size means. What's still missing is narrower: the desk's internal "how much does this matter" score is a single dial from 0 to 1, and a dial cannot say "this matters, and it points the *other* way." So the analyst reads the direction in the notes, but the automatic ranking underneath it doesn't.
**Example —** An executive holding 100,000 shares sells 1,000 of them. Research says that's worth about +0.68% over the next quarter — a small positive. Another sells 80,000 of 100,000; that's worth about −0.81% — a real negative. Today both arrive at the analyst with the same "importance" score of 1.0, distinguishable only by the written note attached. Before this change the first one scored 0.0 and the analyst never saw it at all.
**The decision —** None needed from you right now, and deliberately so. The obvious move — invent a number that scores the bullish case lower or higher — would be exactly the kind of made-up figure this desk refuses. Two sources were checked for a signed scoring scheme and neither has one. This item exists so the gap is on the record rather than quietly papered over, and it gets picked up when either a published source or enough of the desk's own trading history can settle it.

**Moved from WORK.md (2026-09-24) —** Scott & Xu (FAJ 2004): an insider sale under 10% of the holding earns +0.68% adjusted quarterly excess return yet gets weight 1.0, identical to dumping 80% (-0.81%). Ratio and band are already reported so the seat can read the sign; the question is whether the deterministic ranking should too. **Ruled out, with sources: `docs/INCIDENT_HISTORY.md`, 2026-09-13.** Settles with a published signed scoring scheme, or enough own outcome data to read a separation.

**Structure fix shipped (2026-09-25) —** The deterministic ranking now HAS a sign. `SmartMoneyObservation.signal_direction` (derived from `direction`, never stored) returns +1 for a buy, 0 for a sale/exchange/unknown; both ranking keys in `src/agents/smart_money_analyst.py` (`_symbol_rank`, `_transaction_rank`) multiply the `value * signal_weight` term by it. So a bearish sale can no longer tie or outrank a bullish buy of the same dollar value — the exact identity this item names — and a buy's contribution is unchanged (existing behaviour preserved; covered by `tests/test_smart_money.py`). The desk is long-only on smart-money admission (admission requires `direction == "buy"`), so a sale is NEUTRALISED (0), not counted as bullish; the row still reaches the analyst as evidence, so the LLM can still read it bearish. **What stays open:** signing a sale -1 by magnitude (the sourced >50%-of-holdings band is the hook) is the SIGNED SCORING SCHEME still ruled out above — owner appetite or a published source, not a number to guess.

## item 70

**Plain language —** One made-up number, 1.0, is doing two different jobs in the selling path, and neither job is read off anything. The first job is deciding how far a stock has to move against you before the move counts as real rather than ordinary daily wobble. The second is deciding how tight a stop-loss is allowed to be before the desk refuses it as too close. Both are expressed as "one average day's range". That they are the same figure is a coincidence — nothing ties them — so changing one would not change the other, and changing neither is not a source. The first job is also the only measured over-refusal on this path: of eight proposed sales the reviewer approved, seven were blocked as "too small a move". Closing the plumbing next door did not answer why.
**Example —** A stock whose average daily range is $4 has to move $4 against you before the desk stops calling it noise, and separately, its stop is refused if it sits closer than $4 away. Those two rules constrain each other in a way nobody chose, because somebody typed 1.0 twice.
**The decision —** None for you on the number yet. How readily the desk should block a sale at all is still yours and is not this item — this item is only the two unsourced 1.0s. Each stays open until it has either a published measurement of the quantity it bounds, or a decision to derive one from the other as a single named constant.
**Recommendation —** Keep them as two questions. Do not retune either number to make sales easier or harder — that would be picking an appetite figure. Do not collapse them into one shared constant just because the digits match. Search for a published measurement of each quantity, or name a single derivation that produces both; until then, leave the figure where it is.

**2026-09-26, what changed and what did not —** The two jobs now have two names, at the same value, and nothing the desk does changed today. One number became `NOISE_BAND_ATR_MULTIPLE` (how far a holding must move against you before the move counts as real) and the other `BREAK_CONFIRMATION_ATR_MULTIPLE` (how far a day's closing price must sit past a support level before that level counts as broken). Both are still 1.0 and neither was retuned, which this item forbids in the same pass. A separate error turned up in the ledger while doing it: the exit-path band was filed as if it were worked out from the trailing-stop band of 1.25, which it plainly is not, since it reads 1.0 — so it is now recorded honestly as a number with nothing behind it.
**What the published work says, so nobody searches again —** For the first job every published figure is roughly three times today's: Wilder's 1978 volatility system, the Chandelier Exit's standard setting and Kaufman all sit near 3 average daily ranges, though all three measure a stop's distance from a running high rather than a move away from your entry, so they are the nearest published analogue and not the same measurement. Moving the desk from 1 to 3 would make it markedly slower to accept that a loss is real — fewer premature sales, a bigger give-back before it acts — and that is your appetite, not this item's to set. For the second job there is no answer in these units at all: the literature measures a break in PERCENT of price and differently for a major level than a minor one (Edwards & Magee use about 3% and about 1%), or treats a decisive close as sufficient with no distance at all (Bulkowski). The rule around it — two consecutive closes — is properly sourced; only the distance is not.
**Still open —** Both values, and the separate hard floor under every stop, which still has nothing of its own behind it.

**2026-09-30, the route changed —** You ruled that how much risk to tolerate is read from each individual holding's own behaviour and from how strongly the seats believe in it, never set once as a single number for everything. Both leftovers here had been parked as "ask the owner to pick a figure", and that is exactly what your ruling forbids, so neither can be settled that way now. The wobble band settles only by reading, per holding, how far that particular stock ordinarily moves against you; the break margin is worse off — measured in the wrong unit entirely, with nothing published to compare it against, and it settles only by being re-stated as a percentage of the price that depends on how important the level is. Both of those change when the desk sells, so neither was done today, and no substitute figure was made up.

**2026-09-30, a per-name statistical band was tried and REJECTED — do not re-attempt it.** The attempt replaced the one shared number with each stock's own middle-of-the-road past adverse move. It was closed on review. The worked example offered to justify it turned out to argue the opposite way once the arithmetic was checked. The bar history it read was fetched to a fixed depth that was itself an unexplained round number. The stretches of history it measured overlapped each other, so the same days were counted several times and the evidence looked stronger than it was. A stock held twenty-nine sessions produced exactly one past observation, and the code presented that single number as a typical one. One of the two places the desk uses the band was left on the old path, so the two would have disagreed. And when the price history failed to download, it fell back silently to the old, much wider number without saying so.

**Why no summary of history can settle this, ever.** The rule you set is that risk tolerance is read from the instrument in front of the desk, not fitted to the past. Any "typical" figure taken from a spread of past moves — the middle one, the one two-thirds of the way up, the average — is a choice of where in that spread to stand, and that choice is exactly the appetite figure this item exists to remove. Swapping one global guess for a per-stock guess dressed as a measurement makes the guess harder to see, not smaller. So this item cannot be closed by any statistic computed over history, and the next pass should not try.

**What WOULD settle it, if the band survives at all.** Either a published, citable derivation of the quantity the band claims to bound — the adverse excursion at which a move stops being ordinary daily wobble — carrying its own number, or a live reading taken from the instrument at the moment of the decision that no one had to choose the level of. Absent one of those, the honest route is the one below.

**2026-09-30, is the band redundant with the alignment rule? Read from the code, not the prose: NO, and the two places it lives are not the same.** The alignment exit you ruled for — sell only when structure, the stock's own volatility and the trend agree the move is over — WAS not yet built as its own mechanism when this note was written; it now is (`src/risk/alignment_exit.py`), and since the alignment SCAN landed it is read against every held position every session and can start a sale of its own, not only confirm one a model already asked for; what exists is the protection check, which asks whether the seller's stated invalidation has happened, or whether a genuine support level under the stop has broken, and gates a break on the trend it reads. The band's FIRST home is a blanket gate standing in front of all of that: every sale that is not triggered by outside news is compared against how far the stock has moved from the PRICE THE DESK PAID, before any structure is consulted at all. Nothing else in the selling path measures anything from the purchase price. That is its unique job, and it is the wrong job: measuring from what the desk paid is the definition of fitting the decision to the desk's own history rather than reading the instrument, which is what your ruling forbids. Removing this gate, and letting the structure-and-trend test decide, is the honest fix and it removes the number by removing the mechanism — the same move that retired the correlation cutoff. It is a real change to when the desk sells, so it is not made here.

The band's SECOND home is genuinely not redundant and must not be deleted with the first: it is the last resort for a holding that has neither a written invalidation nor a qualifying support level, where the structure test has nothing to read. Delete it there and every such holding loses its protection outright. That case needs its own answer before anything is removed.

**Fixed today, no change to when the desk sells.** The refusal record used to state the move was inside the band without saying that the band's width had been widened by a hold length the desk could not actually read, and the durable per-stock record of the refusal carried only the model's own sentence — nothing saying which rule refused it or on what numbers. Both records now state the rule, the numbers behind it, and whether the hold length was measured or defaulted. Separately, two outcomes of the protection check were filed under the label "noise band held" when the band had never been evaluated at all — a holding that had not moved against the desk, and one with no usable price data. Those now carry their own labels.

**2026-10-01, the break margin's two derivation attempts are spent — both written down so neither is retried blind.** Trying to express it the way the books do, as a percentage of the price, fails twice over: deciding whether a level is an important one or a minor one — which is what picks between the two published percentages — needs a judgement the desk cannot make in those terms, so it would have to invent a second number to replace one; and the published percentages are themselves round figures from a 1948 book written for a market quoted in eighths, so adopting them trades an unsourced figure for an unsourced figure. Trying instead to read the margin off the instrument itself — the smallest price step at which a closing price is even distinguishable from the level — does produce something real, but only that the margin must be bigger than nothing at all. That part IS now ratified with its reason: if the margin were zero, a stock merely touching the support its stop sits on would count as having broken it, and the desk would drop protection on every ordinary retest. The size of the margin is still a made-up figure and is still recorded as one. No third attempt will be made, and a figure taken from the desk's own past trades is barred.

**2026-10-01, what was built instead: the recording that would settle it.** The reason this number could never be checked against the published work is that nothing ever wrote down what "one average day's range" actually came to as a percentage of the price at the moment a decision was taken — the only unit the books measure the same thing in. Every break check now records both, together with how many times the level has been tested. Nothing about when the desk sells changed.

**2026-10-01, measured against the live database, read-only.** The equivalent recording added for the wobble band on 30 September has captured nothing at all: of 334 recorded position checks, not one is a wobble-band refusal and not one carries the machine-readable payload. So that number still has no evidence accruing, and the long-quoted "seven of eight approved sales blocked" cannot be reproduced from that record. Both of the wobble band's remaining routes change when the desk sells and were not taken here.

**Moved from WORK.md (2026-09-24) —** Same round number, two questions, no source, nothing tying them. The first job blocked 7 of 8 recorded discretionary exits. Settles with, for each independently, a published measurement of the quantity it bounds, or a decision to derive one from the other as a single named constant. How readily the desk should block a sale at all is the owner's appetite, not this item.

## item 75

**Plain language —** When the desk bought Oracle on 2 September, the chart analyst set a profit target of $159.52 — a price Oracle had already failed at twice. Every seat saw that number: the portfolio manager used it to justify buying, the risk manager saw it, and the position reviewer was shown it every session with the words "soft — you manage exit". But nothing ever used it to sell. The reviewer is told it manages the exit, while its sell rules refuse "taking profit" as a reason, and the "past target" warning only appears at 150% of the way there. Oracle traded above the target on 4 and 8 September. Selling at target would have made about 9%; the desk would have ended slightly below its purchase price.
**Example —** A target is written down before the trade opens, used to say the trade is worth taking, then ignored for the rest of the trade's life.
**The decision —** You asked for each position's target to be shown on the Mission Control chart, and for using it to be debated rather than dropped. Paper-trading "sell at target" for a week was argued against: too few trades to tell luck from skill, one market mood, and it would muddy the restart with a new manager model at the same time.
**Recommendation —** Show the target on the chart. Then track, without placing orders, what four exit rules would have done on every trade — sell all at target; sell half at target and trail the rest; hitting target tightens the trailing stop instead of selling; and today's desk — with the rules fixed before anyone looks and no tuning afterwards. Nothing is built on one trade.

**Moved from WORK.md (2026-09-24) —** **Every number, narrative and blind spot is in `docs/BOARD_NOTES.md` ("item 75") and `docs/INCIDENT_HISTORY.md` (2026-09-14). Read them before working this; do not re-derive.** Short version: the TA target justified the trade, was seen by the Risk Manager and shown to the reviewer as "soft — you manage exit", while the reviewer's sell gate refuses profit-taking. Selling at target was +9.1%; the only automatic exit the code allows filled below entry. The target never reaches the broker, the breach flag needs >150% progress, an 8-K results release is invisible, and six trail constants are unsourced (in item 90's list). Profit-taking, rejection off a high and upcoming earnings are not allowed SELL reasons, and only the news seat emits state changes, so a chart breakdown cannot unlock an exit. Structural finding carried out of the same work: **the desk never sees today's bar**, so every intraday indicator and level describes yesterday's close. Answered 2026-09-14 [measured]: no single standard technical sell rule gave a timely ORCL exit that also beat chance on a 10-name basket — it supports multi-signal confirmation, nothing more. **Settles with a fix, never fitted to ORCL:** exits confirmed by several independent signals; trail tightness read off the instrument or a cited source; a ruling on whether target-plus-confirmed-breakdown may exit; the 8-K gap closed. One number changed alone is the patch this item exists to prevent.

**Rewritten 2026-10-01 — the two derivations of a trim fraction that FAILED, written down so neither is retried blind.**

**Attempt 1 — read the fraction off the position's own risk (trim back to the original risk budget).** Sell the share of the position that restores the risk the trade was opened with, now that the stop has ratcheted. The fraction is `1 - d0/d1`, where `d0` is the entry stop distance and `d1` the current one. The desk's ratchets move the stop to multiples of `d0` that the owner ratified (+1R, +2R), so `d0` cancels and the fraction is the SAME number for every name, every sector, every volatility. It looks per-name and is algebraically a global constant. Rejected: a global risk constant is a defect on this desk, not a value to ratify.

**Attempt 2 — read the fraction off how far the position has weakened.** Scale the trim by the ATR distance already travelled below the last mark, as a share of the full alignment-exit tolerance. Rejected for two independent reasons: the denominator is the alignment-exit tolerance, which that module's own docstring and its ledger entry declare to be owner appetite and NOT sourced, so the fraction inherits an unsourced denominator and is an appetite dial in disguise; and no record exists of any position ever sitting in that intermediate band, so there is nothing to read the shape off even if the denominator were sound.

**What the live code actually does, checked 2026-10-01 (claims in the original filing had rotted).** The target reaches no broker — still true. The target no longer gates whether the structural trail runs: item 142 removed that switch, and the trail runs for a range trade once it is past the +2R ratchet trigger. One live effect survives — whether price has exceeded the target decides whether the +1R lock floor constrains that trail — and the owner-facing note now says so instead of implying the number is inert. The alignment exit (`src/risk/alignment_exit.py`) sells a WHOLE position and contains no partial path, so the trim case is genuinely untouched by it.

**Three of the original six criteria are VOID, not deferred.** "Sell all at target", "sell half at target and trail the rest", and "target tightens the trail" are all barred outright by the 2026-09-30 ruling. Tracking what a barred rule would have done is only worth doing if it might be adopted, and it may not be.

## item 78

**Plain language —** You locked a standing rule: if something the desk needs is missing, find why and make that step actually produce it. Do not invent the missing words. Do not make "drop this name and trade the rest" the standing answer. The current case is a blank "I'll sell if". A temporary patch currently drops that name after we already asked twice, so one blank cannot veto the rest of the book. That patch is not the fix. The real path is: the seats write a real "I'll sell if" before a buy or short can be ticketed; a sentence the model already wrote is put back if a later wipe blanked it; the seat is asked once more; never invent the words. If it is still blank, that name is refused. A catalyst note stays optional.
**Example —** A buy on a chip stock arrives with prices and a stop but the "I'll sell if" box is empty. The desk does not make up a sentence, does not let that blank name veto the rest of the book, and does not ticket it. After one re-ask still blank, that name is refused and the others can proceed. The standing design is that the box is filled, not that the name is dropped.
**The decision —** You locked the standing rule. The temporary drop-the-name patch stays until a live session proves the seats actually fill the box. The rule is not only about "I'll sell if" — any missing required field is the same class of defect.
**Recommendation —** Keep the never-blank path. Keep the drop-the-name patch labelled temporary. Do not treat skip-and-continue as the product.

**Checked again 2026-09-30 — the answer is still no, and now with numbers.** The technical seat left the "I'll sell if" box empty on 60% of the stocks it looked at on the most recent trading day, 29 September (134 of 223), and on 54% the day before. That is the same as it was through the whole of the previous fortnight, where the daily figure moved between 30% and 73%. The seat's answer format was tightened on 25 September to force a strict shape, and the figures after that change are indistinguishable from the ones before it, so the tighter format did not make the seat do the work. On the much smaller set of names that actually reached a buy or short, the seat was still blank 5 times out of 75. Separately, two of the three things the code itself says must be shown before the patch can be removed cannot be checked at all: they are claims about the repair step, and the repair step for this particular box has never once written down what it did, so there is nothing to read. The patch stays. To judge this item next time, the repair step must record, per stock per session, whether it was tried, skipped, blocked or paid for.

**Checked again 2026-10-01 — still no.** On 30 September the technical seat left the "I'll sell if" box empty on 53 of 78 stocks (68%), so the blank rate has not come down; the portfolio manager filled it on both of the 2 names it targeted, which is too few to prove anything. The repair step still has no record of what it did, so the other two conditions still cannot be read. Source: live database, not the empty repo copy.

**Moved from WORK.md (2026-09-24) —** Plain-language account: `docs/BOARD_NOTES.md` ("item 78"). Permanent fix: heal, then one paid retry; still blank → refuse before the book. Never invent. Delete the isolate when a live session proves never-blank.

## item 90

**Plain language —** About thirty numbers that govern real trades were never read off anything — they were chosen because they sounded sensible. They were catalogued on 11 September and then filed as "an inventory, not a job", with a note saying never to re-audit. Nothing was assigned, nothing had a date, and a week later all thirty were still live. There is no mechanical check of any kind that would catch the next one.
**Recommendation —** Two halves. Read each number off the instrument it is meant to describe. And build a check that fails the build the next time an unsourced trading number is added, so this cannot happen again by filing.

**The second half is built, 2026-09-18, and was then torn apart and rebuilt the same day. The first half is still yours.** Every number of this kind now has to be written down in one file next to a sentence saying where it came from, and the build refuses to pass if one appears that is not. Nobody has to remember the rule: the check works out for itself which numbers count, and the only way to add one is to write the sentence. It insists on six things — that the number is written down at all; that the figure in the file is still the figure in the code, so changing a number puts the reason for it back in front of whoever is changing it; that the figure in the file is also the figure in the settings file the desk actually runs on; that a claimed source is a link or a file and line somebody else can open, not a confident paragraph; that a number with nothing behind it also states the question that would settle it and what the desk pays meanwhile; and — the one worth knowing about — that a number worked out FROM another number fails the build if that other number later moves. A figure can be perfectly well justified when it is written and quietly meaningless a month later because the thing it was measured against changed; that now shows up as a broken build instead of a respectable-looking comment.

**Why it was rebuilt, and it matters more than the check itself.** The single entry the first version held up as its showcase — the 0.50% minimum risk per trade — was wrong in four separate ways. It said the number appeared nowhere in the settings file, nowhere in any document, and in no record of you approving it. All three were false: it is in the settings file, it is in your own ratified risk table in `docs/OUTCOME.md`, and you approved it on 27 August. It also said the number existed in only one place while the same file listed it twice, and a third copy of it was invisible to the check altogether. Every one of those was a thirty-second look away and nobody looked. **So read the honest limit of this thing: it proves a reason has been WRITTEN, never that the reason is TRUE.** That is why a source must now be something openable rather than prose, and why seven corrected entries carry the correction in writing rather than being quietly tidied.

**The count.** 178 numeric sites are now watched, up from 122, after four more modules were brought in — including the ATR period, which every stop and every noise band on this desk is a multiple of, and which the first version watched all the multipliers of while ignoring the unit. **86 distinct numbers decide how this desk trades and have nothing behind them.** That is fewer than the 87 first reported despite the wider net, because mirrored numbers — the same figure written in two files — are now recorded as one number rather than two, and three turned out to be genuinely approved by you and were reclassified. Every value was left exactly as it was: changing one is your call, not an agent's.

**What it still cannot do, so it is never oversold.** It cannot tell a true source from a plausible-sounding sentence; that is the failure above and it is structural, not an oversight. The list of watched modules is written by hand — there is now a second check that counts the numbers in every module NOT on the list and fails if that count grows, so a new one cannot slip in unseen, but the list itself is still a judgement call. It cannot see numbers written into the seats' instruction sheets as prose, which is a separate item. And it does not make any of the 86 sourced — it only stops the 87th arriving unnoticed.

**One factual correction, because it was being repeated.** The story that the range stop-width scaler 0.90 was derived against a stop base of 1.5 which later became 2.5, leaving the derivation stranded, does not match git. The base went from 3.0 to 2.5 on 2026-09-10, and 0.90 was introduced by that same change — it was never derived from anything, and the code beside it says as much ("not a specific measured number"). The class of defect is real and the check now catches it; this particular example is not an instance of it.

**Moved from WORK.md (2026-09-24) —** Full account of what was built and the gate's own honest limit (it proves a justification was WRITTEN, never that it is TRUE): `docs/INCIDENT_HISTORY.md` (2026-09-18). The counts quoted there are a snapshot, already known stale by the next day — read `config/number_ledger.yaml` directly rather than trusting a number here. **Half two, STILL OPEN:** read each arbitrary entry off its instrument. Each states in the ledger the question that would settle it and what the desk pays meanwhile, which is what makes half two prioritisable rather than a list. `MAX_ARBITRARY_ENTRIES` is an EQUALITY, not a ceiling: as a ceiling it rewarded deleting a row.

## item 99 — RETIRED 2026-10-01, both remaining criteria closed: the technical seat's prompt now names every data block the code sends it (the market-context bullet was missing the consolidation state, liquidity, up/down volume and unfilled-gap lines) and is pinned by `tests/test_tech_prompt_input_contract.py`; the deletion-site check shipped 2026-09-26 as `described_gaps()` and now registers the technical seat's two renderers

**Plain language —** A second review, of the prompts that brief the analysts (the seats that read the market and write reports, one layer below the decision-makers), found the prompts are full of numbers and claims nothing in the code actually enforces. The desk already bans numbers that were invented rather than read off real data; a number that lives only in a brief is exactly that, and it was invisible because nobody had looked in the briefs. (Filed twice, as items 99 and 105; diffed and collapsed into this one 2026-09-18.)
**What it found —**
  - About 55 numbers exist only as text in a prompt, with no code checking or producing them.
  - About 20 claims about how markets behave are stated as fact with no source.
  - The technical analyst is told it gets 20 days of price history; it actually gets 40, plus five whole categories of data the prompt never mentions it has.
  - The evening report's briefing hasn't been touched since before this project started, and still describes a completely different strategy — a 77-symbol quarterly value approach — while the technical seat's own briefing describes a 5-to-15-day swing-trading window. Both feed the same decision seat. They cannot both be the desk's real strategy — **this is now a pending decision for you, in `docs/WORK.md`.**
  - Roughly a third of the portfolio manager's briefing, and a quarter of the risk manager's and the position reviewer's, is prose describing machinery the model doesn't actually use. That dead weight is where almost every stale or wrong claim above was found living.
  - Separately: a check already exists that fills prompts with numbers straight from the code so they can't go stale, but it only covers 2 of the 10 prompt files. Scanning prompt text for suspicious numbers doesn't work either — there are about 1,825 numbers in there, mostly just dates and list numbering. Neither of the two confirmed mistakes above (item 98) was even sitting in a prompt file — both were assembled by Python code into a string. What would actually have caught the worst one: when code that a prompt describes gets deleted, search the prompts for its name at that moment.
**Recommendation —** Decide the evening-vs-technical mandate question first (it changes what "fix the prompt" even means); then strip the dead weight, since that's where the false statements cluster; build the deletion-site check as ongoing insurance rather than trying to scan for numbers. Not yet placed in your priority order.

**Technical detail, moved from `docs/WORK.md` 2026-09-24 —** (a) About 55 numbers exist only as prompt prose with no code behind them, and about 20 market-structure claims are asserted with no cited source. (b) The technical seat receives five data blocks its prompt never mentions; its separate bar-count defect was fixed and retired 2026-09-20 (`docs/INCIDENT_HISTORY.md`). (c) Dead weight — prose describing machinery the model does not perform — is roughly a third of the portfolio manager's prompt and a quarter of the risk manager's and the position reviewer's, and is where almost every stale claim clustered. (e) Order of work: settle the mandate question first, then strip the dead weight; the deletion-site check is insurance, not the first move. (f) **Re-verified and PARTLY FIXED 2026-09-26 — the false-mechanism half is closed; the unsourced-grid half stays open.** Measured 2026-09-19: PM sizes sit on the prompt's own 0.25 grid — 36 of 37 recorded `risk_allocation_pct` on-grid, max ever 3.0 vs the 5.0 ceiling. The 2026-09-26 re-audit found the unenforced "2+ oversized → cut every BUY 25%" was WORSE than filed: the same claim also sat in `config/prompts/risk_manager.md`, stated to the Risk Manager as machinery already running ("pre-adjusts its sizing before you ever see the plan — 2+ `oversized` tags cut its base allocations 25%") and used as grounds to discount a conservative PM plan as "anchoring on your history" rather than conviction — one paid seat told to weigh down another seat's caution because of a mechanism that does not exist. What IS real, verified against code: `_build_rm_recent_verdicts` (`src/pipeline.py`, `limit=5`) renders the last five verdicts with their `cat=` tags into PM's sheet via `src/agents/portfolio_manager.py`, and PM's own sheet asks the model to adjust. Nothing reads `reason_category` and resizes anything. Both sheets now say exactly that; the 25% is REMOVED rather than replaced (picking a substitute would be the same defect), the discount instruction is gone, and the three sibling sites (`risk_manager.md`'s `reason_category` bullet and downstream-consumers footer, and the `RiskVerdict.reason_category` comment in `src/models.py`) were corrected in the same pass. Pinned by a new `config/retired_mechanisms.yaml` entry ("the automatic 25% oversized base-allocation cut") whose phrases fail the build if either sentence returns. NOT DONE and still (f): the `0.25` sizing grid, the `2+ occurrences` trigger threshold and the conviction-to-base midpoints in the same PM section are all still unsourced prompt-only numbers of class (a). NOT FIXED, named deliberately: the `_build_rm_recent_verdicts` docstring in `src/pipeline.py` still asserts the loop works ("pull base allocations down before RM has to do it again") — that file was out of bounds for this change. On whether code SHOULD enforce the cut: no, not on a number nobody can source; the honest enforcement would be a measurement of whether PM adjusts at all, which is not in scope here.

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

**(d) SHIPPED 2026-09-26 — the deletion-site check became a trigger instead of a convention.** What existed already was a scan: `config/retired_mechanisms.yaml` lists mechanisms somebody recorded as gone, and `src/retired_mechanisms.py` fails the build while any prose still describes one. Its own docstring names the hole — "a retirement nobody records. This is the real limit, and it is a convention." Added here: a `described:` section in the same file, listing LIVE code symbols that prompt text currently describes together with the exact places that description lives. `described_gaps()` fails the build the moment such a symbol is deleted or renamed, prints every sentence that just became untrue, and tells the reader the one way out — write the `retired:` entry. That is the entry nobody was remembering to write, so the older scan now gets triggered by machinery rather than by memory.

**Why this is not a third grep, stated so nobody builds a fourth.** Three jobs, no overlap: the `retired:` scan searches prose by the WORDS of a mechanism that is already gone (it runs after the fact and holds no code references); `described:` holds no phrases and no digests and only asks whether a live symbol still exists and whether its description is still where it was; `src/prompt_bindings.py` (item 107) pins code and prose to each other by digest to catch behaviour that CHANGED while still existing. A deletion leaves no code to digest, and a changed threshold leaves no retired phrase to grep. `described:` lives inside the existing deletion-site module and registry precisely so it is not a fourth file.

**The blind spot item 107 found is covered here.** Both confirmed drift instances on this desk were strings Python assembles at run time, not prompt files, and item 107's registry anchors `config/prompts/*.md` in both of its entries. `described_in` takes any path. All four mechanisms registered to start anchor `src/agents/*.py`, and three anchor no markdown at all: the sweep vehicle's liquidation before a BUY (told to three seats, backed by `CashSweeper.fund_buys`), the contradicted-exit veto the reviewer is threatened with, the same-day trim discipline, and the risk manager's "an edit that raises it is refused by the engine".

**False positives, and the escape hatch.** Nothing fires unless somebody registered it, so the noise floor is zero by construction and there is no heuristic to tune. A rename fires deliberately and is re-pointed in one line; a deletion fires and is resolved by moving the entry to `retired:`; a prose rewrite that removes the description fires and is resolved by re-pointing or by confirming the seat is meant to stop being told. A needle under 12 characters is refused at load, for the same reason a short `phrase` already is. A symbol listed as live and as retired at the same time is refused rather than ignored, because forgetting to drop the `described:` entry is the likeliest mistake the section invites. The check is pure `ast` plus substring matching over files already in the tree: no model call, no network, no subprocess.

**A blanket symbol scan was measured and rejected, in the same shape as the number-scanner rejection above.** `config/prompts/*.md` carries 430 snake_case tokens [measured 2026-09-26, `ast` over `src/**/*.py` versus a regex over the prompt files]; 151 of them resolve to no Python definition anywhere in `src/`, because they are enum literals and JSON field names the seats EMIT — `greed_top_chasing`, `thesis_invalid_if`, `fundamentals_mispricing`. A gate over all of them is 151 failures on day one, and a check that cries wolf gets switched off, which costs more than not having it.

**Still open under this item:** (a) the un-enumerated remainder of the ~55 prompt-only numbers and ~20 unsourced market claims — the PM and technical sheet subsets are already catalogued precisely under item 107 and should be resolved there, not re-listed; (b) the technical seat's five unnamed data blocks, untouched here; (c) the dead-weight prose in the PM, risk manager and position reviewer sheets, untouched here; (f, residue) the 0.25 sizing grid, the `2+ occurrences` trigger and the conviction-to-base midpoints, left open when the false 25% cut itself was deleted on 2026-09-26; (g) rendering-or-pinning every code-controlled sentence, which is item 107's registry and covers two pairings so far.

**RE-SCOPED AND MEASURED 2026-09-30 (production database, read-only).** The audit was ending in an inventory, which is barred, so it now ends in a test. Over 3,845 recorded `tech_analyst` analysis rows `thesis_invalid_if` is non-empty on 195/195 actionable ratings since 2026-09-25 and 1,664/1,665 before it, and empty on 100% of NEUTRAL ratings because the prompt and `TechAnalysisResult` both require that — a neutral is the absence of a call, so there is nothing to falsify. **Any "the technical seat leaves its falsifier blank on six answers in ten" claim is that neutral population and is NOT a defect; the 25 Sep answer-format schema did not change the rate because the actionable rate was already about 100%.**

**The real analyst-seat gap, measured:** four of the five analyst seats — news, earnings, macro, smart_money — state no falsifier at all. No prompt mentions the field, no answer schema defines it, and 0 of 103 recorded nominations carry one; their invalidation is synthesised downstream, so a name whose only supporting seat is one of those four reaches the conviction bar on a templated falsifier rather than the seat's own words. Requiring it of them changes what those seats are asked to produce, so it is filed as a decision, not taken here.

**Pinned by** `tests/test_analyst_seat_falsifier_contract.py`: a per-seat registry of who states a falsifier, asserted against both prompt text and answer schema, plus the two sentences in `tech_analyst.md` that hold the actionable rate up. It fails if the one enforced requirement is deleted (prompt rot) or if any of the four seats silently gains or loses the field.

**Split out of item 99 on 2026-09-30:** the prompt-only numbers and unsourced market claims (old 99(a)), the render-or-pin requirement (old 99(g)) and the (f) residue belong to item 107(b)/(c), which already tracks them; the PM/RM/position-reviewer dead-weight prose (old 99(c)) belongs to item 109(c). Item 99 keeps the analyst seats' own prompts and their enforcement.

**Analyst-seat falsifier slot BUILT 2026-09-30 —** The four seats that were never asked what would prove their call wrong (news, earnings, macro, smart money) now state it themselves at the moment they make the call: News, Earnings and Macro per nomination, Smart Money per finding, using the same field name and the same plain-string shape the technical seat already uses, so the existing exit checker reads all five unchanged. Honest limit, stated rather than papered over: that checker can only evaluate a price level or a 20/50/200-day average, so a macro or news condition phrased in words comes back as UNEVALUATED with a reason — it is never counted as passed. A seat that names no condition leaves the slot empty; the gap is recorded as a gap on the nomination event and nothing downstream substitutes a template. A neutral smart-money stance keeps the slot empty, the same rule the technical sheet already holds. A test now pins all five seats, so a seat that silently stops being asked fails the build.

**RETIRED 2026-09-30 — the third DONE WHEN closed here.** All five analyst seats now state `thesis_invalid_if` at the moment of the call (`tests/test_analyst_seat_falsifier_contract.py` registry: five of five True, asserted against both prompt text and answer schema), so the "only one of five" headline is no longer true of live code. What was left was the checker's vocabulary: `exit_guard.check_thesis_invalid_if` understood a bare price level and MA20/MA50/MA200 only — both measured off `tech_analyst`, whose falsifier is always about price — while the four new seats' own shipped examples are not (`macro_analyst.md` ships "HY OAS widens back above 420bps"). An unevaluatable falsifier is a falsifier in name only, so a THIRD shape was added: a named macro series compared to a stated level. No new comparison and no tolerance — the same strict above/below test, pointed at the FRED series `src/data/macro.py::get_macro_summary()` already fetches every run and already shows the macro seat, passed in by the caller exactly as `current_price` and the MAs already are. bps<->% is applied because it is a definition (1% = 100bps); an unlabelled spread number ("HY OAS above 420") is UNPARSEABLE rather than assumed, and no threshold was invented to disambiguate it. A latent wrong answer went with it: "HY OAS above 420.5bps" previously matched the bare-price pattern and was compared against the stock's own price, so the named series is now tried first. The four prompts were corrected in the same pass to name the macro form and require the unit, because prompt text that describes the checker is code that rots. What remains UNPARSEABLE is the genuinely qualitative condition ("the contract award is rescinded"); that is the deliberate, recorded limit, reported as unevaluated and never treated as passed.

## item 112 — RETIRED 2026-09-30

**Verified against live code, not rebuilt: both halves already shipped.** `_record_delever_shortfall` (`src/pipeline.py`) has written the durable `specialist_evidence` row (`stage='gross_delever'`) since 2026-09-19. `_alert_owner_delever_incomplete` (`src/pipeline.py`) already pages the owner via a standalone `send_owner_alert`, edge-triggered on the transition into still-over-ceiling, shipped 2026-09-25 (#697, "Item 112: promote `_alert_owner_delever_incomplete` from a one-line session bullet to a standalone `send_owner_alert`"). WORK.md's "the alert decision stays open" clause was stale — the alert was built the same week the clause was written and the item text was never updated. `tests/test_gross_exposure_ladder.py` (11 relevant cases) pass on main. No code change made; item removed from the open queue.

## item 109 — RETIRED 2026-10-01, (a) built 2026-09-26 and (c) closed: changelog prose stripped from the PM, risk-manager and reviewer sheets

**Plain language — not yours to wait on any more; the orchestrator decides after an adversary run, and nobody may settle it by editing code first.** The desk has a big-picture seat that reads the whole market: interest rates, credit, volatility, the general mood. It also has seats that read one company at a time. When the desk counts up how much evidence supports a single trade, it currently counts the big-picture read as one of those votes, for or against that individual company. The instructions given to the trade-picking seats said the opposite — that the big-picture read never counts toward that tally. One of the two has been wrong all along, and the code is the one that has actually been deciding.

**Why it is not theoretical —** On 17 September a bullish read on the market as a whole cancelled out a bearish filing about one specific company, and the trade died. A second company passed with full support on the strength of the market read plus a filing, with no read of its own chart at all.

**Why we stopped rather than fixing it —** The obvious "fix" is to correct the instructions so they match the code. That would quietly make the current behaviour official, and the desk's own standing doctrine points the other way: it is the trade-picking seats' own prompt (not `docs/OUTCOME.md`, which says nothing on this beyond a line about cash deployment) that states the market read is the regime the book is built in, not a fact about one company. Making the instructions match the code would have ratified a rule you never agreed to. So the wording is being made neutral — it says the tally does count it, that this is disputed, and not to lean on it either way — and the counting rule itself is untouched. Filed independently twice, hours apart, as this same question; the two are now one item.

**RULED by the owner, 2026-09-25; built 2026-09-26. Part (a) is closed; item 109 stays open for part (c) only.** His words: "Macro means macro. It is one data point. Depending on how strong the data point is — like a black swan, or war, or calamity — the data point should be weighted." · "I don't understand why long or short would matter. It's a data point... it can be measured and weighted depending on if it's positive or negative. Would help on a long or a short." · "Nothing can green light a name on its own. This is a trading desk with multiple agents. This isn't one guy in a room."

**What that meant in practice.** The macro seat is the only one that back-fills a stance onto every name: where the read stated nothing about a name's sector, the broad market outlook was written into that name's slot and then counted as an independent per-name vote. One market opinion, counted once per name. So: a BROADCAST stance may no longer CORROBORATE a name (its dissent still counts), a SECTOR-SPECIFIC stance counts in full on both sides exactly as before, and a name whose only supporting seat is macro is refused at the conviction bar whatever strength that reading claims.

**Why the exclusion is ONE-SIDED — the correction that had to be made before merge.** The first build took the broadcast stance off BOTH counts and called that sign-symmetric. It LOOSENED the entry gate: dropping an OPPOSED seat raises the net, so technical bullish + earnings bullish + news bearish + broadcast-bearish macro goes from net 0 (refused today) to net +1 (traded). A double-count fix must never admit trades the desk refuses. His sign-symmetry ruling does not decide this and citing it was the error — "would help on a long or a short" is symmetry between the two SIDES OF A TRADE, which a one-sided removal satisfies exactly (flip the reading and flip the direction and the answer is unchanged). Support versus opposition is a different axis he was not asked about, and the defect itself decides it: a market-wide opinion must not manufacture agreement for a name nobody examined; it was never an argument for silencing a warning.

This is deliberately the OPPOSITE carve-out from `_is_broadcast_macro_verdict`, which drops a broadcast stance from OPPOSITION at the conviction bar. Not inconsistent — the difference is blast radius. There, opposition CULLS, and one outlook flip acting on every held name at once is a portfolio-wide forced liquidation wearing per-name clothes. Here it refuses one new entry. Both resolve to the same substance: a broadcast stance is never the reason the desk takes on risk and never the reason it dumps the book, but it may be the reason it declines a single name.

**There is no weight in the tally, and the honest reason is a missing sample, not "none was needed".** The §9.4 tally counts seats at a hard ±1 off plain stance strings; there is no conviction on a registry entry, so a low-confidence macro read and a high-confidence one are identical to it. Confidence weights the candidate RANKING only. A real per-seat weight is barred today by the owner's own August 2026 rule — derived from measured history, never chosen — and the minimum sample is 20 resolved calls against 7 closed round-trips, all recorded with conviction NULL. What shipped is therefore a binary counts/doesn't-count, and the weight belongs here when the sample exists.

**OPEN, and NOT settled by this work: two same-day rulings conflict.** The no-solo refusal is scoped to macro. `_has_supported_directional_thesis` excludes only technical, so news, earnings or smart_money can each still be a name's sole backer — and its own docstring concedes News and Smart-money always synthesise their invalidation, so a news-only name clears the bar on a templated falsifier. Under "nothing green-lights a name on its own" that is the same defect. The role-based bar (one supporting seat, no seat opposed) and the macro ruling BOTH landed 2026-09-25; neither outranks the other by age. Macro is scoped because it is the only seat holding a stance on a name it never examined. Widening means requiring two backers everywhere, which overturns rather than interprets the role-based bar — an owner call, flagged here, not taken.
**Moved from WORK.md (2026-09-24) —** **(a) To be decided by the orchestrator after an adversary run (2026-09-18 ruling) — does macro count as a seat in the agreement gate?** `count_aligned_sources` counts macro ±1 alongside technical/news/earnings/smart_money and flips macro's polarity for inverse ETFs, so the code treats a regime read as per-name evidence **by design**; the PM's sheet said twice, emphatically, that it never counts. On 2026-09-17 a name was killed when a bullish regime read cancelled a bearish filing, and another scored full agreement on macro plus a filing with no technical read at all. The sheet now states the truth and flags the disagreement; the GATE is unchanged because changing it moves trades. The regime-not-evidence side is the PM prompt's own provenance rule, **not** `docs/OUTCOME.md` — that miscitation has already been made twice. Either macro is per-name evidence or it is the regime the book sits in; decide, then make one of the two match. **(c) Dead weight.** ~35% of the PM's sheet, ~26% of the risk manager's and ~24% of the reviewer's is recitation of machinery the model does not perform. Deleting it is the durable fix; done only where a claim was false, because deleting a load-bearing recitation changes behaviour (the reviewer's trigger vocabulary is the clear case — a seat that does not know the words has every exit silently dropped).

## item 76

**Moved from WORK.md (2026-09-24) —** Settles with a before/after benchmark of whether the PM uses `reasoning_chain.macro_audit`, which needs the owner's go. Do NOT reopen as a size problem.

## item 77 — RETIRED 2026-09-30, bare pointer at the model-seat decision line

**Moved from WORK.md (2026-09-24) —** Blocked by owner decision 2026-09-15. **Retired 2026-09-30:** the board item was a bare pointer with no criteria of its own; its only criterion was closing with the `DECIDE BY 2026-10-31` model-seat line, which now names it as absorbed and keeps the owner ruling that nobody proposes the run. Items 17, 76 and 78 were checked the same day and stay open: 17 is a deferred owner decision with no backup channel built, 76 is blocked on a paid benchmark with its own criteria, 78 still has the live temporary isolate in the pipeline.

## item 81 — RETIRED 2026-09-24

**Moved from WORK.md (2026-09-24) —** `SUBFLOOR_SIZE_CAPPED_STATUS` (`src/agents/portfolio_manager.py:59`) is assigned nowhere and asserted only by `tests/test_subfloor_catalyst_gate.py:841`. Keeping a key so a silent rename cannot resurrect a threshold is a real argument, so decide once and record it — keep with a ledger note, or delete. NO LIVE NUMBER'S VALUE CHANGES either way.

**Retired 2026-09-24** — decided: delete. `SUBFLOOR_SIZE_CAPPED_STATUS`, `RiskConfig.min_reward_risk_after_widening` and `ConstructorConfig.min_reward_risk_after_widening` were removed as zero-reader dead code, each re-verified against the current tree first. `REWARD_RISK_FLOOR` itself was NOT deleted — `ops/model_policy/deterministic_selection.py` still reads it in a real comparison for the model-selection benchmark. Full writeup in `docs/INCIDENT_HISTORY.md` (2026-09-24 entry).

## item 107

**2026-09-26 — (a) and (c) shipped; (b) is the whole of what remains.** What was built and what it still cannot do: `docs/INCIDENT_HISTORY.md`, 2026-09-26. Three corrections to the filing below, each verified against the code that day: the evening tilt it lists was removed from the formula on 2026-09-17, the same day this item was filed — only a dangling mention of it survived, now gone; the 12 had NINE homes across five files, not three — five in Python (two comparisons, a flag label, a dataclass comment and a rendered summary line) and four in prompt prose — and now has one definition with one literal site left in `src/pipeline.py`; and the rendering mechanism reached three sheets, not two, because the technical sheet is rendered by its own module rather than by the shared one. The prompt-only arithmetic in (b) was re-checked symbol by symbol: nothing in `src/` computes any of it and none of it is in the number ledger.

**Moved from WORK.md (2026-09-24) —** Three gaps, none a wording fix: **(a) Behaviour that changed without being deleted** — nothing is registered, so nothing is scanned, and the shipped check is blind to the whole class. The live instance is item 108. **(b) Numbers that exist ONLY in prompt prose** — invisible to a code audit and to any drift check: the PM's whole sizing arithmetic (bases 3.0/1.75/0.75, +0.25 R/R bonus, ±0.20/±0.10 evening tilt, 0.5 stale halving at age ≥8d) and Tech's "3+ aligned signals", 1-3/4-7/8+ freshness tiers and forward-PE 40/60 + P/S 15/25 levels. Trade-governing, unsourced; source, derive or delete each. A DIFFERENT shape added 2026-09-18: the reviewer's `weight_pct > 12%` escalation and the `DRIFT` flag's matching 12 are bare inline literals in `src/pipeline.py` and `src/agents/portfolio_manager.py`, with no settings key and no named constant, so the number has three homes and the rendering mechanism can reach none of them. Name it, or move it to settings, before it can be rendered. **(c) Rendering coverage** — `prompt_limits.py` renders limits from live settings in 2 of 10 prompt files; the other eight hand-type every number. Mechanical where a number has a settings key; otherwise it is (b). It raises at agent construction, so a bad placeholder halts the desk — fail-closed, and a new way a settings edit stops trading.

## item 119 — RETIRED 2026-09-30, open-like proof measured: 15 of 15 series served from cache at the 13:30 UTC open and the 14:00 and 14:46 runs

**Moved from WORK.md (2026-09-24) —** On 2026-09-17 the morning open brought back 8 of the 15 required FRED series; the other 7 were never requested at all and the log said the deadline was exceeded. That is not a St. Louis outage: the observation calls and the due-date metadata calls share the same worker slots under the existing 90s ceiling, so a healthy batch spends nearly the whole clock and one slow series starves the rest. PR #435 built an observations-first fix (one attempt per series, then one bounded re-ask of the misses inside whatever budget remains, metadata on leftover budget, unknown freshness named rather than invented, ceiling NOT lengthened). **That code was never merged and the item does not inherit its verdict.** Its measurements — isolated series 1.5-6.7s, a clean full batch 15/15 in ~85s — were taken MID-MORNING, when FRED was already healthy. The defect is at the open. Mid-morning numbers are a starting point, not merge-readiness. Do not lengthen the timeout and do not invent a missing value; an incomplete set is a lost economics seat.

**DECISION 2026-09-26 — the economist is paid ONCE on what arrived, and the verdict is stamped partial. The sentence above ("an incomplete set is a lost economics seat") is SUPERSEDED for the paid call; it stands for the series values themselves, which are still never invented.**

Measured first, from production `agent_logs` and `quant_agent.log*` on the box (read-only):

- 19 paid `macro_analyst` calls carry a coverage section. TWO were formed on a partial set: 2026-09-17 13:33:56 UTC at 8/15 and 2026-09-22 13:34:56 UTC at 7/15. Both land within five minutes of the 09:30 ET open. Both are missing the same tail of `CONFIGURED_SERIES` (UNRATE, BAMLH0A0HYM2, DFII10, T10YIE, DTWEXBGS, BAMLC0A0CM, ICSA, plus PCEPI on the 7/15 run), every one of them named `fetch_deadline_exceeded` — never asked, not asked-and-refused.
- The verdict DID differ. 2026-09-17 came back `transitional`/`low` where 09-16 and 09-18 both said `risk-on`/`medium`. 2026-09-22 came back `risk-on`/`low` between a `risk-on`/`low` and a `risk-on`/`high`. So a partial set is not cosmetic: it moved the regime label once in two, and the economist self-downgraded confidence to `low` on both.
- Both partial runs predate the pre-open series cache (`2bb22eb2`, 2026-09-23 02:25 -0400). Every session since — 09-23, 09-24, 09-25 — returned 15/15. **That is three sessions. It is not evidence the defect is gone**, which is exactly why criterion (a) stays unticked and why item 187 stays open.
- Money: the economist runs on `gemini-3.5-flash-lite` at ~8.4k in / 1.4k out per call, recorded cost **$0.0000** over its last 10 calls, and its highest per-call cost ever recorded is **$0.0018** (2026-08-31, on the earlier model). For comparison the portfolio manager cost **$11.1359 over 74 calls** in the same 14 days (~$0.15 a call).

Why option 1 (pay once, label it partial) and not the other three:

- **Not "defer until the set is complete."** The economist is paid exactly ONCE A DAY, in the morning stage; midday, close and evening read `macro_summary` but never buy a macro verdict. Deferring therefore does not delay the read, it deletes it, and the book gets built at the open with no regime frame at all. Both measured partial runs had already burned the full 90s ceiling, so "wait for complete" has no known finish time to wait for. Under the standing ruling that macro is a weighted per-name input that can never green-light a name on its own, a labelled partial read is strictly more informative than an absent one.
- **Not "pay again when the set completes."** The money objection is weak — a second economist call is $0.0018 at worst — but the second verdict has no consumer. The book is already built, no later session pays for macro, and nothing is wired to unwind an order on a revised regime. The only way to make the repair matter is to re-run the portfolio manager at ~$0.15 a call, which is 80x the economist and sits under a ceiling that genuinely binds (the intraday scan alone is 62.8% of all model spend). Building that path for a trigger that has fired zero times since the cache shipped is PR #435's own mistake — optimising a path that has never succeeded.
- **Not "refuse below some coverage."** That needs a threshold. No literature or broker fact gives one, the desk's no-arbitrary-numbers rule bars picking one, and `tests/test_macro_partial_verdict.py::test_stamp_never_compares_coverage_against_a_threshold` pins the absence of a cutoff: 14/15 and 1/15 are both simply `partial`.

What shipped, and what the gap actually was. The desk already named the holes on the INPUT side (`MacroCoverage.describe()` in the economist's own prompt) and already raised the operator's degraded banner (`data_status["macro"] = "partial"`). It also already refused to pay twice: `"partial"` maps to `CATEGORY_REPORTED` in `src/evidence_gate.py`, never into `HEALABLE_CATEGORIES`, so the one-paid-retry seat heal cannot buy a second opinion on the same holes — that is now pinned by a test rather than left as an accident of the table. The real gap was the OUTPUT side: the verdict outlives the run and carried no trace. It is persisted by `MacroStore.save_last_state` (an explicit key whitelist, which silently dropped anything not listed), read back by midday/close/intra as `carried_from_morning` and by later days as `remembered`, rendered into the PM's sheet and the 7-day regime trajectory, and sent to the owner as the `📊 Market:` line. A 7/15 read looked identical to a 15/15 read on every one of them. `MacroCoverage.verdict_stamp()` now stamps `coverage_state`/`coverage_note` onto `MacroAnalysis` straight after validation — from the deterministic fetch record, never from the model's own self-assessment — and the stamp travels through all of those surfaces. `unknown` is the default and is deliberately not a claim in either direction, so pre-existing snapshots are neither laundered into "complete" nor given a caveat nothing supports.

## item 147 — RETIRED 2026-09-30

**Moved from WORK.md (2026-09-24) —** An inexact day blocks the quota rearm, so a provider omitting usage costs budget never spent.

**Investigated 2026-09-25, STILL OPEN, no code change — the honest fix is out of `cost_circuit.py`.** Since item 14 (2026-09-02) removed reservations, a null-usage success is no longer "charged the reserve": `complete_call` books $0 but increments `unknown_cost_rows`, marks the day inexact, and HARD-latches `unknown_actual_cost` (operator-only). Doctrine (`cost_circuit.py` ~112-115, added 2026-09-22) keeps that on purpose: "real tokens were generated; the unknown is real spend." Adversary-verified 2026-09-25: `complete_call` sees only `cost is None` and conflates TWO cases `src/agents/base.py` produces — (a) genuine zero-token success (`base.py` ~2340, no telemetry, real unknown spend, hard latch defensible) and (b) known non-zero tokens but the model is absent from the pricing table so `estimate_cost` returns None (`base.py` ~2374, `cost_table.py` `estimate_cost`). Case (b) is a pricing-data defect, not unbounded spend, and the hard latch over-punishes it. BUT `cost_circuit` cannot book a measured figure for (b) either — the missing thing is the per-token RATE, and the pricing table is the same one that failed, so there is no rate to multiply by. A real fix must (1) pass token counts from `base.py` into `complete_call` to route (a) vs (b) — which breaks the breaker's deliberate provider-independence — and (2) source a rate for the unpriceable model (add it to `cost_table`, needs a real number). Neither is verifiable/safe on the paper/free-data setup, and whether case (b) has EVER fired in production is unconfirmed (needs a prod-DB read of `llm_budget_days`/`agent_logs`; the "three null-cost rows on 2026-08-31" are not classified (a)-vs-(b)). Left for owner decision; no behaviour changed.

**Measured and half-fixed 2026-09-26 — the filed premise does not survive the data.** Production DB, read-only: 667 `agent_logs` rows over 2026-08-14..2026-09-26, exactly 7 with `cost_usd IS NULL`. All 7 are `smart_money_analyst` synthesis-cache hits (`provider_requests=0`, `latency_s=0.0`, `input_message='[cached evidence hash]'`, 0/0 tokens, `status='success'`) — six on 2026-08-31, one on 2026-09-17. No provider was called, so nothing was spent and nothing was over-charged; `llm_budget_days` shows `unknown_cost_rows=0, costs_exact=1` for both days, because the day row is seeded at 12:00:41 UTC and the cache hits land from 14:23 onward. `unknown_actual_cost` has never tripped in production (0 events in `llm_circuit_events`), and zero rows have tokens > 0 with a NULL cost, so the (a)-vs-(b) split this section left open resolves as: case (a) never, case (b) never, and all 7 rows are a third case neither branch describes — no call at all.

The real defect that remained is the day seeder, not `complete_call`: `_seed_day_locked` counted every NULL-cost row as unknown, so on any day whose `llm_budget_days` row is created AFTER a cache-hit row (a mid-day deploy — the exact case the seeder exists to handle, or a restored/standalone breaker DB), the day seeds inexact and arms `legacy_unknown_cost`, an operator-only hard latch, over calls that provably cost nothing. That is the 2026-09-16 failure shape (events 27/28) with a different source row. Fixed two ways: the cache path now books an exact `cost_usd=0.0`, and the seeder forgives a row proven free by its own record. The proof is deliberately narrow, in the spirit of `_KNOWN_ZERO_COST_STATUS_CODES` — `status='success'` is load-bearing because `src/pipeline.py`'s evening exception path also synthesises `provider_requests=0` with no cost after a call that may have reached the provider, and NULL `provider_requests` (194 legacy rows) is not a proof of zero.

Still open and unchanged on purpose: a success whose provider request DID happen and returned no telemetry. No rate source exists to charge it at, so it keeps booking unknown and latching. Charging it anything invented would be the reservation layer under a new name, which item 14 deleted for cause.

Retired from the queue 2026-09-30: the filed premise (a full reservation eating budget on an untelemetered success) never existed in the data, and the only real defect it uncovered (the day seeder latching legacy_unknown_cost on a proven-zero cache hit) shipped 2026-09-26 per item 14's cost_circuit.py fix. What remains — a success whose provider request truly happened with no usable cost or token telemetry — has zero measured occurrences across the full agent_logs history, so there is no defect left to build against; it stays a documented edge case above, not a queue item.

## item 119 — RETIRED 2026-10-01, (b) shipped and the remaining criterion (a) is the same observation item 187 already owns

**RETIREMENT 2026-10-01.** Criterion (b) was met 2026-09-26 (below). Criterion (a) — "every required series demonstrably gets a real attempt inside the existing ceiling, proven against open-like conditions" — has no build left in it: item 187 shipped the fair-share reserves, the pre-open series cache and the move of the release-schedule wire off the trading path, and its own fourth criterion is the exact proof (a recorded production open with `series_not_attempted` empty). Keeping 119 open would have two items waiting on one row. Criterion (a) is deferred to item 187.

**Measured 2026-10-01, production database, read-only** (`/home/qamc/quant-agent/data/quant_agent.db`): `fred_fetch_coverage_runs` exists and holds **0 rows**. That is NOT a dead recording. The deployed checkout is at the same commit as `origin/main`, the insert at the macro stage is unconditional and not behind a flag, and the newest `macro_analyst` row is 2026-09-30 14:04:11 UTC — no macro-bearing session has run since the recording deployed. The first row is due at the next morning open; until one exists, item 187 cannot tick its fourth criterion either.

The health fault family `economics_feed_incomplete` in `src/log_health.py` pointed at this item. Its patterns are all fetch FAILURES (`FRED fetch deadline`, `Macro event calendar PARTIAL`, a coverage line with `FAILED:`), which is item 187's subject, not 119(b)'s stamp. Repointed to 187.


**Moved from WORK.md (2026-09-24) —** On 2026-09-17 the morning open brought back 8 of the 15 required FRED series; the other 7 were never requested at all and the log said the deadline was exceeded. That is not a St. Louis outage: the observation calls and the due-date metadata calls share the same worker slots under the existing 90s ceiling, so a healthy batch spends nearly the whole clock and one slow series starves the rest. PR #435 built an observations-first fix (one attempt per series, then one bounded re-ask of the misses inside whatever budget remains, metadata on leftover budget, unknown freshness named rather than invented, ceiling NOT lengthened). **That code was never merged and the item does not inherit its verdict.** Its measurements — isolated series 1.5-6.7s, a clean full batch 15/15 in ~85s — were taken MID-MORNING, when FRED was already healthy. The defect is at the open. Mid-morning numbers are a starting point, not merge-readiness. Do not lengthen the timeout and do not invent a missing value; an incomplete set is a lost economics seat.

**DECISION 2026-09-26 — the economist is paid ONCE on what arrived, and the verdict is stamped partial. The sentence above ("an incomplete set is a lost economics seat") is SUPERSEDED for the paid call; it stands for the series values themselves, which are still never invented.**

Measured first, from production `agent_logs` and `quant_agent.log*` on the box (read-only):

- 19 paid `macro_analyst` calls carry a coverage section. TWO were formed on a partial set: 2026-09-17 13:33:56 UTC at 8/15 and 2026-09-22 13:34:56 UTC at 7/15. Both land within five minutes of the 09:30 ET open. Both are missing the same tail of `CONFIGURED_SERIES` (UNRATE, BAMLH0A0HYM2, DFII10, T10YIE, DTWEXBGS, BAMLC0A0CM, ICSA, plus PCEPI on the 7/15 run), every one of them named `fetch_deadline_exceeded` — never asked, not asked-and-refused.
- The verdict DID differ. 2026-09-17 came back `transitional`/`low` where 09-16 and 09-18 both said `risk-on`/`medium`. 2026-09-22 came back `risk-on`/`low` between a `risk-on`/`low` and a `risk-on`/`high`. So a partial set is not cosmetic: it moved the regime label once in two, and the economist self-downgraded confidence to `low` on both.
- Both partial runs predate the pre-open series cache (`2bb22eb2`, 2026-09-23 02:25 -0400). Every session since — 09-23, 09-24, 09-25 — returned 15/15. **That is three sessions. It is not evidence the defect is gone**, which is exactly why criterion (a) stays unticked and why item 187 stays open.
- Money: the economist runs on `gemini-3.5-flash-lite` at ~8.4k in / 1.4k out per call, recorded cost **$0.0000** over its last 10 calls, and its highest per-call cost ever recorded is **$0.0018** (2026-08-31, on the earlier model). For comparison the portfolio manager cost **$11.1359 over 74 calls** in the same 14 days (~$0.15 a call).

Why option 1 (pay once, label it partial) and not the other three:

- **Not "defer until the set is complete."** The economist is paid exactly ONCE A DAY, in the morning stage; midday, close and evening read `macro_summary` but never buy a macro verdict. Deferring therefore does not delay the read, it deletes it, and the book gets built at the open with no regime frame at all. Both measured partial runs had already burned the full 90s ceiling, so "wait for complete" has no known finish time to wait for. Under the standing ruling that macro is a weighted per-name input that can never green-light a name on its own, a labelled partial read is strictly more informative than an absent one.
- **Not "pay again when the set completes."** The money objection is weak — a second economist call is $0.0018 at worst — but the second verdict has no consumer. The book is already built, no later session pays for macro, and nothing is wired to unwind an order on a revised regime. The only way to make the repair matter is to re-run the portfolio manager at ~$0.15 a call, which is 80x the economist and sits under a ceiling that genuinely binds (the intraday scan alone is 62.8% of all model spend). Building that path for a trigger that has fired zero times since the cache shipped is PR #435's own mistake — optimising a path that has never succeeded.
- **Not "refuse below some coverage."** That needs a threshold. No literature or broker fact gives one, the desk's no-arbitrary-numbers rule bars picking one, and `tests/test_macro_partial_verdict.py::test_stamp_never_compares_coverage_against_a_threshold` pins the absence of a cutoff: 14/15 and 1/15 are both simply `partial`.

What shipped, and what the gap actually was. The desk already named the holes on the INPUT side (`MacroCoverage.describe()` in the economist's own prompt) and already raised the operator's degraded banner (`data_status["macro"] = "partial"`). It also already refused to pay twice: `"partial"` maps to `CATEGORY_REPORTED` in `src/evidence_gate.py`, never into `HEALABLE_CATEGORIES`, so the one-paid-retry seat heal cannot buy a second opinion on the same holes — that is now pinned by a test rather than left as an accident of the table. The real gap was the OUTPUT side: the verdict outlives the run and carried no trace. It is persisted by `MacroStore.save_last_state` (an explicit key whitelist, which silently dropped anything not listed), read back by midday/close/intra as `carried_from_morning` and by later days as `remembered`, rendered into the PM's sheet and the 7-day regime trajectory, and sent to the owner as the `📊 Market:` line. A 7/15 read looked identical to a 15/15 read on every one of them. `MacroCoverage.verdict_stamp()` now stamps `coverage_state`/`coverage_note` onto `MacroAnalysis` straight after validation — from the deterministic fetch record, never from the model's own self-assessment — and the stamp travels through all of those surfaces. `unknown` is the default and is deliberately not a claim in either direction, so pre-existing snapshots are neither laundered into "complete" nor given a caveat nothing supports.

## item 147 — RETIRED 2026-09-30

**Moved from WORK.md (2026-09-24) —** An inexact day blocks the quota rearm, so a provider omitting usage costs budget never spent.

**Investigated 2026-09-25, STILL OPEN, no code change — the honest fix is out of `cost_circuit.py`.** Since item 14 (2026-09-02) removed reservations, a null-usage success is no longer "charged the reserve": `complete_call` books $0 but increments `unknown_cost_rows`, marks the day inexact, and HARD-latches `unknown_actual_cost` (operator-only). Doctrine (`cost_circuit.py` ~112-115, added 2026-09-22) keeps that on purpose: "real tokens were generated; the unknown is real spend." Adversary-verified 2026-09-25: `complete_call` sees only `cost is None` and conflates TWO cases `src/agents/base.py` produces — (a) genuine zero-token success (`base.py` ~2340, no telemetry, real unknown spend, hard latch defensible) and (b) known non-zero tokens but the model is absent from the pricing table so `estimate_cost` returns None (`base.py` ~2374, `cost_table.py` `estimate_cost`). Case (b) is a pricing-data defect, not unbounded spend, and the hard latch over-punishes it. BUT `cost_circuit` cannot book a measured figure for (b) either — the missing thing is the per-token RATE, and the pricing table is the same one that failed, so there is no rate to multiply by. A real fix must (1) pass token counts from `base.py` into `complete_call` to route (a) vs (b) — which breaks the breaker's deliberate provider-independence — and (2) source a rate for the unpriceable model (add it to `cost_table`, needs a real number). Neither is verifiable/safe on the paper/free-data setup, and whether case (b) has EVER fired in production is unconfirmed (needs a prod-DB read of `llm_budget_days`/`agent_logs`; the "three null-cost rows on 2026-08-31" are not classified (a)-vs-(b)). Left for owner decision; no behaviour changed.

**Measured and half-fixed 2026-09-26 — the filed premise does not survive the data.** Production DB, read-only: 667 `agent_logs` rows over 2026-08-14..2026-09-26, exactly 7 with `cost_usd IS NULL`. All 7 are `smart_money_analyst` synthesis-cache hits (`provider_requests=0`, `latency_s=0.0`, `input_message='[cached evidence hash]'`, 0/0 tokens, `status='success'`) — six on 2026-08-31, one on 2026-09-17. No provider was called, so nothing was spent and nothing was over-charged; `llm_budget_days` shows `unknown_cost_rows=0, costs_exact=1` for both days, because the day row is seeded at 12:00:41 UTC and the cache hits land from 14:23 onward. `unknown_actual_cost` has never tripped in production (0 events in `llm_circuit_events`), and zero rows have tokens > 0 with a NULL cost, so the (a)-vs-(b) split this section left open resolves as: case (a) never, case (b) never, and all 7 rows are a third case neither branch describes — no call at all.

The real defect that remained is the day seeder, not `complete_call`: `_seed_day_locked` counted every NULL-cost row as unknown, so on any day whose `llm_budget_days` row is created AFTER a cache-hit row (a mid-day deploy — the exact case the seeder exists to handle, or a restored/standalone breaker DB), the day seeds inexact and arms `legacy_unknown_cost`, an operator-only hard latch, over calls that provably cost nothing. That is the 2026-09-16 failure shape (events 27/28) with a different source row. Fixed two ways: the cache path now books an exact `cost_usd=0.0`, and the seeder forgives a row proven free by its own record. The proof is deliberately narrow, in the spirit of `_KNOWN_ZERO_COST_STATUS_CODES` — `status='success'` is load-bearing because `src/pipeline.py`'s evening exception path also synthesises `provider_requests=0` with no cost after a call that may have reached the provider, and NULL `provider_requests` (194 legacy rows) is not a proof of zero.

Still open and unchanged on purpose: a success whose provider request DID happen and returned no telemetry. No rate source exists to charge it at, so it keeps booking unknown and latching. Charging it anything invented would be the reservation layer under a new name, which item 14 deleted for cause.

Retired from the queue 2026-09-30: the filed premise (a full reservation eating budget on an untelemetered success) never existed in the data, and the only real defect it uncovered (the day seeder latching legacy_unknown_cost on a proven-zero cache hit) shipped 2026-09-26 per item 14's cost_circuit.py fix. What remains — a success whose provider request truly happened with no usable cost or token telemetry — has zero measured occurrences across the full agent_logs history, so there is no defect left to build against; it stays a documented edge case above, not a queue item.

## item 203

**Measured against production 2026-10-01 (`/home/qamc/quant-agent/data/quant_agent.db`, read-only; the repo-local DB is empty).** Across all 713 `agent_logs` rows, 2026-08-14 to 2026-10-01: ZERO rows where a provider request happened (`provider_requests > 0`) and no token counts and no cost came back. The eleven rows carrying no token counts at all are all `provider_requests = 0` -- no request was made -- which is item 147's already-settled cache-hit case. 238 successes report $0 against real token counts, all from the `google` free route: a correctly measured zero, not a hole. So the occurrence count this item was filed to watch for is still zero, and nothing can be priced from an occurrence that has never happened -- the item stays OPEN rather than being closed on a fallback price the desk would have to invent.

**What shipped anyway (2026-10-01): the case is now counted as unknown instead of banked as a measured zero.** `_unknown_cost_row_expr` in `src/cost_circuit.py` scored a row unknown only when `cost_usd IS NULL`. A success whose provider request happened and returned no usage would be written with a literal `cost_usd = 0.0` -- zero because nothing was reported, not because anything was measured -- and that row read to the budget as a proven free call. It now scores 1 (unknown) whenever `telemetry = 'no_usage'`, which `src/agents/base.py:usage_telemetry_word` writes only for a row whose request reached the provider. Disjoint from `_PROVEN_ZERO_ROW_SQL` by construction, and the clause is dropped (not fabricated) on an older `agent_logs` with no `telemetry` column. No price is substituted from a model list, an average of other calls, or anything else: the day simply loses `costs_exact` and the hole stays visible.

**The `telemetry` recorder (#879, merged 2026-09-30) has captured nothing in production yet -- UNPROVEN, not proven dead.** All 713 rows have `telemetry` NULL. The only session since the merge ran 2026-10-01 00:01-00:02 ET while the production checkout's HEAD was being updated at 00:01:13 ET, so no row has yet been written by a process that provably loaded the recorder. Re-check after the next full session: three consecutive successes still NULL would make it dead and a defect in its own right. Until a row carries the word, the clause added above is correct but unexercised in production.

Filed 2026-09-30, carried over from item 147 at retirement. Item 147 measured zero rows in agent_logs where a provider request actually happened and returned no usable cost or token telemetry, so nothing needs building today; this item exists only so that case is tracked if it ever fires, rather than silently dropped when 147 was retired.

## item 157 — RETIRED 2026-09-30, the enforced answer format shipped on both wire routes; the live-call criterion is structurally unreachable and was replaced by an on-every-real-call hygiene recorder

**Live-call criterion is BLOCKED on a credential grant, not on effort (measured 2026-09-26).** The rehearsal identity the desk uses for live proofs is granted the broker, economics, messaging and general-model credentials and is NOT granted the Google one, so the only identity that can make the confirming call is production — a live attempt spends real money on the shared account. Either grant the Google credential to the rehearsal identity or accept one production-billed call; until then this box cannot be ticked from a rehearsal. Evidence: `docs/INCIDENT_HISTORY.md` (2026-09-26).

**Moved from WORK.md (2026-09-24) —** Per that write-up, constrained output needs a wrapper object (answer is a bare list, strict schema needs an object), a separate model-facing schema (eight desk-filled fields), `strict=false` (one free-form map field), and a live call to confirm the Google route actually enforces a sent schema — untried.

**2026-09-30 — the case is now RECORDED, not yet priced.** `agent_logs.telemetry` says `complete`, `no_cost` (tokens known, no price) or `no_usage` (no token counts), so a missing measurement no longer looks like a measured zero; NULL on every older row means unknown. Recording only: the pricing/exclusion question in the criterion stays open, and the free model in use today is the normal source of `no_cost`/`no_usage` rows.

**2026-10-01 — measured against production (713 agent_logs rows, read-only; telemetry column not yet deployed there, so classified from token and cost fields).** Zero successful rows have no token count at all, so the no-usage case has never fired in production. The no-cost case is the whole direct-Google route (238 of 238 successes, tokens present, cost zero) and it is a PRICE-TABLE zero, not provider silence: that model is pinned at 0.0 input and output in the cost table. Every OpenRouter and earlier-route success carries a non-zero cost. Still open: whether the 0.0 pin is genuinely free (then exclude from unknown-cost counting) or a placeholder to price; that is a pricing call, not a telemetry gap. Separate unfiled gap noticed, not fixed: the cost circuit's manual reset records no duration and sends no alert.

## item 174

**Manual reset made as observable as the automatic one (2026-10-01).** The same hole existed on the operator's `reset`: its event row recorded no suspension duration, captured no suspension `alert_state` before the same transaction zeroed it (so the pairing column fell back to its DEFAULT 1, an unchecked claim), and no owner notification fired at all — the desk could stop, be restarted by hand, and the owner learn nothing from either end. The row now carries the elapsed suspension in minutes, derived from the singleton's own `suspended_at` against SQLite's clock exactly as `_auto_clear_transient_latch_locked` derives it, and omitted entirely when `suspended_at` is NULL (a reset clearing only an inexact day or a durable marker) rather than reported as zero. The recovery scan now covers `reset` rows alongside `auto_reset`, so the same claim/retry state machine, the same unpaired-suppression rule and the same notifier carry both; the closing line of the alert branches, because "no operator reset was needed" is false of a manual reset. A one-time backfill (SQLite's own `user_version`, unused elsewhere in this schema) marks the `reset` rows already on the books handled, so no stale RESUMED burst fires on deploy.

**Measured against production 2026-10-01** (`/home/qamc/quant-agent/data/quant_agent.db`, read-only): 9 `reset` rows and 22 `auto_reset` rows. Every one of the 9 manual rows holds `recovery_alert_state=0` (never announced), `suspension_alert_state=1` by column default rather than by capture, and no duration anywhere in its detail text. This also corrects the note below: production `auto_reset` events are no longer zero — 22 have occurred — so the cooldown/allowance criterion can now be re-read against real occurrences.

**Pairing added 2026-09-26 —** the resume alert is now tied to a suspension alert the owner actually received. When the "SUSPENDED" send fails, the auto-clear wipes `suspended` and `alert_state`, so that suspension alert becomes permanently undeliverable; sending "RESUMED" anyway reported a recovery from an incident he was never told about. The auto-clear now records the suspension's alert state at the last moment it is knowable, and an unpaired resume is resolved without sending. Also: the offline harness could not reach this path at all (its 503 is pre-generation, hence provably free, hence never latches), so a `server_error_mid_stream` kind was added and a morning rehearsal reproduced latch → suspension alert → auto-expiry → paired resume alert end to end. Detail in `docs/INCIDENT_HISTORY.md`.

**Alert shipped 2026-09-25 —** the auto-expiry now sends the owner the same-surface Telegram alert the suspension does (🟢 RESUMED, naming the forgiven trigger and that it auto-expired), keeping the `auto_reset` DB event and log; delivery is durable/retryable with the same claim state machine the quota-recovery alert uses. Still open: the two unledgered constants below remain unmeasured.

**Moved from WORK.md (2026-09-24) —** Also unmeasured: the 15-min cooldown (midpoint of the 30-min paid-run gap) and the 19/day allowance (one per paid run) have not met a real occurrence, and neither is covered by the number-ledger check.

**Ledger gap is STRUCTURAL, not an oversight (verified 2026-09-25).** Both constants live in `LLMCostCircuitConfig`, which is DELIBERATELY outside the ledger's `SCOPED_CONFIG_CLASSES` (the scanner's own comment names "LLM cost circuits" as settings with "nothing to do with a trade"). Closing the gap would mean scoping the whole cost-circuit class and ledgering every numeric field in it — a scope-policy change, not bookkeeping — and even then `max_transient_latch_auto_clears_per_day` uses a `default_factory` (`_paid_run_count()`), which the scanner structurally cannot see (its own docstring lists this as uncatchable). So there is no clean two-row ledger add here; left for the owner/scope call, not fixed in the item-148 pass. **Separate finding, not mine to fix:** `intra_check` is the desk's LARGEST model spender — 72% of spend on 2026-09-22, 90% on 09-21, 13-14 paid runs a day [measured] — while its own code comment said "no LLM"; comment corrected, but whether a 30-min tick should be spending that is untouched.

## item 187

**2026-10-01 — the closing observation now records itself.** Before this, coverage lived only as a prose line inside the macro seat's prompt (production `agent_logs`, `macro_analyst` rows 2026-09-21..09-30: 10 rows, 9 stating full coverage and 1 stating 7/15 on 09-22 [measured, production database, read-only]) and as a 21-day state file the box user alone can read. Neither covered the event-calendar half, neither was structured, and neither kept "asked and failed" apart from "never asked". The table `fred_fetch_coverage_runs` (one row per morning fetch, built from the coverage objects themselves) fixes that. The event-calendar provider cannot tell a release it asked for from one cut off by the deadline, so those failures are stored with their own reason text and never labelled either way. No row exists for any open before the deploy.

**Moved from WORK.md (2026-09-24) —** Also: every FRED failure in the log is `fetch_deadline_exceeded`, 4 of 12 runs full coverage, worst 5/15 [measured 09-17..23] — owned by the approved fetch redesign.

**2026-09-30 — the series half is done; the failure that remained was a DIFFERENT fetch.** Re-measured against the production log (`/home/qamc/quant-agent/quant_agent.log*`, read-only): every surviving `fetch_deadline_exceeded` line on 09-29 and 09-30 belongs to the macro EVENT CALENDAR (`MacroEventCalendarProvider`, seven `/fred/release/dates` calls), not to the fifteen-series macro fetch. The series fetch has been clean since the fair-share reserve and the pre-open series cache shipped, and the box was running that code (`per_series_reserve_s` present in the deployed `src/data/macro.py`, file dated 2026-09-30 12:45 UTC).

The calendar had already been given item 187's fair-share split, and it was **not enough**: on 2026-09-30, with the split deployed, the 13:33 morning run returned 1/7 and the 14:04 run 0/7, with a mix of `fetch_deadline_exceeded`, read timeouts and an HTTP 502 [measured, same log]. Dividing the same twenty seconds more fairly cannot make seven serial HTTPS round trips to FRED reliable in the minutes after the opening bell — which is the identical conclusion item 119 reached about the fifteen series, and the identical remedy applies.

**Fixed by moving the wire off the trading path**, not by widening the deadline and not by dropping releases from `MACRO_RELEASES`. Both of those were considered and refused explicitly: the pre-open prefetch already times the same class of work at 0.4-1.9 s per call under no pressure, so the ceiling was never the binding constraint, and every release in the list is a single-day volatility event the desk is supposed to see coming. `ReleaseScheduleCache` stores each release's published forward dates on disk; only the new `prefetch_release_schedules()` writes it, run from the existing `quant-agent-macro-prefetch` job (08:45 and 18:30 ET weekdays) at the SAME `total_fetch_deadline_s` the trading path uses — a prefetch needing a bigger ceiling than the thing it replaces would be the widened timeout this item exists to refuse.

Honesty contract, unchanged and pinned by tests: a cached answer is reported as cached with its age in the coverage prose the seat reads, a cache entry whose query window cannot reach the horizon asked about is a miss rather than a silent "nothing scheduled", an exhausted schedule is a miss, and a release that neither the cache nor the wire could answer stays a named failure. No new threshold was introduced — the age test reuses `FOMCCalendar`'s existing `cache_ttl_days` on the same class of data, and the coverage test reuses the existing `RELEASE_SCHEDULE_LOOKAHEAD_DAYS`.

**MEASURED 2026-09-30 against the production database (`agent_logs`, 2026-08-14 to 2026-09-30) — the quality half cannot be answered from the record, and that is the finding.**

Exposure at the three decision seats: the free model has produced exactly THREE answers there in the whole history — portfolio manager 1 (2026-09-30 14:47, tier-3 Google-direct after the HTTP 402), risk manager 1 (same run), position reviewer 1 (2026-09-21, the OpenRouter-served copy). All three parsed as JSON, all three carried the seat's reasoning chain, the portfolio-manager one carried a falsifier; none was truncated (`finish_reason` = `stop`). Three answers is an existence proof, not a rate, and no honest usable-rate can be quoted from it [measured].

Why a rate cannot be computed for ANY model, free or paid: `agent_logs` records the responding model, but nothing records whether the seat ACCEPTED that answer. The only usability-shaped `status` in the whole table is the risk manager's `agent_failure`/`ok` pair (17 rows); every other row is `success`, which means the provider call returned, not that the seat could use the reply. The distortion is visible: 15 of 15 portfolio-manager rows and 19 of 55 risk-manager rows are stored as `success` while their `full_response` is not JSON at all, so the record cannot separate "the seat refused this" from "this seat stores prose" [measured]. A seat that rejects an answer and refuses leaves no row tying that rejection to the model that caused it.

So the second DONE WHEN box cannot be closed by reading the record, and the benchmark route is closed too (the owner has ruled out paid credits while the board is full). The fix is the recording: every decision seat must persist, beside the model that answered, whether its answer passed the seat's own acceptance gate and, when it did not, why — then the rate falls out of the database on the next total-OpenRouter outage instead of needing a paid benchmark. Until that exists the desk is running its decision seats on an unmeasured model as a matter of course, and the true state is "unknown", not "degraded".



**Measured from records 2026-10-01 (no paid run).** Read the production database read-only (`/home/qamc/quant-agent/data/quant_agent.db`): 713 `agent_logs` rows, of which 230 belong to the three decision seats (portfolio manager 142, risk manager 56, position reviewer 32). The free `gemini-3.5-flash-lite` route answered exactly 3 of them, one per seat, all marked `fallback` [measured, 3 rows]. All 3 parse as JSON and carry every field the seat's other answers carry; the other 227 rows parse too, with none blank or truncated [measured, 230 rows]. The comparison is impossible: n=1 per seat gives no rate, and the only stored verdict (`status`) records failure of the paid route (14 grounding or agent failures), never whether a free answer would have passed the seat's own gate. Verdict: the records cannot answer this; the item's closing condition is the per-answer acceptance recording, and it stays open.

## item 190

**Filed 2026-09-30, out of the order-placement gates review.** The desk built a feature to automatically sweep idle cash into a short-term Treasury-bill fund, then turned it off. Turning it off did not remove it: the code that runs it is still wired into every trading session, just switched to do nothing. A one-line flag flip would turn it back on with no further review.

That earlier review found one small piece of this dormant feature — a reserve band that decides how much cash to hold back — could not simply be deleted, because deleting it while the rest of the sweep stays wired in would leave the feature reachable with no record of what it does. Properly retiring the whole feature is a big, mechanical job: an estimated 187 places in the code and its tests still refer to it.

Right now nobody owns that job. It is only described inside another item's writeup, where it risks being forgotten once that item's narrower question is answered. This item exists so the cleanup has its own visible line on the board until it is actually done.

**No owner decision needed here** — this is an engineering bookkeeping fix (remove dead, switched-off code) rather than a money or risk-appetite question.

## item 194

**Filed 2026-09-30.** When the desk buys something it works out, from the chart, the nearest price level the stock has to get through on the way up, and that becomes the profit target it quotes you. The number is then frozen for the life of the position.

Until now the desk only revisited that number when the level it was measured against **disappeared** — the stock jumped clean over it and the ceiling was gone. It never revisited it when the opposite happened: the stock spent a few weeks building a **new** ceiling somewhere between where the desk bought and the number it was quoting. In that case the desk carries on quoting a target with a wall in front of it, which is exactly the thing the target is supposed to be. That is now fixed — a new wall counts as a reason to work the number out again, the same way a broken wall does. The number is still always **worked out from the chart**, never typed in, and still measured from the original buy price so it cannot drift upwards just because the stock went up.

What is left open is the **way in**. The recalculation only runs on a stock one of the desk's analysts has specifically raised a hand about. A stock that quietly grows a wall while nobody mentions it gets reported every morning and never recalculated. Two positions are in exactly that state right now: Apple and Nokia. Whether the morning report should be allowed to trigger the recalculation by itself is the open question — it would mean an automatic change to a live position's record, which is not something to switch on without a decision.

**No owner decision needed on the fix itself** — it is the same measurement the desk already does, run in one more circumstance. The open question above may need one, because it changes live position records without a human in the loop.

**RESOLVED 2026-10-01.** The way in is no longer the flag. `_adjudicate_target_revision_flags` now builds its work list from the OPEN BOOK: every held position is adjudicated every session, and a seat flag only supplies the seat label and the prose evidence for the symbols it did raise. Both Apple and Nokia are open positions, so both are in the swept population from the next session on, each producing either a re-derivation or a named, recorded refusal; the row appears when that session runs, not at merge.

**Why this did not need the owner's decision, and why it introduced no number.** The flag was argued above to be the human in the loop. It is not: the seat is a model, and `src.models.TargetRevisionFlag` has exactly two fields, a symbol and a prose evidence string, with no price. Every number the adjudication uses is either recomputed from bars that session or read off the already-ratified `risk_engine.config` bars, so the flag supplies no input to the arithmetic and removing it as a gate changes nothing about HOW a target is worked out — only about WHICH positions get the measurement. The algebra was checked explicitly for the trap of a number that looks per-name but cancels to a constant: no term was added, removed or rearranged in the derivation at all, so there is nothing new to source and no new ledger row. What the gate actually produced was arbitrary coverage, and the cost of that coverage is live: the stored target decides which trailing-stop regime a range trade is in (`src/risk/trailing.py` — below the target the breakeven and +2R ratchets hold the stop, above it the structural trail takes over), so a target left aiming past a wall nobody mentioned was already governing a live stop.

**MEASURED 2026-10-01, production DB read-only** (`/home/qamc/quant-agent/data/quant_agent.db`): the book holds 11 open positions, and across the entire record only 3 distinct symbols have ever carried a `target_revision` outcome row. That is the size of the gap the flag-only gate left — the sweep takes the session's coverage from whatever a seat happened to say to all 11.

**ROUND 2, 2026-10-01 — the sweep shipped a one-way tightening, now fixed, and the item is UN-RETIRED for two brakes it still lacks.**

*What actually changes at merge: nothing* [MEASURED 2026-10-01, production DB read-only, `/home/qamc/quant-agent/data/quant_agent.db`]. Of the eleven open positions, ten carry a live target identical to their entry target, and exactly one — a SHORT range trade — differs, by about 2.2% of price. For that one, current price sits OUTSIDE the band between the two targets, so it is on the same side of both boundaries. No position changes trailing regime on the day this merges. That is the strongest thing that can be said for the change and it was missing from the first telling: the correction is to the rule, not to today's stops.


*The money fault.* A wall-triggered re-derivation can move a target DOWN toward entry. The stored target is what `_apply_deterministic_trails` passed as the trailing `reference_target`, and that boundary decides whether a range trade is held by the below-target breakeven/+2R ratchets or by the structural trail. Crossing it tightens the stop; the trail only ever ratchets toward price; so when the next session restores the old target the stop is NOT given back. Oscillation did not cancel, it accumulated tightening — and the sweep had just widened that exposure from the handful of flagged names to the whole book, with the give-back case (a winner stopped out on a pullback it would have ridden) measured nowhere. FIXED by reading the entry column, `trades.initial_take_profit`, instead of the live `take_profit`. **This moves protection in BOTH directions and both are intended.** It removes a ratchet that could never be given back; it also LOOSENS the boundary case, because where a revised target sits below the entry target, a fall to just under the old boundary used to hand the stop to the structural trail and now leaves it in the earlier ratchets — strictly less tightening there, which is a real loosening of future protection. The trade is accepted because the ratchet it removes was irreversible and this loosening is not, but it is a trade and not a pure win. **"Pinned" is the INTENT of that column, not a verified property of every row:** the `initial_take_profit` migration backfilled it FROM `take_profit` for every legacy row carrying a target, so a row revised before the migration ran was backfilled with an already-revised number, and whether any such row exists is UNVERIFIED. The null fallback is safe either way, since it only applies to rows the migration left empty. No number was involved: the code's own comment at that call site already claimed every field it read was pinned at entry, and `take_profit` stopped being pinned the day `update_open_take_profit` was written. The regime boundary is now a property of the trade, so a revision cannot ratchet a stop the desk would not otherwise have moved, and the revised target is once again a quoted number that no deterministic gate reads — which is what the owner's 2026-09-30 alignment ruling says it should be. It is not inert: it still feeds `distance_to_target_pct` into the facts the reviewing model reads, so it can still colour a later session's prose.

*Three smaller faults, fixed.* The sweep now reads its bars in ONE `get_ohlcv_batch` call rather than a serial per-name fetch that the seat flag had been quietly rationing; no timeout constant was added, because any seconds value would be invented and batching removes the serial exposure that motivated one. Every name is adjudicated inside its own guard, so a mid-sweep exception costs exactly one name and that name gets a durable `FAULT_POSITION_NOT_MEASURED` row — previously one exception unwound to a single `try` at the call site that returned an empty list, and because the work list is sorted the SAME tail of the book was dropped every time, each dropped name keeping a stored target the record did not mark as unverified. And an unapplied outcome identical to the symbol's last one is no longer re-filed, the same rule `record_trail_state_if_changed` already applies to trail state.

*Stated plainly, not fixed.* A revision binds from the NEXT session: the trails run before the sweep in the same review, deliberately, so that a revision cannot reach the exits that have already run.

*STILL OPEN, and why the item is un-retired.* (1) Only the level-broken trigger has a brake. The ATR-reach and wall triggers recompute off today's ATR and today's level set with no hysteresis and no cooldown, so the quoted target can flip session to session; the trailing stop carries both a cooldown and a noise band for exactly this failure mode. The fix must be a brake of the same KIND as the two-close confirmation — a second day's agreement — and not a damping constant, because no damping constant can be sourced and borrowing the trail's cooldown, which is owner appetite for a different decision, would be the same invented number with a newer date. (2) The outcomes reach the dashboard (`src/api/holding_why.py` reads the `target_revision` evidence rows) and reach Telegram nowhere: `src/notifier.py` and `src/trader_feed.py` contain no rendering for them at all. Work that reaches neither surface did not happen for the owner, and this one reaches only half of them.

**What this deliberately does NOT do.** It does not make the target a sell rule. The re-derived number reaches no broker and places no order; the alignment exit remains the only way a position is closed on a thesis, and the owner's 2026-09-30 ruling that the desk exits on alignment and never on a target is untouched. It is NOT, however, true that the number can never contribute to a sale: the live target still feeds `distance_to_target_pct` into the position facts the reviewing model reads, so a revision can influence a later session's sale through that model's prose. What is true, and narrower, is that no DETERMINISTIC gate reads it — not the exit guard, not progress or pace, and since this change not the trailing-stop regime either. This also does not touch the accept-or-decline side, which is a separate change in flight.

## item 177

**Moved from WORK.md (2026-09-24) —** Questions: the 3 `IntradayScanConfig` ledger entries. Cadence: 3 code sites. Stop coverage is separately scheduled and free; the intra preamble (fills, stop-outs, loss check, drains) is not.

### What exists today [all measured 2026-09-26 against the production DB `/home/qamc/quant-agent/data/quant_agent.db`, read-only copy]

A paid intraday tick exists and is ON. `IntradayScanConfig.enabled` defaults False, but the deployed `config/settings.yaml` turns it on (the 2026-09-14 verification already corrected a stale comment claiming otherwise) and the cost circuit proves it: **211 `intra_check` sessions, 106 of them paid, $13.93 total — 62.8% of the desk's entire recorded model spend of $22.18** (`llm_budget_sessions`, 2026-08-25..2026-09-25). Morning, by comparison, is 31 sessions and $7.66. Per-day, intra_check outspends morning roughly eightfold.

TRIGGER. `move_threshold_pct` (flat 3% since the prior close, from one bulk snapshot call), then `cooldown_hours` (3h, same symbol), then `max_candidates_per_scan` (5 movers). Held investable names are added *on top of* the cap and consume no cooldown.

CADENCE. **Production is the systemd timer, not the APScheduler trigger** — verified on the box 2026-09-26 (`quant-agent-intra_check.timer` last fired 06:45, next 07:15). `OnCalendar=*:15,45` (moved off the shared `*:0/30` tick 2026-09-17) crossed with `run_if_et_window.sh`'s 09:30-16:00 ET window gives **13 ticks a day** — and the cost circuit records exactly 13 `intra_check` sessions on every trading day 2026-09-21..25, and 14 on every day before the timer moved. The `intra_check` mode is deliberately exempt from the once-per-day guard and the cross-mode session lock.

HELD BOOK. Measured on paid ticks joined to `specialist_evidence`: the symbol count is bimodal — 66 paid ticks analysed ≤5 symbols (movers only) and 39 analysed ≥11. **A tick carrying the held book costs 1.54x a movers-only tick** ($0.167 mean vs $0.108, n=39/66). The book stood at 10-12 names through the measured window, so held coverage roughly triples the tech payload of a capped scan.

### What the record says about each part

- **The flat trigger does not discriminate.** Across 253 `selected` rows (2026-09-02..25), the median move of a selection that produced a BUY or SHORT was **3.50%**, against **3.67%** for one that produced nothing. By bucket: 3-4% → 154 selections, 7 traded; 4-5% → 34/1; 5-6% → 25/0; 6-7% → 26/0; 7-8% → 6/2; 8-9% → 8/1. Raising the threshold would have removed the only band with volume and kept two bands that produced nothing. **Re-picking the flat number in either direction has no basis in the desk's own record.**
- **The ATR-relative form was unmeasurable.** The ledger's open question asks what move matters *relative to the name's own ATR*, and `intraday_evaluations.detail` stored `move_pct=` and nothing else — the denominator was never recorded. Fixed 2026-09-26: the row is now upserted after `compute_indicators` with `atr_pct=` and `move_atr=`, on bars the scan already paid to fetch, changing no behaviour and adding no row (`_record_intraday_trigger_atr_context`, `src/pipeline.py`). The question becomes answerable once enough rows accumulate. Coverage limit, stated: only names that already passed the flat 3% are stamped, so the record can show the trigger firing too *loosely* for a quiet name, never that it fired too tightly for a volatile one.
- **The cap binds, and binds silently.** Movers ledgered per run: 1→28 runs, 2→18, 3→16, 4→11, **5→20**. The cap of 5 is hit on 20 of 93 runs (21.5%), and the candidates dropped past it leave no record — exactly the `cost_while_unanswered` the ledger row predicted.
- **The yield.** 85 of 106 paid ticks (80%) produced no order at all. Across the whole record, paid intraday ticks are attributable to 21 new-position decisions (18 BUY + 3 SHORT), i.e. **$0.66 of model spend per position opened**. Over the retained report window, `intraday_no_trades` was 27 ticks for $4.39 and `intraday_executed` 10 ticks for $2.07.
- **Skips are already visible.** `paid_analysis_suspended`, `intraday_scan_lock_contended`, `intraday_scan_disabled`, `intraday_scan_crashed` and `intraday_scan_no_opportunity` all carry plain-English renderings in `src/notifier.py` and routing in `src/trader_feed.py`, and every return path is persisted to `intra_check_reports`. The doctrine that a budget-skipped tick must be visibly skipped is **already met**; no defect here.

### How the three parts interact — why they cannot be settled apart

The trigger sets how many movers *qualify*; the cap decides how many of those are paid for; the cadence multiplies whatever survives by 13; and the held book adds a fixed ~54% surcharge to every one of those 13 regardless of how many movers there were. So a day's paid intraday bill is roughly `13 × (movers-under-cap + held-book) × per-symbol cost`, and **the held book is the only term that is neither capped nor cooled down**. Loosening the trigger without touching the cap changes nothing (the cap already binds a fifth of the time). Tightening the trigger without touching the cadence saves little, because 13 ticks × held-book coverage is paid whether or not anything moved. Cutting the cadence is the only lever that scales all three at once — and it is also the only one that directly trades away loss-protection latency, because the same tick carries the free preamble.

### Still open, and exactly why

1. **The 3 `IntradayScanConfig` ledger rows.** `move_threshold_pct` cannot leave `arbitrary` yet: the measurement that would source it started on 2026-09-26 and has no rows. `max_candidates_per_scan` and `cooldown_hours` are **owner-appetite**, not research questions — see below.
2. **Every intra-preamble job on its own schedule.** **DONE 2026-10-01.** The free safety work no longer depends on the paid tick: it is one shared method with two callers — the paid `intra_check` tick (unchanged) and a new free `intra_safety` mode with its own systemd service and timer. Additive, not a move, so there is no window in which protection is not restored; both callers take the same broker-write flock and the same blocking-owner check (item 127), so concurrent firing serialises rather than races, and each tick now has two independent chances at the safety work instead of one. The new timer's interval is the already-ledgered `INTRA_CHECK_TICK_MINUTES` (the cadence this work runs on today, so latency is unchanged by construction) and its phase is the midpoint of the two phases already in use on the box, which is the only one at that interval colliding with no existing unit; both are pinned by `tests/test_intra_safety_schedule.py`. **Nothing here licenses choosing a different interval** — the input that would, the observed distribution of how long a position actually stays unprotected, cannot be computed at all yet — `pending_protection_restores` records when the intent was written but the drain deletes the row on success, so nothing records when protection came back [measured 2026-10-01 against the production DB, read-only]. A cleared-at record has to exist before an interval can be chosen on evidence. **Consequence for the rest of this item:** cutting the paid cadence is now a pure spend decision and no longer trades against loss-protection latency.
3. **A correction the ledger needs and this change could not make** (`config/number_ledger.yaml` is held by another change): the `source:` on `src.config.INTRA_CHECK_TICK_MINUTES` names `src/scheduler.py:56` as "the authority for how often the intraday control actually fires". That is **false in production** — `src/scheduler.py` is the `--mode live` path and the box runs the systemd timer. The row's status can stay `sourced`; the source text should name `scripts/systemd/quant-agent-intra_check.timer` crossed with `run_if_et_window.sh`'s window, with `src/scheduler.py` as the live-mode mirror, and cite `tests/test_systemd_units.py` for the pin that now holds all three sites together.

## item 193 — RETIRED 2026-10-01, the id-ceiling gap is explained: the WAL id sequence is SHARED with the protected-sell exit path, all 15 production cancels carry their own wal_row_id and all pair, and every id handed out is now recorded durably at the single insert choke point

**2026-10-01 — the row-id gap is explained, and the pair count is NOT a floor.**
Measured against the production database (`/home/qamc/quant-agent/data/quant_agent.db`,
read-only). Every `scale_in` / `protective_sell_cancelled` event carries its OWN
`wal_row_id` in its payload, so the cancels do not have to be counted against the
sequence at all: there are 15 of them (2026-09-17..2026-09-30), holding ids 5, 6, 7,
9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 20 and 23, and all 15 pair with a later
same-run same-symbol `protection` / `placed` event. `sqlite_sequence` stands at 23 and
`pending_protection_restores` is empty. The eight ids the scale-in path never held are
1, 2, 3, 4, 8, 17, 21 and 22, and the reason the ceiling outruns the cancel count is
structural rather than missing: `pending_protection_restores` is a SHARED autoincrement
and the protected-sell exit path (`TradingPipeline`'s `_WAL_SELL_SENTINEL` write-ahead,
plus the orphan-persist branch) draws ids from the same sequence, as do the scale-in
rollback branches that insert the row and discharge it when the cancel fails or will
not confirm. Six of the eight line up with closing trades: ids 1-4 all predate the
first scale-in cancel entirely and seven sell-side trades precede it, and ids 8 and 17
each fall in a window containing exactly one sell-side trade. Ids 21 and 22 could not
be attributed, and the two honest attempts are written down rather than papered over:
(1) no path except a SUCCESSFUL scale-in cancel files its row id anywhere durable — a
sweep of all 5,836 production `pipeline_event` rows found `wal_row_id` in exactly the
15 scale-in cancel payloads and nowhere else; (2) the production log has rotated past
them — it now reaches back only far enough to show row 23. So the deriving stopped and
the RECORDING was built instead: `protection_restore_wal_audit` takes one never-deleted
row per id, written inside `Database.insert_pending_protection_restore` itself, which is
the single choke point every writer passes through. It carries the writing sentinel, the
symbol, the side and the held quantity, it survives the discharge that deletes the WAL
row, and `get_protection_restore_wal_audit` reads it back, so the next time the ceiling
outruns the cancels the missing ids are named instead of argued about. Bookkeeping only:
nothing rules on it, and a failure to write it never blocks the protective WAL row it
describes (proved by `tests/test_protection_restore_wal_audit.py`). AMENDING IS STILL
NOT THE REMEDY HERE and nothing above reopens it — the 2026-09-30 finding stands: the
cancel exists because the resting protective SELL collides with the BUY add, a price
amend leaves that SELL open, and the quantity amend that would cover an enlarged
position is refused by this broker on a fractional order (42210000).


Measurement only. No production code was written or changed for it; both events already exist.

**The two event names, as they actually appear in the code.** The cancel is filed in `src/pipeline_stages.py` as stage `scale_in`, outcome `protective_sell_cancelled`, reason `cancel_confirmed_via_trade_updates` — the reason string is stale wording kept deliberately, since the `trade_updates` socket has been off since 2026-09-17 and the confirm is now a bounded REST wait. The rearm has NO scale-in-specific event: it is the generic stage `protection`, outcome `placed` or `not_placed`, reason `protective_stop_result`, fired for every entry protection whether or not a scale-in preceded it. Pairing therefore has to be done on `run_id` plus `symbol` plus event ordering, which is why a cancel whose run placed no protection at all would show up as unpaired.

**Method.** Both events land in `specialist_evidence` with `kind='pipeline_event'` (via `_record_pipeline_event` -> `_persist_evidence` -> `Database.insert_specialist_evidence`); there is no `pipeline_events` table. The live file was copied out with its `-wal` and `-shm` sidecars and the counts agreed with and without them. Exposure per event is `abs(held_qty_before)` from the cancel event's own payload multiplied by the add's fill price from the `trades` row for the same run and symbol. Volatility is the standard deviation of the last 20 daily close-to-close log returns strictly before the event date, from daily bars for that name, scaled to the window by `sigma_daily * sqrt(seconds / 23400)`. No volatility number was assumed or carried over from anywhere.

**2026-09-30 — the window is now self-measuring, and amending cannot remove it.**
Each scale-in that cancels a protective stop emits one `scale_in` /
`unprotected_window_closed` event (or `unprotected_window_still_open` when the
rearm did not land) at the moment the rearm attempt returns. Both ends are
`time.monotonic()` readings taken inside the run — the broker's cancel
acknowledgement and the broker's rearm acknowledgement — so the figure never
reflects a row's write time, which was the first DONE WHEN. The event carries
`window_seconds`, the WHOLE `held_qty_before` the cancel exposed (abs()ed, so a
short reads positive), `exposed_notional` (None, never a fabricated 0, when the
reference price is unknowable) and the same `wal_row_id` as the cancel event, so
an unpaired cancel is a visibly missing partner rather than something inferred
from row-id arithmetic. Both new outcome words joined `UNDECIDED_OUTCOMES`:
they are mid-add bookkeeping and rule on nothing.

**2026-09-30 — the skip stays, the silence does not.** The coverage sweep
skips any symbol holding a live scale-in write-ahead row, and that skip is
correct: placing a stop there re-creates the opposite-side block the cancel
just cleared. It also meant the one moment the desk is naked was the one
moment the report said nothing, because a skipped symbol simply vanished from
the sweep. It now appears by name — held quantity, short or long, the row's
`created_at`, and roughly how long protection has been down — in the sweep's
run record, in its single log line, and in `CoverageStatus.unguarded`. It is
kept OUT of `gaps`, because a gap is something the sweep tries to repair and
this one must never be repaired. The duration is the write-ahead row's WRITE
time, not the broker's cancel acknowledgement, so it is labelled approximate
everywhere; the exact figure remains the `unprotected_window_closed` event the
session files at rearm. The overdue test is not a chosen number: the bound is
the LONGEST window the desk has actually measured, read back out of its own
closed-window events, and with no measured history there is no bound and
nothing is called overdue. Over the bound, the owner is paged once per symbol
per trading day on its own footing — never folded into the coverage-gap alert,
which would tell him the desk failed to place a stop it in fact cancelled
deliberately. No broker order is placed by any of this.

**Amending does not close this window.** The desk measured on the rehearsal
account that Alpaca amends a resting stop's price in place, and `broker.py`
grew an amend path. It does not apply here. The cancel exists because a resting
protective SELL holds the shares and collides with the BUY add; a price amend
leaves that SELL open, so the collision — and the reason for the cancel — is
unchanged. The quantity amend that WOULD cover an enlarged position is refused
by this broker on a fractional order (42210000), and scale-in adds are routinely
fractional. The window is a property of the broker's order model, not of the
desk's sequencing, so the remaining work is detection and bounding, not removal.

**2026-09-30 — the lock-held skip is now BOUNDED, which is the only part of
this that was ever removable.** The coverage sweep's skip had two arms. The
crash arm was already safe: with no session lock it skips a symbol only while a
working entry order still rests, so a dead session's naked position is repaired.
The session-lock arm was not: while the wrapper's lock directory existed, every
symbol holding a scale-in write-ahead row was skipped for as long as that row
survived, so a session that cancelled the protective stop and then HUNG without
releasing the lock left the whole held position naked with the one watchdog that
could re-protect it deliberately looking away, with no end. The lock arm now
defers to the desk's own measurement: within the longest window it has ever
closed and recorded — and always when it has measured nothing at all, or cannot
read a row's write time — behaviour is byte-for-byte what it was, because that
is a normal live window. Past that bound the lock stops being reason enough, and
the symbol falls back to the same collision test the crash arm uses: a working
entry order still resting keeps the skip, nothing resting hands the position to
the sweep to re-protect. No number was chosen and none was changed. The residual
risk is taken deliberately and on the conservative side: a live session slower
than every window ever measured may have its add blocked by the stop the sweep
places, and the add's own failure path restores from the write-ahead row — an
add refused with the position protected beats a position left naked.

**The amend question, answered for the last time.** A quantity amend on the
resting stop cannot replace the cancel, for two independent reasons: the resting
protective SELL collides with the working BUY whatever its quantity says, and
this broker refuses a quantity amend on a fractional order (42210000), which
scale-in adds routinely are. A test now pins that the path never reaches for
`replace_order_by_id` at all, so the refusal cannot be hit and the resting stop
is never left in an unknown state.

**Still open.** The second DONE WHEN (explaining the historical write-ahead-log
row-id gap, so the 14-pair count is known complete rather than a floor) is
unaddressed: the new event makes FUTURE pairs complete by construction but says
nothing about the rows already filed. The third (answering "is any position
naked right now, and for how long" without a one-off query) is also unmet — the
new event is still something you have to go and read, and the coverage watchdog
deliberately skips symbols mid-scale-in, which is exactly this window.

**What was deliberately not done.** No fix, no alert, no change to `scale_in.py` — including the docstring's "~15 s", which the measurement contradicts but which is a code change and not this pass's mandate.

## item 197 — RETIRED 2026-09-30, the re-peg now derives a floor for a short and a ceiling for a long, with the room test, the quote side and the walk direction inverted to match, and a sell_short spec is exercised in tests

The re-peg path (`_repeg_entry_order`, `src/pipeline_stages.py`) was written for
the BUY side and never generalised. It builds a single bound —
`reference * (1 + slippage_bps / 10_000)` — names it `ceiling`, stores it on the
spec, and returns `no_room` when the submitted `limit_price` is already at or
above it. No `side`, `action` or `is_short` is read anywhere in the function.

For a BUY the logic is correct and, since the submitted limit IS the ceiling, it
almost always returns `no_room` immediately. For a `sell_short` the same
arithmetic produces a number ABOVE the reference when the fillable bound is
BELOW it, so two things break together: the room test fires backwards (a short
limit far from the floor reads as "already there"), and any walk it did perform
would move the limit UP, away from where a short can fill.

Not live. `repeg_enabled` is `false` in `config/settings.yaml` and defaults to
`False` in `src/config.py`. Board item 183 found this while removing the
far-through-quote entry skip and deliberately left it alone: it predates that
work and fixing it is a behaviour change on a money path with no live exercise
and no recorded outcomes to measure against.

The hazard to watch is ordering: the flag being turned on before the side fix
lands would put the defect straight into production on the short book.

## item 199 — read the unbacked-stop floor off the chart

Moved out of `docs/WORK.md` on 2026-09-30 to keep that file under the
100,000-byte cap `tests/test_status_board.py` enforces. Nothing is
changed; this is the item body verbatim.
 `risk.min_stop_atr_multiple` (2.5) is not sourced — there is no citation for a fixed entry stop at that multiple and both ends of the band quoted at its definition site are unsupported (item 90, 2026-09-30) — so `docs/OUTCOME.md`'s first-ranked remedy applies: reformulate the rule so it needs no constant. Doctrine's own worked example is structural ("does the last higher low still hold?" needs no number because the chart supplies the level), and item 90's pass established that this desk can already do it. `_level_backing_stop` discards any level with fewer than `min_level_touches_for_stop_honor` (5) touches, after which the stop falls to the flat ATR multiple; but that 5 was measured for whether a level is trustworthy enough to justify a stop TIGHTER than the floor, where a level that fails costs a whipsaw. Used as a WIDENING anchor the bet inverts: the stop sits beyond the level, so a level that fails leaves the stop merely wider than needed, which under risk-based sizing costs position size and not loss. That asymmetry has never been examined and it is where the constant's blast radius shrinks. The measured backing is in the repo and is not fitting, because it is measured off bars rather than off this desk's trades: pooled bounce probability rises from 0.516 at first touch to 0.644 at 5+ touches against a flat ~0.48-0.51 shuffled control (7,218 touch episodes, 101 symbols, 5 years; `src/data/levels.py`, `docs/RESEARCH_FINDINGS.md` §7), so a 2-touch level (`MIN_TOUCHES` = 2) still carries real information. The machinery also exists: `_derive_structural_stop_no_atr` (owner-ratified, item 80) already reads a protective stop from structure using the ratified `structural_stop_buffer_pct`; only its trigger condition would change. **Two things this must settle rather than assume, both of which could sink it:** what the floor does when the nearest computed level below entry is very far away (a distant anchor shrinks the position toward `position_sized_to_zero` and may be worse than the flat multiple), and what happens when no computed level exists below entry at all — that residue is the only population an ATR multiple would still govern, and item 90 stays open on it.

## item 90 — the 2026-09-30 `min_stop_atr_multiple` pass

Moved out of `docs/WORK.md` on 2026-09-30 for the 100,000-byte cap
`tests/test_status_board.py` enforces. Verbatim; nothing changed.
**2026-09-30 — `risk.min_stop_atr_multiple` (2.5): the value is UNCHANGED, the claim that it was SOURCED is withdrawn, and the reformulation is filed as item 199 rather than refused.** This constant belongs to no tranche (182 is the ladder, 183 the order gates, 185 the trailing numbers, 186 the portfolio ceilings), so it was taken here. A first pass refused it; an adversary pass found that refusal rested on a false history and wrong arithmetic, and what follows is the corrected result. Full reasoning is in `config/number_ledger.yaml` under its id rather than duplicated here. **(a) A false history is deleted from five files, not softened.** The first pass asserted a "3.0 -> 1.5 move on 2026-09-04" and built an argument on it. There was no such move: `config/settings.yaml` went 3.0 (2026-08-27) straight to 2.5 (2026-09-10) and never deployed 1.5, because the commit that carried it squashes PR #269's two legs (3.0 -> 1.5, then 1.5 -> 2.5) into one merge. The wrong date was inherited from a settings comment and then copied into four more places by a change whose purpose was removing rot; it is now corrected at the source. **(b) The ledger's own open question was doctrine-barred and is replaced.** It asked what this desk's maximum-adverse-excursion record says about the point inside the band — an MAE study over the desk's own trades is FITTING, which `docs/OUTCOME.md` bars outright, and it is how the 1.5 was produced in the first place. **(c) There is no cited band, so BOTH ends are unsupported.** The entry carries no source field and `config/settings.yaml` offers only "general swing-trading guidance" with no URL, which doctrine explicitly rejects. Searched and recorded: the pages asserting 2.5-3.0x for a fixed multi-day entry stop are vendor content rather than literature, one secondary claim points the other way at 1.5-2.0x, the corroborating Van Tharp and Chandelier figures are trailing mechanisms the settings comment already concedes, and the top search hit for the desk's own phrasing is now the desk's own PR. The quoted band (2.5-3.0) does not even match the one quoted three lines below it (2-3). 2.5 stays as the INTERIM value and is deliberately not re-picked, because with no cited band moving it is one more unsourced choice. **(d) The reformulation is NOT refused — it is specified and filed as item 199.** The first pass refused a sqrt-horizon floor claiming it pins reward:risk at exactly 1.0; that was wrong twice (the setup and regime scalers still multiply in, giving about 1.17 to 0.83, and the target and stop rules fire on opposite sides of price so they do not share a population) and is retracted. More importantly it tested the wrong reformulation: doctrine's worked example is structural, and this desk already computes levels with touch counts and already has owner-ratified machinery that reads a stop from structure. The asymmetry nobody had examined is that the 5-touch bar was measured for justifying a TIGHTER stop, where a level that fails costs a whipsaw; as a WIDENING anchor a level that fails only leaves the stop wider than needed, which under risk-based sizing costs position size and not loss. **(e) One stale constant fixed and the class closed mechanically.** `src/pipeline.py` fell back to 1.5 whenever the configured multiple was absent or not a real number — a half-landed second leg of PR #269, which is exactly the failure `scripts/definition_of_done.py` exists for. Measured: not reachable in production, and the three test modules that build a pipeline give 108 passed with the fallback at either value, so nothing depended on it. `tests/test_risk_setting_fallbacks.py` now pins all fifteen fallbacks to the DEPLOYED value in `config/settings.yaml`. **(f) The screen contradiction was FIXED ON MAIN by item 185, which landed first and went further; this branch drops its own narrower version.** This pass proposed dividing `STOP_SANITY_FLOOR_FRACTION` by the widest reachable stop multiple instead of the base, taking the ceiling from 20% to 16.67%. Item 185 instead deleted the borrowed 0.5 literal outright, so the ceiling is now `1 / widest_reachable_stop_atr_multiple(...)` = 1/3.00 = 33.3%. Main's form is kept. The FINDING survives and item 185 confirms it: dividing by the bare base was false across a band of names, and the divergence was exactly the risk-off scaler 1.20. Also withdrawn as wrong on the facts: the board's note that this "needs the owner's call because it tightens a live screen" — `universe_screen.enabled` is false, so the screen does not ship on. **(g) THE BLAST RADIUS GREW WHILE THIS PASS WAS OPEN, and that strengthens rather than weakens the interim finding.** As of 2026-09-30 this constant no longer governs only the entry stop. Through `widest_reachable_stop_atr_multiple` (2.5 x 1.00 x 1.20 = 3.00) it now also sets (1) the midday stop clamp — an over-wide proposed stop is no longer refused but CLAMPED to that multiple of the name's own live ATR14 and placed (item 80), and (2) the universe screen's volatility ceiling at 1/3.00, which decides which names are tradeable at all before any seat sees them. Moving 2.5 now moves three money decisions, not one. That is a reason to state the interim status loudly, not a derivation: composing an unsourced number into more rules removes independent literals without adding evidence for any of them. Item 185 records the same point from its own side and also stays open.

Full heading text, moved for the same reason:

**2026-09-30 — `risk.min_stop_atr_multiple` (2.5): the value is UNCHANGED, the claim that it was SOURCED is withdrawn, and the reformulation is filed as item 199 rather than refused.** This constant belongs to no tranche (182 is the ladder, 183 the order gates, 185 the trailing numbers, 186 the portfolio ceilings), so it was taken here. A first pass refused it; an adversary pass found that refusal rested on a false history and wrong arithmetic, and what follows is the corrected result. Full reasoning is in `config/number_ledger.yaml` under its id rather than duplicated here. **(a) A false history is deleted from five files, not softened.** The first pass asserted a "3.0 -> 1.5 move on 2026-09-04" and built an argument on it. There was no such move: `config/settings.yaml` went 3.0 (2026-08-27) straight to 2.5 (2026-09-10) and never deployed 1.5, because the commit that carried it squashes PR #269's two legs (3.0 -> 1.5, then 1.5 -> 2.5) into one merge. The wrong date was inherited from a settings comment and then copied into four more places by a change whose purpose was removing rot; it is now corrected at the source. **(b) The ledger's own open question was doctrine-barred and is replaced.** It asked what this desk's maximum-adverse-excursion record says about the point inside the band — an MAE study over the desk's own trades is FITTING, which `docs/OUTCOME.md` bars outright, and it is how the 1.5 was produced in the first place. **(c) There is no cited band, so BOTH ends are unsupported.** The entry carries no source field and `config/settings.yaml` offers only "general swing-trading guidance" with no URL, which doctrine explicitly rejects. Searched and recorded: the pages asserting 2.5-3.0x for a fixed multi-day entry stop are vendor content rather than literature, one secondary claim points the other way at 1.5-2.0x, the corroborating Van Tharp and Chandelier figures are trailing mechanisms the settings comment already concedes, and the top search hit for the desk's own phrasing is now the desk's own PR. The quoted band (2.5-3.0) does not even match the one quoted three lines below it (2-3). 2.5 stays as the INTERIM value and is deliberately not re-picked, because with no cited band moving it is one more unsourced choice. **(d) The reformulation is NOT refused — it is specified and filed as item 199.** The first pass refused a sqrt-horizon floor claiming it pins reward:risk at exactly 1.0; that was wrong twice (the setup and regime scalers still multiply in, giving about 1.17 to 0.83, and the target and stop rules fire on opposite sides of price so they do not share a population) and is retracted. More importantly it tested the wrong reformulation: doctrine's worked example is structural, and this desk already computes levels with touch counts and already has owner-ratified machinery that reads a stop from structure. The asymmetry nobody had examined is that the 5-touch bar was measured for justifying a TIGHTER stop, where a level that fails costs a whipsaw; as a WIDENING anchor a level that fails only leaves the stop wider than needed, which under risk-based sizing costs position size and not loss. **(e) One stale constant fixed and the class closed mechanically.** `src/pipeline.py` fell back to 1.5 whenever the configured multiple was absent or not a real number — a half-landed second leg of PR #269, which is exactly the failure `scripts/definition_of_done.py` exists for. Measured: not reachable in production, and the three test modules that build a pipeline give 108 passed with the fallback at either value, so nothing depended on it. `tests/test_risk_setting_fallbacks.py` now pins all fifteen fallbacks to the DEPLOYED value in `config/settings.yaml`. **(f) The screen contradiction was FIXED ON MAIN by item 185, which landed first and went further; this branch drops its own narrower version.** This pass proposed dividing `STOP_SANITY_FLOOR_FRACTION` by the widest reachable stop multiple instead of the base, taking the ceiling from 20% to 16.67%. Item 185 instead deleted the borrowed 0.5 literal outright, so the ceiling is now `1 / widest_reachable_stop_atr_multiple(...)` = 1/3.00 = 33.3%. Main's form is kept. The FINDING survives and item 185 confirms it: dividing by the bare base was false across a band of names, and the divergence was exactly the risk-off scaler 1.20. Also withdrawn as wrong on the facts: the board's note that this "needs the owner's call because it tightens a live screen" — `universe_screen.enabled` is false, so the screen does not ship on. **(g) THE BLAST RADIUS GREW WHILE THIS PASS WAS OPEN, and that strengthens rather than weakens the interim finding.** Detail: `docs/BOARD_NOTES.md` ("item 90 — the 2026-09-30 `min_stop_atr_multiple` pass").


**2026-09-30, third pass — the recording is COMPLETED and is now the completion criterion of items 90 and 199, replacing any further re-derivation.** The pinned-at-entry half (`entry_atr`, `initial_stop_loss`, the entry `price`, and `stop_basis` carrying the constructor's own `stop_rule`) and the resolved half (`realized_pnl`, `exit_reason_category` = `broker_stop_fill` when the broker's stop filled) were already on the `trades` row. The gap closed here is the FAVOURABLE excursion: `max_favourable_excursion`, the exact mirror of `max_adverse_excursion`, widened by the same `Database._accumulate_excursions` call inside the same `sync_positions` transaction as the snapshot it is derived from. Without it a stop-out recorded beside a wide adverse excursion cannot be told apart from one that first ran a long way in the desk's favour and gave it all back, which is the question the floor actually turns on. Nothing reads any of these columns back into a decision — no threshold, no gate, no surface — so this cannot change what the desk trades; the accumulation is swallowed on error so a recording fault can never fail a position sync. The stop's distance in ATR multiples is deliberately NOT a column: it is recomputed from entry price, entry stop and entry ATR, per the standing rule against storing what code can recompute. Both excursions are snapshot-frequency FLOORS on the true figures and legacy rows are NULL; a reader who drops either caveat is reading them wrong. Proved by `tests/test_stop_evidence_excursions.py`, including a position that opens, runs against the desk, recovers and then closes still carrying the worst excursion it reached.

**2026-09-30, second pass — the EVIDENCE the floor would need is now being recorded, and the fallback divergence is closed at its source rather than pinned by a test.** Two changes, no change to any traded number. (1) `src/pipeline.py`'s hand-copied fallback literals are now overridden by the default `RiskConfig` itself declares, so the 1.5-vs-2.5 divergence note (e) describes cannot recur for this or any other risk ceiling; an audit of every `_risk_setting` literal in the file found `min_stop_atr_multiple` to be the only mismatch, and `max_position_pct` to be the only name with no declared default (required field), so its literal stays as the genuine last resort. (2) Every closed trade now carries the four facts the desk has never recorded and therefore could never check its floor against: `trades.entry_atr` (ATR14 pinned at entry), `trades.stop_basis` (the constructor's own STOP_RULE_* string, which already separates a stop honoured at a computed level from one set by the ATR band), `trades.max_adverse_excursion` (worst against-entry price accumulated monotonically from each session's position snapshot), alongside the `realized_pnl` and `exit_reason_category` already on the row. **This does NOT reopen the doctrine-barred MAE study of note (b).** The permitted use is falsification only: showing whether the ratified floor was ever VIOLATED in practice — whether trades that went on to resolve well were stopped out by a floor sitting inside their ordinary excursion. Sweeping this record for the multiplier that would have maximised past outcomes is fitting and stays barred; the floor is still read from published doctrine and from the instrument. One caveat any reader must carry: the excursion is sampled at snapshot frequency, so it is a floor on the true MAE — a reading that says the floor WAS violated is trustworthy, one that says it was not means only "not observed". Nothing reads any of it back into a trading decision. **The next pass on this item should ask what the record now shows, not re-derive the multiple.**
## Item 202 — the rehearsal harness reaches the network

Found 2026-09-30 while closing a hole in the test suite's outbound-HTTP guard.

`tests/conftest.py` blocked `requests.get` only. A `requests.Session` bypassed
it, and yfinance does not use `requests` at all — it ships its own transport on
curl_cffi [measured: `yfinance.data` references `curl_cffi` and `session.get`,
and `requests.Session` zero times]. So the guard never applied to the one
library that actually reached the internet.

Closing both holes exposed five tests that silently depended on a live Yahoo
Finance response. Four were not about market data and now state their own
sectors. The fifth is this item: a test whose premise is replaying a RECORDED
session downloads SPY and per-symbol price history on every run, reports
`TECH DATA BLIND SPOT`, and never reaches the Portfolio Manager.

It **fails on `origin/main` today** with the network reachable, taking 196
seconds [measured 2026-09-30], so it is pre-existing rot rather than a
regression from the guard.

Do NOT fix it by loosening the guard, skipping the test, or marking it flaky.
That is the same error as raising a safety sweep's frequency instead of fixing
what the sweep is covering for.


### Item 202 update — the isolation was never real (2026-09-30)

`ops/rehearsal/broker.py::blocked_market_data` replaces the market-data
provider with one that fetches nothing, and `ops/rehearsal/isolation.py`
describes a socket wall covering "Anthropic, OpenAI, OpenRouter, Alpaca,
yfinance, FRED and RSS". Neither held: price data still reached the rehearsal
through **curl_cffi**, which is yfinance's own transport and which the test
suite's outbound-HTTP guard did not cover.

So the rehearsal has been validating against LIVE market data while claiming
to be offline, deterministic and free. With the hole closed, the session
degrades honestly to `status='no_data'` and never reaches the Portfolio
Manager, which is why `test_the_settled_cost_ceiling_still_suspends_paid_analysis`
cannot build its 'before' case.

That test is marked `xfail(strict=False)` with the reason above — NOT as a
flake. It flips to XPASS the moment this item serves recorded market data,
which is the signal that item 202 is done.

It also fails on `origin/main` today, taking ~196 seconds of live fetching
[measured 2026-09-30], so the defect predates the guard rather than being
caused by it.
## items 182 / 183 / 185 / 186 — consolidation check against item 90 (2026-09-30)
**Verdict: all four KEPT, none retired.** Each opens with "item 90's half two, surfaced for visibility", but each carries its own DONE WHEN criteria that item 90 does not own: 182 the ladder alert and cash-deficit cushion, 183 the order gates and the dead cash-sweep config removal, 185 the ATR-eligibility question and its two inherited rows, 186 three open owner-appetite answers. Retiring any would lose those criteria. The defect found was in item 90 itself: it claimed the four carry one word-for-word shared criterion, which was false (checked against each block). Item 90's line now names the four tranches and what each covers. No constant, threshold or value was chosen or changed.
## Item 192 (RETIRED 2026-09-30) — local interpreter pinned to CI's
Retired because all three DONE WHEN criteria are satisfied on main, not because
the item was abandoned.
- `.python-version` on main reads `3.11`, and both CI jobs read it via
  `python-version-file` rather than each naming a version.
- A local pytest run aborts and names both versions when the running
  interpreter is not the pinned one (shipped in #792).
- The last open criterion — actually rebuilding the dev `.venv`, which measured
  3.12.3 — was completed 2026-09-30: `pip install uv`, `uv python install 3.11`,
  `uv venv --python 3.11`. `/home/ubuntu/projects/quant-agent/.venv` now measures
  **Python 3.11.16** [measured: `.venv/bin/python -V`]. The previous interpreter
  is preserved at `.venv312` so any session mid-run on it is not broken.
Why it mattered: the split was the direct cause of two confident, wrong agent
diagnoses in one session. A prompt-drift check hashed `ast.dump()` of a parsed
function, 3.12 changed that output, and identical source hashed differently
locally and in CI.
## item 193 — RETIRED 2026-10-01, the write-ahead row-id gap is explained: the id sequence is shared with the ordinary protective-sell restore path, every one of the 15 production cancels carries its own row id, and all 15 pair with a later rearm, so the pair count is complete and not a floor

Measurement only. No production code was written or changed for it; both events already exist.

**The two event names, as they actually appear in the code.** The cancel is filed in `src/pipeline_stages.py` as stage `scale_in`, outcome `protective_sell_cancelled`, reason `cancel_confirmed_via_trade_updates` — the reason string is stale wording kept deliberately, since the `trade_updates` socket has been off since 2026-09-17 and the confirm is now a bounded REST wait. The rearm has NO scale-in-specific event: it is the generic stage `protection`, outcome `placed` or `not_placed`, reason `protective_stop_result`, fired for every entry protection whether or not a scale-in preceded it. Pairing therefore has to be done on `run_id` plus `symbol` plus event ordering, which is why a cancel whose run placed no protection at all would show up as unpaired.

**Method.** Both events land in `specialist_evidence` with `kind='pipeline_event'` (via `_record_pipeline_event` -> `_persist_evidence` -> `Database.insert_specialist_evidence`); there is no `pipeline_events` table. The live file was copied out with its `-wal` and `-shm` sidecars and the counts agreed with and without them. Exposure per event is `abs(held_qty_before)` from the cancel event's own payload multiplied by the add's fill price from the `trades` row for the same run and symbol. Volatility is the standard deviation of the last 20 daily close-to-close log returns strictly before the event date, from daily bars for that name, scaled to the window by `sigma_daily * sqrt(seconds / 23400)`. No volatility number was assumed or carried over from anywhere.

**2026-09-30 — the window is now self-measuring, and amending cannot remove it.**
Each scale-in that cancels a protective stop emits one `scale_in` /
`unprotected_window_closed` event (or `unprotected_window_still_open` when the
rearm did not land) at the moment the rearm attempt returns. Both ends are
`time.monotonic()` readings taken inside the run — the broker's cancel
acknowledgement and the broker's rearm acknowledgement — so the figure never
reflects a row's write time, which was the first DONE WHEN. The event carries
`window_seconds`, the WHOLE `held_qty_before` the cancel exposed (abs()ed, so a
short reads positive), `exposed_notional` (None, never a fabricated 0, when the
reference price is unknowable) and the same `wal_row_id` as the cancel event, so
an unpaired cancel is a visibly missing partner rather than something inferred
from row-id arithmetic. Both new outcome words joined `UNDECIDED_OUTCOMES`:
they are mid-add bookkeeping and rule on nothing.

**2026-09-30 — the skip stays, the silence does not.** The coverage sweep
skips any symbol holding a live scale-in write-ahead row, and that skip is
correct: placing a stop there re-creates the opposite-side block the cancel
just cleared. It also meant the one moment the desk is naked was the one
moment the report said nothing, because a skipped symbol simply vanished from
the sweep. It now appears by name — held quantity, short or long, the row's
`created_at`, and roughly how long protection has been down — in the sweep's
run record, in its single log line, and in `CoverageStatus.unguarded`. It is
kept OUT of `gaps`, because a gap is something the sweep tries to repair and
this one must never be repaired. The duration is the write-ahead row's WRITE
time, not the broker's cancel acknowledgement, so it is labelled approximate
everywhere; the exact figure remains the `unprotected_window_closed` event the
session files at rearm. The overdue test is not a chosen number: the bound is
the LONGEST window the desk has actually measured, read back out of its own
closed-window events, and with no measured history there is no bound and
nothing is called overdue. Over the bound, the owner is paged once per symbol
per trading day on its own footing — never folded into the coverage-gap alert,
which would tell him the desk failed to place a stop it in fact cancelled
deliberately. No broker order is placed by any of this.

**Amending does not close this window.** The desk measured on the rehearsal
account that Alpaca amends a resting stop's price in place, and `broker.py`
grew an amend path. It does not apply here. The cancel exists because a resting
protective SELL holds the shares and collides with the BUY add; a price amend
leaves that SELL open, so the collision — and the reason for the cancel — is
unchanged. The quantity amend that WOULD cover an enlarged position is refused
by this broker on a fractional order (42210000), and scale-in adds are routinely
fractional. The window is a property of the broker's order model, not of the
desk's sequencing, so the remaining work is detection and bounding, not removal.

**2026-09-30 — the lock-held skip is now BOUNDED, which is the only part of
this that was ever removable.** The coverage sweep's skip had two arms. The
crash arm was already safe: with no session lock it skips a symbol only while a
working entry order still rests, so a dead session's naked position is repaired.
The session-lock arm was not: while the wrapper's lock directory existed, every
symbol holding a scale-in write-ahead row was skipped for as long as that row
survived, so a session that cancelled the protective stop and then HUNG without
releasing the lock left the whole held position naked with the one watchdog that
could re-protect it deliberately looking away, with no end. The lock arm now
defers to the desk's own measurement: within the longest window it has ever
closed and recorded — and always when it has measured nothing at all, or cannot
read a row's write time — behaviour is byte-for-byte what it was, because that
is a normal live window. Past that bound the lock stops being reason enough, and
the symbol falls back to the same collision test the crash arm uses: a working
entry order still resting keeps the skip, nothing resting hands the position to
the sweep to re-protect. No number was chosen and none was changed. The residual
risk is taken deliberately and on the conservative side: a live session slower
than every window ever measured may have its add blocked by the stop the sweep
places, and the add's own failure path restores from the write-ahead row — an
add refused with the position protected beats a position left naked.

**The amend question, answered for the last time.** A quantity amend on the
resting stop cannot replace the cancel, for two independent reasons: the resting
protective SELL collides with the working BUY whatever its quantity says, and
this broker refuses a quantity amend on a fractional order (42210000), which
scale-in adds routinely are. A test now pins that the path never reaches for
`replace_order_by_id` at all, so the refusal cannot be hit and the resting stop
is never left in an unknown state.

**2026-10-01 — the row-id gap is EXPLAINED, and the pair count is COMPLETE.**
Measured read-only against the production database
(`/home/qamc/quant-agent/data/quant_agent.db`, snapshot taken 2026-10-01; the
repo-local DB is empty): 15 `scale_in|protective_sell_cancelled` events exist
(2026-09-17..2026-09-30, one more than the 14 of the first pass), ALL 15 pair
with a later same-run same-symbol `protection|placed`, and ZERO
`scale_in|skipped` events of any reason have ever been filed, so no preparation
has yet aborted between the write-ahead insert and the confirmed cancel. Each
cancel event now carries the row id it allocated: 5, 6, 7, 9, 10, 11, 12, 13,
14, 15, 16, 18, 19, 20, 23, against an AUTOINCREMENT sequence standing at 23.
The eight ids scale-in does not hold — 1, 2, 3, 4, 8, 17, 21, 22 — belong to the
OTHER writer of the same table, the ordinary protective-sell restore path in
`src/pipeline.py`. The sequence is shared, so the highest row id was never a
count of scale-ins and the apparent "20 ids vs 14 events" shortfall was an
artefact of reading one writer's census off two writers' counter. The census
that is correct filters on the sentinel `sell_order_id`, and
`tests/test_scale_in_wal_row_id_census.py` pins both properties so the argument
stays mechanical. No production code was changed by this pass and no broker
order was placed.

**The amend merged to main does not reach this path — verified in code, not
assumed.** `_amend_resting_stop_price` in `src/execution/broker.py` has exactly
one caller, the trailing-stop re-price, and it amends a stop's PRICE. A price
amend leaves the protective SELL resting, which is the thing that collides with
the BUY add, so it cannot replace the cancel; the quantity amend that would
cover an enlarged position is refused by this broker on a fractional order
(42210000). `tests/test_scale_in.py` already pins that the scale-in path never
reaches for `replace_order_by_id`. The window is therefore still real, and this
item closes on measurement and detection, not on removal.

**Nothing remains open** (the paragraph below is kept as the record of what was
open on 2026-09-30; the second DONE WHEN was settled on 2026-10-01 above, and
the third was met on 2026-09-30 by the coverage sweep's named skips).

**Was open on 2026-09-30.** The second DONE WHEN (explaining the historical write-ahead-log
row-id gap, so the 14-pair count is known complete rather than a floor) is
unaddressed: the new event makes FUTURE pairs complete by construction but says
nothing about the rows already filed. The third (answering "is any position
naked right now, and for how long" without a one-off query) is also unmet — the
new event is still something you have to go and read, and the coverage watchdog
deliberately skips symbols mid-scale-in, which is exactly this window.

**What was deliberately not done.** No fix, no alert, no change to `scale_in.py` — including the docstring's "~15 s", which the measurement contradicts but which is a code change and not this pass's mandate.

## item 197 — RETIRED 2026-09-30, the re-peg now derives a floor for a short and a ceiling for a long, with the room test, the quote side and the walk direction inverted to match, and a sell_short spec is exercised in tests

The re-peg path (`_repeg_entry_order`, `src/pipeline_stages.py`) was written for
the BUY side and never generalised. It builds a single bound —
`reference * (1 + slippage_bps / 10_000)` — names it `ceiling`, stores it on the
spec, and returns `no_room` when the submitted `limit_price` is already at or
above it. No `side`, `action` or `is_short` is read anywhere in the function.

For a BUY the logic is correct and, since the submitted limit IS the ceiling, it
almost always returns `no_room` immediately. For a `sell_short` the same
arithmetic produces a number ABOVE the reference when the fillable bound is
BELOW it, so two things break together: the room test fires backwards (a short
limit far from the floor reads as "already there"), and any walk it did perform
would move the limit UP, away from where a short can fill.

Not live. `repeg_enabled` is `false` in `config/settings.yaml` and defaults to
`False` in `src/config.py`. Board item 183 found this while removing the
far-through-quote entry skip and deliberately left it alone: it predates that
work and fixing it is a behaviour change on a money path with no live exercise
and no recorded outcomes to measure against.

The hazard to watch is ordering: the flag being turned on before the side fix
lands would put the defect straight into production on the short book.

## item 199 — read the unbacked-stop floor off the chart

Moved out of `docs/WORK.md` on 2026-09-30 to keep that file under the
100,000-byte cap `tests/test_status_board.py` enforces. Nothing is
changed; this is the item body verbatim.
 `risk.min_stop_atr_multiple` (2.5) is not sourced — there is no citation for a fixed entry stop at that multiple and both ends of the band quoted at its definition site are unsupported (item 90, 2026-09-30) — so `docs/OUTCOME.md`'s first-ranked remedy applies: reformulate the rule so it needs no constant. Doctrine's own worked example is structural ("does the last higher low still hold?" needs no number because the chart supplies the level), and item 90's pass established that this desk can already do it. `_level_backing_stop` discards any level with fewer than `min_level_touches_for_stop_honor` (5) touches, after which the stop falls to the flat ATR multiple; but that 5 was measured for whether a level is trustworthy enough to justify a stop TIGHTER than the floor, where a level that fails costs a whipsaw. Used as a WIDENING anchor the bet inverts: the stop sits beyond the level, so a level that fails leaves the stop merely wider than needed, which under risk-based sizing costs position size and not loss. That asymmetry has never been examined and it is where the constant's blast radius shrinks. The measured backing is in the repo and is not fitting, because it is measured off bars rather than off this desk's trades: pooled bounce probability rises from 0.516 at first touch to 0.644 at 5+ touches against a flat ~0.48-0.51 shuffled control (7,218 touch episodes, 101 symbols, 5 years; `src/data/levels.py`, `docs/RESEARCH_FINDINGS.md` §7), so a 2-touch level (`MIN_TOUCHES` = 2) still carries real information. The machinery also exists: `_derive_structural_stop_no_atr` (owner-ratified, item 80) already reads a protective stop from structure using the ratified `structural_stop_buffer_pct`; only its trigger condition would change. **Two things this must settle rather than assume, both of which could sink it:** what the floor does when the nearest computed level below entry is very far away (a distant anchor shrinks the position toward `position_sized_to_zero` and may be worse than the flat multiple), and what happens when no computed level exists below entry at all — that residue is the only population an ATR multiple would still govern, and item 90 stays open on it.

## item 90 — the 2026-10-01 classification pass (mechanical, no number changed)
**Verdict: the item stays OPEN, and the honest count of remaining work is 134, not the "half done" the board carried.** What was built is the CLASSIFICATION itself, mechanically and from the ledger, plus the ratchet that keeps it honest; what was deliberately NOT done is any re-derivation, because that is the move this item has already recorded as failing every time — the desk cannot derive these numbers from data it never recorded. (a) `src/number_sources.py` gains `classification()`, which partitions all 329 ledger rows into item 90's three states — sourced or measured; ratified as a structural bound with the reason recorded; unsourceable today with a NAMED recording, built or specified, that would settle it — and a fourth bucket that is the defect, a live number in none of the three. A test pins that every row lands in exactly one bucket, so a row cannot fall through the classification unnoticed. (b) `status: arbitrary` rows may now carry `settles_by:` (`kind`, `state` built/specified, `where`, `records`, `closes_when`). That is the only way to express state 3; before this the schema could not tell "unsourceable but recorded against" from "nothing will ever answer this", which is why the classification could only be produced by hand and never stayed produced. A malformed or unactionable route is a HARD build failure, on the ground that it reads as an answer while silently removing the row from the outstanding count; a missing route is counted instead, because 134 of them exist and deleting rows is not the fix. (c) `MAX_ROUTELESS_ARBITRARY` is computed from `config/number_ledger_route_history.yaml` and checked for EQUALITY — identical in shape to `MAX_ARBITRARY_ENTRIES` and chosen for the same two reasons already learned on that ratchet: a hand-edited literal drifts from its own record, and a ceiling rewards deleting a row rather than answering it. Lowering it requires an appended negative delta naming the row and the recording, in the same commit. (d) MEASURED from the ledger 2026-10-01: 109 not trade-governing, 82 sourced or measured, 3 ratified as a bound, 1 in state 3, 134 in none of the three. The 134 are enumerated by the check, not duplicated in prose, and group as `src.config` 36, `src.agents` 30, `src.risk` 21, `src.pipeline` 18, `src.portfolio_constructor` 9, `src.execution` 8, `src.data` 5, `src.verdicts` 4, `src.pipeline_stages` 2, `src.rotation` 1. (e) The single state-3 row is `src.config.RiskConfig.min_stop_atr_multiple`, routed to the per-closed-trade excursion recording built by the 2026-09-30 pass; its value is UNCHANGED and the recording may not be optimised against, since fitting a number to this desk's own trading record is barred outright. (f) One sentinel moved for a non-trade reason and is declared: `MAX_UNSCOPED_NUMERIC_SITES` 154 -> 155 for `MIN_ROUTE_PROSE_CHARS` (40), the shortest route prose the schema accepts — it governs the ledger's own schema and no size, price, stop or exit.

## item 90 — the 2026-09-30 `min_stop_atr_multiple` pass

Moved out of `docs/WORK.md` on 2026-09-30 for the 100,000-byte cap
`tests/test_status_board.py` enforces. Verbatim; nothing changed.
**2026-09-30 — `risk.min_stop_atr_multiple` (2.5): the value is UNCHANGED, the claim that it was SOURCED is withdrawn, and the reformulation is filed as item 199 rather than refused.** This constant belongs to no tranche (182 is the ladder, 183 the order gates, 185 the trailing numbers, 186 the portfolio ceilings), so it was taken here. A first pass refused it; an adversary pass found that refusal rested on a false history and wrong arithmetic, and what follows is the corrected result. Full reasoning is in `config/number_ledger.yaml` under its id rather than duplicated here. **(a) A false history is deleted from five files, not softened.** The first pass asserted a "3.0 -> 1.5 move on 2026-09-04" and built an argument on it. There was no such move: `config/settings.yaml` went 3.0 (2026-08-27) straight to 2.5 (2026-09-10) and never deployed 1.5, because the commit that carried it squashes PR #269's two legs (3.0 -> 1.5, then 1.5 -> 2.5) into one merge. The wrong date was inherited from a settings comment and then copied into four more places by a change whose purpose was removing rot; it is now corrected at the source. **(b) The ledger's own open question was doctrine-barred and is replaced.** It asked what this desk's maximum-adverse-excursion record says about the point inside the band — an MAE study over the desk's own trades is FITTING, which `docs/OUTCOME.md` bars outright, and it is how the 1.5 was produced in the first place. **(c) There is no cited band, so BOTH ends are unsupported.** The entry carries no source field and `config/settings.yaml` offers only "general swing-trading guidance" with no URL, which doctrine explicitly rejects. Searched and recorded: the pages asserting 2.5-3.0x for a fixed multi-day entry stop are vendor content rather than literature, one secondary claim points the other way at 1.5-2.0x, the corroborating Van Tharp and Chandelier figures are trailing mechanisms the settings comment already concedes, and the top search hit for the desk's own phrasing is now the desk's own PR. The quoted band (2.5-3.0) does not even match the one quoted three lines below it (2-3). 2.5 stays as the INTERIM value and is deliberately not re-picked, because with no cited band moving it is one more unsourced choice. **(d) The reformulation is NOT refused — it is specified and filed as item 199.** The first pass refused a sqrt-horizon floor claiming it pins reward:risk at exactly 1.0; that was wrong twice (the setup and regime scalers still multiply in, giving about 1.17 to 0.83, and the target and stop rules fire on opposite sides of price so they do not share a population) and is retracted. More importantly it tested the wrong reformulation: doctrine's worked example is structural, and this desk already computes levels with touch counts and already has owner-ratified machinery that reads a stop from structure. The asymmetry nobody had examined is that the 5-touch bar was measured for justifying a TIGHTER stop, where a level that fails costs a whipsaw; as a WIDENING anchor a level that fails only leaves the stop wider than needed, which under risk-based sizing costs position size and not loss. **(e) One stale constant fixed and the class closed mechanically.** `src/pipeline.py` fell back to 1.5 whenever the configured multiple was absent or not a real number — a half-landed second leg of PR #269, which is exactly the failure `scripts/definition_of_done.py` exists for. Measured: not reachable in production, and the three test modules that build a pipeline give 108 passed with the fallback at either value, so nothing depended on it. `tests/test_risk_setting_fallbacks.py` now pins all fifteen fallbacks to the DEPLOYED value in `config/settings.yaml`. **(f) The screen contradiction was FIXED ON MAIN by item 185, which landed first and went further; this branch drops its own narrower version.** This pass proposed dividing `STOP_SANITY_FLOOR_FRACTION` by the widest reachable stop multiple instead of the base, taking the ceiling from 20% to 16.67%. Item 185 instead deleted the borrowed 0.5 literal outright, so the ceiling is now `1 / widest_reachable_stop_atr_multiple(...)` = 1/3.00 = 33.3%. Main's form is kept. The FINDING survives and item 185 confirms it: dividing by the bare base was false across a band of names, and the divergence was exactly the risk-off scaler 1.20. Also withdrawn as wrong on the facts: the board's note that this "needs the owner's call because it tightens a live screen" — `universe_screen.enabled` is false, so the screen does not ship on. **(g) THE BLAST RADIUS GREW WHILE THIS PASS WAS OPEN, and that strengthens rather than weakens the interim finding.** As of 2026-09-30 this constant no longer governs only the entry stop. Through `widest_reachable_stop_atr_multiple` (2.5 x 1.00 x 1.20 = 3.00) it now also sets (1) the midday stop clamp — an over-wide proposed stop is no longer refused but CLAMPED to that multiple of the name's own live ATR14 and placed (item 80), and (2) the universe screen's volatility ceiling at 1/3.00, which decides which names are tradeable at all before any seat sees them. Moving 2.5 now moves three money decisions, not one. That is a reason to state the interim status loudly, not a derivation: composing an unsourced number into more rules removes independent literals without adding evidence for any of them. Item 185 records the same point from its own side and also stays open.

Full heading text, moved for the same reason:

**2026-09-30 — `risk.min_stop_atr_multiple` (2.5): the value is UNCHANGED, the claim that it was SOURCED is withdrawn, and the reformulation is filed as item 199 rather than refused.** This constant belongs to no tranche (182 is the ladder, 183 the order gates, 185 the trailing numbers, 186 the portfolio ceilings), so it was taken here. A first pass refused it; an adversary pass found that refusal rested on a false history and wrong arithmetic, and what follows is the corrected result. Full reasoning is in `config/number_ledger.yaml` under its id rather than duplicated here. **(a) A false history is deleted from five files, not softened.** The first pass asserted a "3.0 -> 1.5 move on 2026-09-04" and built an argument on it. There was no such move: `config/settings.yaml` went 3.0 (2026-08-27) straight to 2.5 (2026-09-10) and never deployed 1.5, because the commit that carried it squashes PR #269's two legs (3.0 -> 1.5, then 1.5 -> 2.5) into one merge. The wrong date was inherited from a settings comment and then copied into four more places by a change whose purpose was removing rot; it is now corrected at the source. **(b) The ledger's own open question was doctrine-barred and is replaced.** It asked what this desk's maximum-adverse-excursion record says about the point inside the band — an MAE study over the desk's own trades is FITTING, which `docs/OUTCOME.md` bars outright, and it is how the 1.5 was produced in the first place. **(c) There is no cited band, so BOTH ends are unsupported.** The entry carries no source field and `config/settings.yaml` offers only "general swing-trading guidance" with no URL, which doctrine explicitly rejects. Searched and recorded: the pages asserting 2.5-3.0x for a fixed multi-day entry stop are vendor content rather than literature, one secondary claim points the other way at 1.5-2.0x, the corroborating Van Tharp and Chandelier figures are trailing mechanisms the settings comment already concedes, and the top search hit for the desk's own phrasing is now the desk's own PR. The quoted band (2.5-3.0) does not even match the one quoted three lines below it (2-3). 2.5 stays as the INTERIM value and is deliberately not re-picked, because with no cited band moving it is one more unsourced choice. **(d) The reformulation is NOT refused — it is specified and filed as item 199.** The first pass refused a sqrt-horizon floor claiming it pins reward:risk at exactly 1.0; that was wrong twice (the setup and regime scalers still multiply in, giving about 1.17 to 0.83, and the target and stop rules fire on opposite sides of price so they do not share a population) and is retracted. More importantly it tested the wrong reformulation: doctrine's worked example is structural, and this desk already computes levels with touch counts and already has owner-ratified machinery that reads a stop from structure. The asymmetry nobody had examined is that the 5-touch bar was measured for justifying a TIGHTER stop, where a level that fails costs a whipsaw; as a WIDENING anchor a level that fails only leaves the stop wider than needed, which under risk-based sizing costs position size and not loss. **(e) One stale constant fixed and the class closed mechanically.** `src/pipeline.py` fell back to 1.5 whenever the configured multiple was absent or not a real number — a half-landed second leg of PR #269, which is exactly the failure `scripts/definition_of_done.py` exists for. Measured: not reachable in production, and the three test modules that build a pipeline give 108 passed with the fallback at either value, so nothing depended on it. `tests/test_risk_setting_fallbacks.py` now pins all fifteen fallbacks to the DEPLOYED value in `config/settings.yaml`. **(f) The screen contradiction was FIXED ON MAIN by item 185, which landed first and went further; this branch drops its own narrower version.** This pass proposed dividing `STOP_SANITY_FLOOR_FRACTION` by the widest reachable stop multiple instead of the base, taking the ceiling from 20% to 16.67%. Item 185 instead deleted the borrowed 0.5 literal outright, so the ceiling is now `1 / widest_reachable_stop_atr_multiple(...)` = 1/3.00 = 33.3%. Main's form is kept. The FINDING survives and item 185 confirms it: dividing by the bare base was false across a band of names, and the divergence was exactly the risk-off scaler 1.20. Also withdrawn as wrong on the facts: the board's note that this "needs the owner's call because it tightens a live screen" — `universe_screen.enabled` is false, so the screen does not ship on. **(g) THE BLAST RADIUS GREW WHILE THIS PASS WAS OPEN, and that strengthens rather than weakens the interim finding.** Detail: `docs/BOARD_NOTES.md` ("item 90 — the 2026-09-30 `min_stop_atr_multiple` pass").


**2026-09-30, third pass — the recording is COMPLETED and is now the completion criterion of items 90 and 199, replacing any further re-derivation.** The pinned-at-entry half (`entry_atr`, `initial_stop_loss`, the entry `price`, and `stop_basis` carrying the constructor's own `stop_rule`) and the resolved half (`realized_pnl`, `exit_reason_category` = `broker_stop_fill` when the broker's stop filled) were already on the `trades` row. The gap closed here is the FAVOURABLE excursion: `max_favourable_excursion`, the exact mirror of `max_adverse_excursion`, widened by the same `Database._accumulate_excursions` call inside the same `sync_positions` transaction as the snapshot it is derived from. Without it a stop-out recorded beside a wide adverse excursion cannot be told apart from one that first ran a long way in the desk's favour and gave it all back, which is the question the floor actually turns on. Nothing reads any of these columns back into a decision — no threshold, no gate, no surface — so this cannot change what the desk trades; the accumulation is swallowed on error so a recording fault can never fail a position sync. The stop's distance in ATR multiples is deliberately NOT a column: it is recomputed from entry price, entry stop and entry ATR, per the standing rule against storing what code can recompute. Both excursions are snapshot-frequency FLOORS on the true figures and legacy rows are NULL; a reader who drops either caveat is reading them wrong. Proved by `tests/test_stop_evidence_excursions.py`, including a position that opens, runs against the desk, recovers and then closes still carrying the worst excursion it reached.

**2026-09-30, second pass — the EVIDENCE the floor would need is now being recorded, and the fallback divergence is closed at its source rather than pinned by a test.** Two changes, no change to any traded number. (1) `src/pipeline.py`'s hand-copied fallback literals are now overridden by the default `RiskConfig` itself declares, so the 1.5-vs-2.5 divergence note (e) describes cannot recur for this or any other risk ceiling; an audit of every `_risk_setting` literal in the file found `min_stop_atr_multiple` to be the only mismatch, and `max_position_pct` to be the only name with no declared default (required field), so its literal stays as the genuine last resort. (2) Every closed trade now carries the four facts the desk has never recorded and therefore could never check its floor against: `trades.entry_atr` (ATR14 pinned at entry), `trades.stop_basis` (the constructor's own STOP_RULE_* string, which already separates a stop honoured at a computed level from one set by the ATR band), `trades.max_adverse_excursion` (worst against-entry price accumulated monotonically from each session's position snapshot), alongside the `realized_pnl` and `exit_reason_category` already on the row. **This does NOT reopen the doctrine-barred MAE study of note (b).** The permitted use is falsification only: showing whether the ratified floor was ever VIOLATED in practice — whether trades that went on to resolve well were stopped out by a floor sitting inside their ordinary excursion. Sweeping this record for the multiplier that would have maximised past outcomes is fitting and stays barred; the floor is still read from published doctrine and from the instrument. One caveat any reader must carry: the excursion is sampled at snapshot frequency, so it is a floor on the true MAE — a reading that says the floor WAS violated is trustworthy, one that says it was not means only "not observed". Nothing reads any of it back into a trading decision. **The next pass on this item should ask what the record now shows, not re-derive the multiple.**
## Item 202 — the rehearsal harness reaches the network

Found 2026-09-30 while closing a hole in the test suite's outbound-HTTP guard.

`tests/conftest.py` blocked `requests.get` only. A `requests.Session` bypassed
it, and yfinance does not use `requests` at all — it ships its own transport on
curl_cffi [measured: `yfinance.data` references `curl_cffi` and `session.get`,
and `requests.Session` zero times]. So the guard never applied to the one
library that actually reached the internet.

Closing both holes exposed five tests that silently depended on a live Yahoo
Finance response. Four were not about market data and now state their own
sectors. The fifth is this item: a test whose premise is replaying a RECORDED
session downloads SPY and per-symbol price history on every run, reports
`TECH DATA BLIND SPOT`, and never reaches the Portfolio Manager.

It **fails on `origin/main` today** with the network reachable, taking 196
seconds [measured 2026-09-30], so it is pre-existing rot rather than a
regression from the guard.

Do NOT fix it by loosening the guard, skipping the test, or marking it flaky.
That is the same error as raising a safety sweep's frequency instead of fixing
what the sweep is covering for.


### Item 202 update — the isolation was never real (2026-09-30)

`ops/rehearsal/broker.py::blocked_market_data` replaces the market-data
provider with one that fetches nothing, and `ops/rehearsal/isolation.py`
describes a socket wall covering "Anthropic, OpenAI, OpenRouter, Alpaca,
yfinance, FRED and RSS". Neither held: price data still reached the rehearsal
through **curl_cffi**, which is yfinance's own transport and which the test
suite's outbound-HTTP guard did not cover.

So the rehearsal has been validating against LIVE market data while claiming
to be offline, deterministic and free. With the hole closed, the session
degrades honestly to `status='no_data'` and never reaches the Portfolio
Manager, which is why `test_the_settled_cost_ceiling_still_suspends_paid_analysis`
cannot build its 'before' case.

That test is marked `xfail(strict=False)` with the reason above — NOT as a
flake. It flips to XPASS the moment this item serves recorded market data,
which is the signal that item 202 is done.

### Item 202 update 2 — the swap missed the stage that owns the provider (2026-10-01)

Serving recorded bars was not enough on its own. `TradingPipeline.__init__`
hands the SAME market-data object to the stages it builds
(`MorningResearchStage(market=self.market, ...)`), so replacing
`pipeline.market` afterwards left the technical read — the read a rehearsal
most needs served from the recording — still pointing at the LIVE provider.
Offline that read as "No data for SPY, skipping" for every symbol and the
session degraded to `status='no_data'`; online, before the curl_cffi hole was
closed, it is what actually downloaded the bars. `run_rehearsal` now rebinds
every holder and raises rather than starting if one still points at the live
provider.

Measured after the rebind (2026-10-01): `tech_analyst` runs offline and
appears in `agents_ran`, where before it did not. The test still XFAILs, now
for two different and named reasons: the run ends on
`APIConnectionError: Connection error.` before the Portfolio Manager, and
other components still construct their own `MarketDataProvider`, whose
blocked yfinance crumb fetches retry per symbol (~188s). Those two are what
is left of item 202; the xfail reason on the test says the same thing and
flips to XPASS when they are served.

Not found, and checked because the same class of bug bit elsewhere: nothing
under `ops/rehearsal/` dates anything by `date.today()` or `datetime.now()`.
The only `utcnow()` is the capture timestamp written into the recording's
metadata, which is a provenance stamp, not a trading date.

It also fails on `origin/main` today, taking ~196 seconds of live fetching
[measured 2026-09-30], so the defect predates the guard rather than being
caused by it.
## items 182 / 183 / 185 / 186 — consolidation check against item 90 (2026-09-30)
**Verdict: all four KEPT, none retired.** Each opens with "item 90's half two, surfaced for visibility", but each carries its own DONE WHEN criteria that item 90 does not own: 182 the ladder alert and cash-deficit cushion, 183 the order gates and the dead cash-sweep config removal, 185 the ATR-eligibility question and its two inherited rows, 186 three open owner-appetite answers. Retiring any would lose those criteria. The defect found was in item 90 itself: it claimed the four carry one word-for-word shared criterion, which was false (checked against each block). Item 90's line now names the four tranches and what each covers. No constant, threshold or value was chosen or changed.
## Item 192 (RETIRED 2026-09-30) — local interpreter pinned to CI's
Retired because all three DONE WHEN criteria are satisfied on main, not because
the item was abandoned.
- `.python-version` on main reads `3.11`, and both CI jobs read it via
  `python-version-file` rather than each naming a version.
- A local pytest run aborts and names both versions when the running
  interpreter is not the pinned one (shipped in #792).
- The last open criterion — actually rebuilding the dev `.venv`, which measured
  3.12.3 — was completed 2026-09-30: `pip install uv`, `uv python install 3.11`,
  `uv venv --python 3.11`. `/home/ubuntu/projects/quant-agent/.venv` now measures
  **Python 3.11.16** [measured: `.venv/bin/python -V`]. The previous interpreter
  is preserved at `.venv312` so any session mid-run on it is not broken.
Why it mattered: the split was the direct cause of two confident, wrong agent
diagnoses in one session. A prompt-drift check hashed `ast.dump()` of a parsed
function, 3.12 changed that output, and identical source hashed differently
locally and in CI.
## item 212

Filed 2026-09-30 alongside the revert of PR #857.

What is true in the code: for a Type A (range) entry the structural and
chandelier trail does not run at all until price exceeds the recorded
take-profit target. The +1R breakeven lock and the +2R second ratchet are
separate, ratified, and unaffected.

Why the target gate is a defect: the target is an unsourced number, and the
desk's doctrine bars an unsourced number from governing an exit. Between
entry and the target the position has only its original entry stop, so
nothing follows price up through the part of the move the trade actually
spends most of its life in.

Why PR #857 was NOT the fix, and was reverted: it moved the gate from the
target to +2R. Measured on 33 real production BUY trades, the target's
reward-to-risk is median 1.33 and at most 1.72 — never as high as 2.0. The
target is therefore reachable in practice and +2R never has been, so the
change made range positions LESS trailed on live data, not more. The
backtest showed no difference only because its simulated targets sat nearer
still.

Also verified: the target cannot close or cap a position. No take-profit
order is ever sent to the broker and a target rationale cannot authorise a
sale, so gating this trail is the target's only live behaviour.

The owner's ratified answer is exit-on-alignment: sell when structure, ATR
and an SMA cross agree the trend is over, never on a single made-up level.
The alignment exit on open PR #853 is the candidate replacement for this
gate. Do not build a replacement under this item, and do not re-derive,
widen or replace any multiple.
## item 211 — RETIRED 2026-10-01, both-edge paging deferred to the circuit's own self-clear window, repeat suppression made per-type, and every refusal now readable at /alerts/suppressed

Why the threshold is not a new number. The circuit already answers "how long
before this stops being a blip": `_auto_clear_transient_latch_locked` refuses
to retire a transient latch until `transient_latch_cooldown_minutes` of wall
clock have passed, and refuses again if the day's
`max_transient_latch_auto_clears_per_day` allowance is spent. Paging the owner
the instant the latch is set contradicts the circuit's own stated belief that
the fault may not be real yet. So the paging threshold IS that field. There was
no need to invent one, and inventing one would have been a barred arbitrary
number.

Why an episode is one trigger code on one ET day. The auto-clear allowance is
already counted per ET day against `llm_circuit_events` for exactly this
purpose — "a fault recurring this often is not transient". The episode
boundary reuses that unit rather than defining a second, differently-shaped
notion of "the same fault again".

What this does NOT do. It does not re-enable Telegram: the owner muted it
deliberately on 2026-09-30 and it stays muted. It does not make the desk
quieter about anything an operator must act on — every non-self-clearing
trigger still pages immediately, because there is no window it can expire
inside. It does not migrate the three existing per-symbol markers in
`src/coverage_watchdog.py` onto the new generic helper; that is a refactor of
working code and was left alone so this change cannot alter what they already
suppress. The suppression record IS now on the API, which was the item's last open
criterion: `GET /alerts/suppressed` returns the `suspend_alert_deferred`
rows from `llm_circuit_events` alongside the `suppressed_alerts` block from
BOTH watchdog state files, and reports "could not read it" separately from
"nothing was suppressed" rather than collapsing the two into an empty list.
It still does not migrate the three existing per-symbol markers in
`src/coverage_watchdog.py` onto the generic helper; that is a refactor of
working code and was left alone so this change cannot alter what they
already suppress. There is no dashboard tile — the criterion reads
"API/dashboard" and the API is what shipped.

Correction to the item text as filed. It said `scripts/check_deploy_drift.py`
claims through `coverage_watchdog.claim_typed_alert`. It does not: it keeps
its own `drift_alerted_for` marker in `deploy_drift.json`, which is still a
per-type, per-key, per-day claim but a separate implementation. What it was
genuinely missing is the recording half — a repeat it declined to send left
no trace at all — so `record_state` now writes that refusal through
`_record_suppressed_alert`, into the same state file, where the endpoint
reads it.

Where the live-risk line is drawn. A live-risk alert is one about a position
whose protection is gone or never arrived: a stop that failed to place, a
stop that failed to re-arm after a scale-in, an uncovered position, a broker
rejection. None of them is ever silenced: the cost circuit's deferral applies
only to `_SELF_CLEARING_HARD_TRIGGERS` (paid-provider faults), and the typed
claim helper always releases the FIRST occurrence for a symbol on an ET day,
holding only a byte-identical repeat of the same fault for the same symbol on
the same day — and recording even that. Both halves are proved in
`tests/test_alert_suppression_api.py` and `tests/test_cost_circuit.py`.
## item 214

**Filed 2026-09-30 out of item 157's retirement.** Item 157 built the enforced answer format and both OpenAI-wire routes now send a strict `json_schema` response format for `TechAnalystAnswer`. Its first DONE WHEN — a live call confirming the Google route enforces what was sent — is structurally unreachable, not merely undone: the rehearsal identity is not granted the Google credential and production is the only identity that could make the call, so the confirming pytest would spend real money on the shared account. The replacement shipped on 2026-09-23: `_record_answer_hygiene` tags every real answer with the provider that served it and records fenced-markdown and extra-key violations to `parse_telemetry`. That evidence is being collected and has never been read. This item carries the unanswered question, not the build.

## item 201

The naked window is real and ordinary: every place the desk cancels a protective stop and submits a replacement, the position is unprotected for the width of that round trip, on paths that run on normal days against real open positions.

What is converted. A price-only move is exactly what the 2026-09-30 rehearsal measurement established `replace_order_by_id(stop_price=...)` performs atomically, so both `shift_stops_down` (the ex-dividend gap absorber) and `replace_stop_loss`'s trailing re-price now amend each resting stop in place and cancel nothing. Both the shape test (`_stop_order_amendable_in_place`) and the failure classification (`_amend_one_stop_price`) are shared, because sharing only the shape test leaves the two paths free to drift on the branch where money is lost. The multi-leg case came with it deliberately: 9 of the 11 open positions are fractional and every fractional position is ASSUMED (not measured) to carry the two-leg hybrid stop pair; what is measured is that 9 of the 11 open positions are fractional [measured 2026-10-01, production database, read-only], so a one-order-only atomic path would have left the trailing stop cancelling and resubmitting across most of the book while the ex-dividend path no longer did — protection moving in two directions inside one change.

The failure branch, which is where this desk's incidents come from. Three outcomes, never two. The broker ANSWERING no (400/404/422, or a replacement that comes back rejected or cancelled) means the original stop is still resting and may be said so. No answer at all — a 504, a timeout — means the amend MAY have landed: nothing is cancelled, nothing is written back, and the code states no level, because a log line claiming "still resting at the old price" would be false exactly when it matters. A confirmed amend needs a real order id and a live status; "no exception raised" is a request accepted, not a stop moved.

Partial outcomes are not successes. A 1-of-2 shift returns no order id, so `accepted_stop_order` rejects it, no level is written back and no TRAIL_STOP row is filed for a stop that never moved. A durable `specialist_evidence` row of kind `stop_shift_legs` names every leg — id, quantity, old level, new level, replacement id, outcome — and the owner gets a plain-words alert. An un-shifted stop across an ex-dividend open is NOT conservative: it is wrong by exactly the dividend, in the direction that triggers it, so the likeliest consequence of a silent refusal is an unintended exit at the open.

What stays on cancel+resubmit, stated honestly. A FRACTIONAL quantity amend is refused by the broker (measured 2026-09-30), so any coverage-repairing size change on a fractional position must still cancel and resubmit; a WHOLE-SHARE quantity amend was measured working on the same day, so "quantities cannot be amended" would be an over-claim. The stop-LIMIT fallback leg cannot take a stop_price-only amend without its limit drifting. A bracket/OTO child is UNMEASURED rather than known-unamendable. The lot-consolidating replace is a design choice about preserving per-lot levels, not a broker limit — this very change amends several stops in a loop.

What is NOT in this item. The partial-sell reprotect path is branch `fix/reprotect-cancelled-id`. The scale-in cancel-to-rearm window was retired as item 193.

The dead-replacement hole. A replacement that comes back rejected or cancelled was being reported as "refused, the original is still resting, protection is intact". That is a guess about live protection: if a replace moves the original order to REPLACED before the replacement is accepted — which is ASSUMED here and not verified — a dead replacement can mean the symbol has no protective stop at all. The code now goes back to the broker and classifies the leg from what is actually resting: the original read back is refused, a stop at the new level is amended, an empty book is NAKED and says UNPROTECTED, and a book that cannot be read is unknown. What would settle the assumption: a rehearsal that forces a replacement to be rejected and then lists the symbol's open orders.

Healing the straddle, narrowly. An earlier draft of this work normalised every mismatched leg to the most protective level already resting, on every pass. That was wrong: per-lot stop levels are a design choice the desk maintains (the audit-round-2 fix inside `shift_stops_down` exists because collapsing them "could TIGHTEN a wide lot's stop to the tightest lot's level"), two names in the last thirty days carry multiple buy lots at two distinct stop levels [measured, production database, read-only], and tightening a lot the desk chose to keep wide exits good positions. It also ran ahead of the trailing gates, so stops moved on days the desk had decided to leave alone, and it re-attempted a refused leg every pass forever. It is removed. What heals now is only the inconsistency the desk's own amend just created: when a multi-leg amend comes back partial, the legs that did not move are retried EXACTLY ONCE, at that proposal's own intended level, inside the ratchet floor and the tightening cooldown. A leg whose outcome is unknown or naked is never retried, because the desk does not know where it is. A straddle that survives the retry is recorded and left in place for the next accepted proposal — preserving a straddle is better than collapsing deliberate geometry.

A naked leg is not repaired by the session that finds it. Coverage repair runs earlier in the same position review than both the ex-dividend shift and the trails, so the gap persists until the next intra sweep. That is why the owner is alerted rather than merely logged. A flat position is not a naked one: a replacement is also rejected when the original already triggered, so the position is re-read and an empty book on a closed position is reported as flat. A replace still pending at the broker can present as an empty book too; the two are indistinguishable from one read, so the code reports the loud direction and says so.

Five assumptions that are NOT established, each named at the code that rests on it: that a replace is atomic, that it is atomic across two legs, that a lost answer can never leave the original already replaced, that an empty book during a pending replace is distinguishable from a real gap, and that every fractional position carries the two-leg pair. That last one has been written as measured and is not: the production database holds positions, not an order book, and nobody has queried the broker for the legs. All of them rest on one unreviewed rehearsal.

Open. No production evidence yet shows both hybrid legs amending. It cannot be produced by a desk session (placing a broker order is forbidden) and no amend outcome was ever recorded before this change. Waiting on an ex-dividend was an unbounded wait — 0 of the 80 production trades between 2026-09-02 and 2026-09-30 were ex-dividend shifts — so the trailing path records the same per-leg row on every re-price, and the closing condition is a `stop_shift_legs` row with one whole-share and one sub-share leg, both amended, with distinct replacement ids.
