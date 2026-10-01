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

