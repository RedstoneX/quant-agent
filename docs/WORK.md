# QAMC Current Work

## Active finish line

### Session start — read this first

**STANDING PRINCIPLE — NO ARBITRARY NUMBERS, EVER.** Every trade constant
must come from real data or this desk's own record — never a flat
count, round %, or a number that "sounds prudent." **Approval does not
make a flat number non-arbitrary.** Same bug, three times: the flat
5-day hold → a level-break check; a same-day trigger → a two-day close
confirmation (a reclaimed dip is reversal, not breakdown); a flat 5%
risk/trade defended as ratified, when the fix is volatility-based sizing.
Mark unmeasured numbers provisional, never settled.

## DECISIONS PENDING — CI FAILS WHEN ONE GOES OVERDUE

**Do not delete a line to pass the build — decide it, then remove it in
the SAME commit.** Format: `- [ ] DECIDE BY YYYY-MM-DD — question`
(`test_no_pending_decision_is_overdue` parses it).

This exists because a decision was deferred here and forgotten: on
2026-08-28 this file said to "gather a week of rejections then decide."
Nobody came back; four days later the desk reviewed 38 signals and placed
zero trades for it.

**THREE of the four raised on 2026-09-01 were ratified that day and are spec
Phase 12. The fourth was NOT — it was deferred, and I wrongly deleted its line
while writing "all four were ratified". That is exactly the forbidden move this
test guards against, and it made a later session believe the model choice was
settled. Restored:**

- [ ] DECIDE BY 2026-10-31 — Which model should run the desk's actual trade-decision seat?
  **DATE MOVED 2026-09-13 BY THE OWNER, reason recorded — not a silent
  deferral, and not an agent's choice.** His ruling, verbatim: *"We can't run
  model comparison until the job board is clean. Otherwise the test is flawed
  because the data is flawed because the agents are flawed because the board
  is flawed because the project is flawed."* The gating condition is therefore
  the PM TEST GATE section below, not a date — the date exists only because
  this file's format requires one, and it must move again rather than force a
  decision the gate has not earned. **Do not propose running the benchmark,
  and do not ask him to authorise the spend, while any PM-gate item is open.**
  This supersedes the recommendation put to him on 2026-09-13 to spend roughly
  $5 and settle it; that recommendation was wrong for exactly the reason he
  gave, and it is recorded here so it is not made a third time.
  **DATE MOVED 2026-09-02, reason recorded.** The re-measure this decision
  waits on DOES NOT EXIST: the newest file in `ops/model_policy/results/` is
  dated 2026-09-02 and tested the INCUMBENT ALONE, with no challenger, so
  there is nothing to compare. Every earlier file was measured against a
  prompt that has since been rewritten — verified 2026-09-13, the live
  `config/prompts/portfolio_manager.md` hashes to `00ca991d...` and no result
  file in the repo was produced against it. Deciding without a re-run would be
  picking a model from numbers already written down as invalid.
  **The blocking dependency is a benchmark re-run, and it SPENDS OPENROUTER
  CREDITS — real money, and the owner's single stated financial concern.** It
  is therefore an owner call to authorise, not an agent one, and that is why
  this line moved rather than resolved. Weight it against the fact that the
  `portfolio_manager` seat is ~93% of the LLM bill, so this is also the
  largest available saving. See `qamc-llm-cost-concentration`.
  **NOT DECIDED. Do not act on the existing benchmark numbers** — every score in
  `ops/model_policy/results/*2026-09-01*.json` was measured against the OLD
  prompt and is stale (see below). Owner's instruction was to RE-MEASURE after
  the rewritten prompt ships, then decide. The incumbent `openai/gpt-5.5`
  stays until that re-run exists. Re-running is a re-run, not a rebuild — the
  rig reads the prompt from disk.

- [x] RESOLVED 2026-09-03 — Level quality bar for Phase 12.1 (a stop is
  honoured however tight only when its level has 5+ touches). Detail:
  `docs/INCIDENT_HISTORY.md`, 2026-09-03 "a level needs 5 touches, not 2".

- [x] RESOLVED 2026-09-11 — The drawdown-brake anchor multiplier AND its
  sensitivity. Alarms now trip at a multiple of how much the desk's actual
  holdings normally move in a day; owner set the sensitivity to 3.0 the
  same day. Detail: item 32 and `docs/INCIDENT_HISTORY.md`. **3.0 is a
  reversible risk-appetite setting, not a researched number** — none
  exists. Revisit on appetite or live evidence, never by fitting to this
  desk's own record (that is what was rejected).

**Macro `regime_shift` freshness DECIDE BY line — RESOLVED, see item 48.** Superseded rather than answered: the day-count this question was about was removed entirely, not re-tuned. Detail: `docs/INCIDENT_HISTORY.md`, 2026-09-11.

**RECONFIRM AFTER A FEW DAYS LIVE — the per-session LLM call-count cap,
owner instruction 2026-09-03.** This is the cost circuit's real defence
against a runaway loop, and it replaced the deleted per-call spend
reservation. Shipped at `max_calls_per_session: 40`, set from
real production data (worst COMPLETE session on record made 14 calls,
almost all of it `tech_analyst` chunking the symbol universe, not the
`portfolio_manager` — see `docs/INCIDENT_HISTORY.md`, "item 14"). Owner
approved shipping this as a first number, not a final one. Once a few more
days of live sessions exist, re-pull `llm_budget_sessions.logical_calls` and
confirm 40 still sits comfortably above the real ceiling — raise it if a
legitimate session ever gets close, do not lower it on a hunch. Not a
blocking decision; the mechanism is live either way.

**RATIFIED 2026-09-03 — the silence-watchdog threshold (item 17c) set to 2
windows (~1hr of total desk-wide silence), not the 6-window placeholder.**
See `docs/INCIDENT_HISTORY.md`.

**DEFERRED 2026-09-03 — a second, independent alert channel beyond
Telegram.** Owner decision: not now, bigger problems to solve first;
revisit at his discretion, no due date. Item 17(b) (failed-alert retry) is
already shipped regardless — this is only about a channel to fall back to
if Telegram itself goes down, still a real gap, just not an urgent one.

## PM TEST GATE — GARBAGE IN, GARBAGE OUT

**Owner's framing: the PM model test means nothing until everything feeding
the PM is clean.** Blocks the "which model runs the PM seat" decision above.
Index into the audit below, for the board. Cleared seats are deleted from
here once written up in `docs/INCIDENT_HISTORY.md` — this list is what is
still wrong, not a history of what was.

**4. News analyst seat data quality — PARTIALLY FIXED, one gap open.**
**7. PM-input shape/volume redesign — MEASURED and the null-content slice SHIPPED 2026-09-13; two named pieces left, neither of them volume.** The step that was actually missing — nobody had counted the CURRENT prompt, only the 2026-09-02 one — is done: the frozen `run_64290730` fixture rendered through the live `build_user_message` is 100,968 chars over 25 sections, and 22,094 of them (21.9%) were content-free. Full per-section table and the confirmation of item 18's "70%" (it was exactly 70.4%) in `docs/INCIDENT_HISTORY.md` ("item 18d"). Prompt is now 85,933 chars. **What is left is not volume:** (a) macro is the one seat still couriering full reasoning — its 6-paragraph `reasoning_chain` (2,287 chars) is verbatim, deliberately, under "audit these for logic errors"; deciding whether the audit hook is worth a non-bounded seat is a PROMPT change and needs the paid `--replay-run` benchmark, which the rig cannot substitute for; (b) the two largest remaining sections, Technical Analysis (16,736) and Independent Source Agreement (11,902), are both already bounded and both scale linearly with the number of candidates covered — there is no honest cap to put on either, so the lever is how many names get covered, not how each one renders. Earnings, news and tech all now hand over call + conviction + thesis + falsifier. Do NOT re-open this as a size problem.
**8. Every past model-comparison benchmark may be contaminated by bad seat data — OPEN, no re-run yet.** Same shape as the already-known spend-baseline contamination: a benchmark run before tonight's data-honesty fixes could have scored a model on how well it coped with (or quietly hid) empty/wrong input, not on real analytical quality. Combine with the PM model test itself — same re-run, same gate, not two separate jobs.

Detail below, under "DATA QUALITY AUDIT" and "PM-INPUT ARCHITECTURE".

<!-- END PM TEST GATE -->

**DATA QUALITY AUDIT — 2026-09-02, owner priority: this pillar (garbage in,
garbage out) must work before anything else.**

Owner's instruction: every one of the 5-6 shared analyst seats (tech, news,
macro, earnings, smart_money, evening) has data-quality issues — empty
fields, silent death, or empty data passed to the PM as if it were real.
Audited from real production logs, not assumed. Ranked by measured severity:

(Items 1-3 and 5's full write-ups already live in `docs/INCIDENT_HISTORY.md`;
see the struck-through index above for status.)

4. **News analyst — root cause found and fixed: one dropped opening quote
   broke whole-document JSON parsing, not a real 4-field gap.** See
   `docs/INCIDENT_HISTORY.md`. **Still open:** a second, different
   failure (a dropped symbol key) — left unfixed, can't be auto-repaired
   without inventing data.
6. **Macro analyst — CORRECTED 2026-09-03, prior "no defect" claim was
   wrong.** Fires on 52% of runs, not rare. NOT a fetch/pipeline defect —
   FRED's real publication lag is 2 days, but the gate's freshness bar
   assumes 1. Calibration bug, not broken data. Replacement number is a
   risk-threshold call for the owner — see DECIDE BY below. Full
   measurement: `docs/INCIDENT_HISTORY.md`.

**Also shipped: a bad analyst seat now gets its OWN Telegram alert.** Moved to `docs/INCIDENT_HISTORY.md`, 2026-09-11.

**Standing alert-design rule, reiterated by the owner 2026-09-02 (already
in effect for margin/naked-position alerts, now extended to data quality):
every failure alerts in its OWN Telegram message, never bundled into a
normal run summary, and severity is carried in TEXT, never colour.**
Deliberately not deduplicated: a still-broken seat should keep alerting,
not go quiet.

**ITEM 0 CONTINUED — PM-INPUT ARCHITECTURE, owner priority 2026-09-02, NOT
YET IMPLEMENTED, recorded so it isn't lost.** PM must receive concise
recommendation + conviction only, never raw reasoning. The earnings fix
above does NOT cover this — that fixed data GROUNDING, not the
VOLUME/SHAPE reaching the PM.

**Precedent (not invented):** standard buy-side equity-research hand-off
to a PM is bounded — recommendation, conviction, short thesis, named
risks. Full reasoning stays in supporting workpapers, not the PM's copy.

**Measured same day:** `portfolio_manager.py`'s macro section renders
macro's full 6-paragraph `reasoning_chain` verbatim into the PM prompt
("audit these for logic errors" — deliberate, not an oversight, but still
full reasoning not a conclusion). Other seats not yet measured.

**Next, in order:** (1) quantify what every seat forwards to the PM
today, (2) redesign each to the bounded shape above, (3) any prompt
change needs a real paid benchmark — the rig can't verify one (item 1's
own lesson) — so no prompt edit here is "done" without one.

**STATE AT 2026-09-01 END OF SESSION — read this before the older handoff below.**

**SHIPPED AND VERIFIED, on `integration/ship-2026-09-01` (tip `af266de`), pushed:**
Phase 12.1, 12.2, 12.3, all five open branches merged, and four corrections to
the rewritten PM prompt. Full suite green: **3,961 passing**, only the two known
`test_rehearsal_reproduces_cost_ceiling.py` failures that read live production
state. **NOT DEPLOYED.**

**PHASE 11's original four-branch WIP state (dispatched, interrupted,
unverified) is superseded below and fully recorded in
`docs/INCIDENT_HISTORY.md`'s 2026-09-01 handoff entry** — branch names and
per-branch status live there now, not duplicated here.

**SUPERSEDED 2026-09-02 — `allow_margin` is now `true`.** The condition below
was met: the gross cap and the ladder merged and were verified, and the PM
prompt's exposure table moved to 2.0x in the same commit as the flip. Two
things a reader needs that the paragraph below cannot tell them:

- The flip was INERT for longs for its first day. A third ceiling nobody had
  listed — the BUY submit loop's clamp against raw broker cash — held gross
  under 1.0x whatever the setting said. Fixed 2026-09-02; the submit loop now
  draws on a ladder-derived pool. Spec §11.2 carries the detail.
- **2.0x is still not reachable, and that one is the owner's call.**
  `max_total_position_pct: 90` hard-blocks NET exposure, and for a long-only
  book net IS gross: measured, long-only tops out at 0.90x and a long/short
  book at about 1.3x. The standing 2.0x rung and the -8% 1.5x rung cannot
  bind. The PM prompt still asks for 1.60-2.00x on `risk-on`.

The original paragraph, kept for the sequencing it records:

**`allow_margin` is still `false`. It must STAY false** until the gross cap and
the ladder are merged and verified. The PM prompt's exposure table is at 1.0x
cash-only and moves to 2.0x **at the same moment as that flip, never before.**

**THE GATE ON THE 90% SECTOR CEILING.** The owner ratified 90% conditional on
the de-levering ladder being *proven to step*: "The 90% works if you've got the
ladder, so ensure the ladder works." A test must assert the ceiling CHANGES at
each of the four drawdown thresholds, that new exposure is blocked BEFORE any
trimming, and that the ladder is applied EXACTLY ONCE. **That test has not been
confirmed to pass. If it cannot pass, revert the sector ceiling — do not weaken
the test.**

**THE LADDER MUST NOT DEPEND ON THE PM RETURNING ANYTHING.** One candidate model
returns an empty book 1 run in 10. At 1.0x that is a lost day; at 2.0x during a
drawdown it means the desk stays levered exactly when it should be shedding. The
ceiling must come from account state, and the TRIMMING path must be engine-driven,
never driven by the PM proposing SELLs. **Unverified — check it in the code before
enabling margin.**

**WHAT VALIDATES WHAT — learned the hard way tonight.** The rehearsal rig
**cannot validate a prompt change**. It replays recorded answers into a changed
prompt; measured 23-53% overlap, 23 candidates died before sizing, and the
changed code was never reached. It will pass a broken prompt and tell you
nothing. **Rig validates CODE. The model benchmark validates PROMPT.** Moving the
exposure table to 2.0x is a prompt change and the rig cannot clear it.

**The deploy gate PASSED on the current prompt text** (sha
`96856424b02888879b24a99f25f801faaeb090a8057991840a7d5b4fde154862`, 895 lines).
gpt-5.5 scored 1.000 on all 5 runs against 0.850 on every run of the old prompt,
and the two-position collapse is gone, 0 of 5. **Read that narrowly:** three of
the four checks did no discriminating work, only `actionable_book` separated
anything at 15% weight, and a 1.000 means well-formed and grounded, NOT
profitable. The scenario feeds 30 byte-identical candidates, so it cannot measure
stock-picking and must never be quoted as if it could.

**An uncomfortable finding from the same run:** pick identity there is pure model
prior. gpt-5.5 put 10 of 18 picks into index ETFs where chance is about 2; qwen
picked zero index ETFs in 20. **Which model runs the seat partly decides what the
desk buys before any analysis happens.** An earlier claim that the rewrite cut
index reliance was WRONG — the habit moved from one index to two, it did not go
away.

**Still unbuilt after tonight:** Phase 10.2 (deterministic analyst weighting in
Python), the universe pruning design recorded above, and whatever of Phase 11
does not survive verification.

---

**The 2026-09-01 branch-by-branch handoff, the Phase 11 merge record and
the telegram deep-link deploy entry now live in `docs/INCIDENT_HISTORY.md`**
— all finished, all superseded by the state block at the top of this file.
Moved rather than deleted, per the rule below. The 2026-08-27 evening
deploy record went with them.

**Where finished work goes: `docs/INCIDENT_HISTORY.md`. Move it there. Do not
delete it.** This file is capped at 100,000 bytes and is loaded into context
every session, so it must hold ONLY work still to be done. When it fills up,
the temptation is to delete completed sections because git history keeps
them — that was the previous rule and it was wrong. Rex is not a developer
and does not read git, so deleting erased the record of what had broken at
exactly the moment it became history, on a schedule, every time the backlog
grew.

The log is append-only and is never trimmed. Every entry leads with one
plain-language line saying what actually broke, in ordinary words, because
the person who most needs to read it is not a developer; technical detail
goes underneath. Two tests in `tests/test_status_board.py` enforce the cap
and the pointer — `test_work_md_stays_under_a_hundred_thousand_bytes` and
`test_finished_work_has_somewhere_to_go_that_is_not_deletion`. Owner
instruction, 2026-09-01.

Ratified architecture decisions do NOT go in either file — they go in
`docs/QAMC_REMEDIATION_SPEC.md` as a numbered phase (most recent: Phase 10).


**Run the rehearsal rig before you touch anything that trades — owner
instruction, 2026-08-29, not an agent decision.** His words: it exists so we
do not wait for Monday's market open to find bugs, shutdowns and errors, he
wants it used properly and routinely, and he wants that written where he
looks — the board, `docs/phases.yaml`'s `rehearsal_rig` entry, and here.

- What it is: `ops/rehearsal/` runs a full trading session offline against a
  snapshot of production, replaying recorded model responses. Free, about
  50 seconds. Blocks outbound network at the process level, then proves the
  production database is byte-identical afterward. Suppresses operator
  alerts via `QAMC_REHEARSAL=1` so a rehearsal never pages anyone.
- **Deterministic again as of 2026-09-02, and it was not before.** For four
  days it returned PASS or FAIL on identical code depending on which
  recorded responses it happened to draw; the incident entry has the
  measurement. Omitting `--replay-run` now pins the most recent complete
  recorded run of that session type and prints which one under the verdict.
  Do not go back to comparing commits without reading that line.
- **Read the verdict as one of three, not two.** PASS (exit 0), FAIL (exit
  1), and INCONCLUSIVE (exit 2) — the last means the rig could not
  reproduce the session faithfully enough to judge it, and is deliberately
  hard to reach: a pinned run can never return it. It is not a soft FAIL.
- **A PASS is not "the session was fully exercised."** Offline the rig
  resolves only part of the batch — 20 of 56 symbols went unresolved on the
  2026-09-01 morning — and it now says so directly under the verdict. And
  on the morning scenario it cannot currently PASS at all, in any state:
  macro and news fail outright offline, so the session that reaches the
  decision stage is not the one the recorded decision was grounded in. The
  rig can show you a morning got worse; it cannot yet show you one is
  well.
- When to run it: this is the default way to find a bug, not a formality —
  run it before deploying anything touching the session pipeline, and after
  any change to the agents, the risk engine, the cost circuit or execution.
- Why this is not optional: a full trading day, 2026-08-28, was already lost
  to a defect a rehearsal would have caught before the market opened.
- **It can now force a provider to fail (2026-08-31).** `--fail-provider
  agent:kind[:count]` makes any provider attempt fail as a rate-limit, a
  5xx, a timeout, a dead key or an out-of-money error — offline, free, any
  hour. Before this, every response it replayed was one that had SUCCEEDED,
  so the retry loop, the cross-provider failover, and every circuit guard
  those cross could not be exercised here at all. That blind spot is exactly
  what cost the 2026-08-31 open; see the incident entry below. Reproduce it
  with `--fail-provider tech_analyst:rate_limit:2` — two rate-limited
  primary attempts that the failover must rescue.
- State plainly rather than round up: a draft of this note claimed the
  rig's own acceptance test did not pass — that replay ran out of recorded
  responses on the Technical Analyst's chunked calls and could not
  reproduce the 2026-08-28 cost-ceiling failure on demand, and a rig that
  cannot reproduce the failure it was built for is not trustworthy. Checked
  against `origin/main` before writing this rather than repeated on faith:
  that was true only through commit `ee6f671` (2026-08-28 18:31 UTC,
  "fix(rehearsal): un-merge chunked agent rows so replay stops running
  dry"), already merged. Re-run today, 2026-08-29:
  `tests/test_rehearsal_reproduces_cost_ceiling.py` passes both of its
  tests — the fix holds (`portfolio_manager` is now reached, not blocked)
  and the rig can still force-reproduce the original block on demand via
  `config_overrides` when asked to. It is trustworthy for the one incident
  it has been tested against. What it does not yet have is a track record
  as a standing pre-deploy gate — that starts with this entry.
- **CORRECTION, 2026-09-02: that acceptance test no longer passes, and this
  predates today's determinism work.** Re-run against a clean `0bbb69c`
  checkout with the rig UNMODIFIED: `2 failed in 457s`. Both fail for
  reasons that have nothing to do with the pinning fix, and the identical
  failures appear with the fixed rig, so the cause is drift between the
  code and the 2026-08-28 recording, not the rig:
  (1) `test_rehearsal_reproduces_2026_08_28_pm_cost_ceiling_failure` —
  today's pipeline makes more `tech_analyst` chunk calls than
  `run-be9f8f06` recorded ("all 4 recorded response(s) were already
  replayed"), so replay starves mid-batch and 23 of 47 symbols go
  unresolved; (2)
  `test_the_pre_fix_estimator_still_reproduces_the_2026_08_28_block` —
  the forced pre-fix configuration no longer produces the block at all
  (`blocked_agents=[]`). **Consequence: the rig currently cannot
  demonstrate that it still reproduces the incident it was built for.**
  Reported, not fixed — it is a decision about what the acceptance test
  should now assert, not a bug with an obvious repair.

**The correct, production Mission Control address is
`https://ovh-vps.wallaby-bowfin.ts.net/cockpit/`** (Tailscale Serve proxying
to the loopback-only qamc API). The 2026-08-28 config-drift reconciliation
and the stale branch-preview server that was masking a week of cockpit work
under a different address are both closed; full record moved to
`docs/INCIDENT_HISTORY.md`, 2026-09-11.

**A `git checkout` on the box is not a deploy.** The API holds the
cockpit bundle, so `/cockpit` keeps serving the old one until
`quant-agent-api.service` is restarted. Always restart it and then
confirm the hashed bundle filename the server returns matches the one on
disk under `src/api/static_cockpit/assets/`.

**These are not the final values.** `max_paid_sessions_per_mode_per_day: 8` and
`daily_reserved_exposure_limit_usd: 5.50` are stopgaps that the four
cost-circuit fixes are expected to supersede — the session cap should become
dollar-based, and the reservation ceiling should fall once the estimator
reserves from measured history instead of the theoretical maximum. Committing
them is deliberate: git must describe the running system even while the
running system is wrong.

There are now **no uncommitted config deltas on the box.** Verify with
`sudo -n -u qamc git -C /home/qamc/quant-agent status --porcelain`.

**This section does not record a live production pointer — check it
directly:** `sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1`.
This file has documented five different "current" production SHAs in two
days; that was a documentation bug, not something worth repeating. Compare
the SHA you get against `git log origin/main` and the ordered backlog below
to see what production has and what is still pending.


**Two findings from 2026-08-27 that outlive this PR** (the fixed
benchmark-harness finding moved to `docs/INCIDENT_HISTORY.md`, 2026-09-11):

2. **The LLM-spend baseline is contaminated** — see the annotated
   measured-spend section below. Do not build allocation conclusions on it.

**Model-market strategy is TABLED** by owner decision (2026-08-27): fix
everything, deploy, measure a clean baseline against the current build and
allocation, and only then revisit champion/challenger and keeping up with new
models. Do not reopen it before that clean measurement exists.

**Caution — duplicate Phase 2b work in flight:**
`/home/ubuntu/projects/quant-agent-worktrees/phase2b` (branch
`feat/risk-based-sizing`) still holds an independent, uncommitted attempt at
the same Phase 2b sizing work (dirty `src/models.py`, untracked, unwired
`src/data/company.py` — company profile blurbs the owner asked for, never
finished). It predates and duplicates the Phase 2b that actually landed as
`75c0233` on `feat/pm-flex-routing`. It was not touched by this documentation
pass. Reconcile (pull `company.py` forward if still wanted) or discard that
worktree before doing further Phase 2b/PM-prompt work, so two sizing
implementations don't collide.

**Engineering setup:** work as `ubuntu`, never as `qamc`. There is no venv in
the engineering checkouts — use `/home/ubuntu/projects/quant-agent/.venv/bin/python`
with `PYTHONPATH` set to the checkout root and the five dummy API-key env vars
CI uses. Read the live box with `sudo -n -u qamc`. Log timestamps are **UTC**;
the owner is **ET** — convert before quoting times to him.

**Working agreement:** the owner has given a standing autonomy grant — do not
stop at phase gates for approval. Interrupt only for live-capital activation,
new paid dependencies, secrets redesign, destructive infrastructure, or
evidence that a ratified decision was wrong. See `Owner decisions` below.

**Four corrections the owner issued on 2026-08-27. All four were earned:**

- **Stage git paths explicitly. Never `git add -A` in these checkouts.** Other
  sessions and subagents edit the same tree; a blanket stage committed another
  session's in-flight work under the wrong message and had to be unpicked with
  a soft reset.
- **Delegate grunt work to Sonnet subagents at the START of a task** — doc
  passes, verification sweeps, mechanical refactors — not after the work is
  already done on Opus. Budget is the binding constraint on this project.
- **Stop when you find something broken that predates your task.** Report it
  in plain, non-technical language and let the owner decide. The autonomy
  grant covers EXECUTING the agreed backlog, not expanding it.
- **Never state a date or duration from impression.** Get it from `git log`,
  and check the author: everything in this repository before **2026-08-09**
  belongs to the upstream author `yebof`, not to QAMC. Two claims were
  overstated in one day ("rotted months ago" — it broke the same day;
  "unexamined for months" — it was inherited with the fork), both in the
  direction of making findings sound worse than they were.

**Owner decisions, 2026-08-29 (ratified in conversation, not inferred).** The
owner reviewed a brief written for an autonomous overnight session
(`docs/SESSION_BRIEF_OVERNIGHT.md`, since folded into this file and deleted —
creating it was itself a document-authority mistake this file's own rule
exists to prevent) and corrected it on the spot:

- **Short selling ships finished and enabled, not behind a flag** — moved to
  `docs/INCIDENT_HISTORY.md`, 2026-09-11.
- **The next session runs autonomously overnight and must not ask him
  anything.** It reports decisions afterward, in plain language, for him to
  overrule.
- **The desk board (`docs/phases.yaml`, rendered at `/board`) is a source of
  truth.** Defects found during engineering belong on it, not only in
  session notes.

**Operating model for an autonomous session.** It is an orchestrator and
implements nothing inline. Dispatch subagents matched to complexity: Haiku
for documentation, inventory and mechanical edits from a supplied spec;
Sonnet for bounded implementation with real judgement; the strongest
available reasoning reserved for architecture, trading/risk logic and
anything that can lose money. Delegate aggressively to protect the
orchestrator's own context — read diffs and summaries, not whole source
files. Never run two writing agents in the same worktree; read-only
reporters may overlap freely.

**Do not trust a subagent's claim on its word — verify the single
load-bearing assertion, cheaply and adversarially.** Overnight 2026-08-28/29
an agent confidently reported a root cause that was wrong: the documented
diagnosis blamed a data table when the real cause was a header element. It
was caught only because the orchestrator reproduced the claim itself against
live data. If an agent says "tests pass", check the count. If it says "the
fix works", reproduce the fix's effect. If it says "X is the cause", check
that X actually produces the symptom.

**Operational facts that will cost hours if missed:**

- **Never bare `git stash`.** It is a repo-global ref shared across every
  worktree in this checkout, not scoped to one; two agents collided on it
  overnight 2026-08-29. Use `git stash push -m "<name>"` and pop by explicit
  index, or measure baselines in a throwaway worktree instead.
- **Branch protection requires branches to be up to date**, so a merge queue
  must be serialized: merge `main` in, wait for CI, merge, repeat. Never use
  `--admin`.
- **`gh pr edit` fails on this repo** (a deprecated Projects-classic GraphQL
  field). Confirmed 2026-08-29 that it is not edit-specific — `gh pr view`
  trips the same field. Use
  `gh api repos/RedstoneX/quant-agent/pulls/N -X PATCH` instead.
- **Subagents stall on polling loops** and will burn enormous token counts
  waiting on CI. Give every agent an explicit polling budget, or poll
  yourself.
### Ordered backlog — RESUME POINT

## THE FUNNEL QUEUE — why trades do not happen, ranked by measured cost

**This is the top of the backlog. Work it in order.** It is not a survey of
good ideas from other projects; every line below is derived from THIS desk's
own record and carries its own denominator. Do not reorder it from intuition.

**Measured 2026-09-02 from the pre-reset database**
(`data/resets/20260902T181859Z/quant_agent.db` — the live DB was wiped that
day, this backup is the only complete copy). Reproduce with
`scripts/blocked_proposals_census.py`, which is read-only and makes no broker
calls.

**Denominator: 68 entry proposals, 2026-08-18 to 2026-09-02. 15 filled — 22%.**
53 blocked. Zero-fill sessions: **6 of 11**.

Each item is classified WORKING AS INTENDED / TOO STRICT / DEFECT / NO RECORD.

---

An item is only struck through when the fix is merged AND re-measured against
the same 68.

---

**1. The reward:risk floor — 17 of 68 (25%). TOO STRICT. IN FLIGHT.**

*Owner clarification, 2026-09-10: the reward:risk ratio is not rejected as a
concept — judging it against a stop THE ATR FLOOR invented, instead of a real
level, was. A level-backed stop is always judged on its own honest distance.
Stop-geometry half fixed: `min_stop_atr_multiple` is now 2.5 (published
swing-trading doctrine, not this desk's own data) and applies ONLY where no
real level backs the stop — derivation in `docs/INCIDENT_HISTORY.md`,
2026-09-10. Not yet re-measured against the same 68.*

The single largest cause; nothing else is close. 10 died before an order was
built, 7 were killed by the AI Risk Manager citing the 1.5 floor by name.

Independently confirmed 2026-09-02, and the count UNDERSTATES it: where the
floor does not reject outright, the Risk Manager **halves the allocation**
instead ("Halve allocation per R/R enforcement policy" appears verbatim on
XLF, XLE x2, XLB). So the floor both blocks and shrinks, and only the blocking
half is counted above.

It rests on the ATR floor overwriting a real level. Evidence, `run-64290730`
(2026-09-01): SLB entered at $60.10, stop at $55.50 — exactly the 3.0x ATR
floor, not a level — over a 15-session hold, for an analyst reward:risk of
1.28 against a geometric maximum of 1.29. The floor cannot be cleared once it,
not the level, sets the stop. See `qamc-rr-geometry-defect`.

**RETRACTED, 2026-09-04 — do not re-cite:** the earlier "PM's reasoning
assumes R/R 1.67 but the executed order has R/R 1.18" example did NOT show
this mechanism. Re-verification found the stop identical ($61.54) on both
the analyst's read and the executed order; only the ENTRY price drifted
between the analysis snapshot and the live fill. The ATR floor was not
involved. See `docs/QAMC_REMEDIATION_SPEC.md`, the 2026-09-04 correction
near line 2002.

**The gate fails in BOTH directions, and that is the thing to understand.**
It refuses good trades on a fabricated ratio (above), AND it waves through
exactly the wrong names via its own exception clause (below). Fixing one
without the other just moves the damage.

**The exception clause hands out the key to its own lock.** The rule permits
a below-floor pick when a named catalyst justifies it. On the NVDA case the
model complied completely: it named a catalyst, cut size below 1%, stated in
plain text that the ratio was below floor, and the Risk Manager reviewed and
agreed. Everyone followed the rule. **The catalyst came from our own news
feed** — and for any famous name there is always a concrete dated catalyst
available, so the clause can never bind on precisely the names it most needs
to bind on. Our own accountability machinery supplies the escape.

This is NOT a model overriding evidence, and any framing that says so is an
overstatement that has already been made twice in this project and corrected
twice. Blinding the ticker was run 2026-09-02 (`feat/blind-the-ticker`) and
changed NOTHING — NVDA picked 5/5 in both arms, quality identical to four
decimal places. The cause is this gate, not recognition of the name.

Four parts, in this order:
  a. **Fix the geometry** so every R/R computation uses identical stop
     geometry and a level-backed stop is honoured however tight. The 1-ATR
     guard upstream already prevents a stop sitting in pure noise, so the
     floor is redundant in the level-backed case and destructive in it.
     Dispatched 2026-09-02 on `wt/rr-geometry`.
  b. **Make the catalyst CHECKABLE rather than ASSERTABLE.** It must point at
     a specific dated row we already store, not merely be asserted in prose.
     Prose is unfalsifiable and the model is not lying — it is citing real
     news we handed it. Started on `wt/catalyst-loophole` (`e547f04`, WIP
     checkpoint: real code in the PM, constants and pipeline, plus a
     480-line gate test — stopped mid-work to conserve budget, NOT finished).
  c. **Cap any below-floor pick at the smallest starter size IN CODE**, after
     the model submits. Keeps the capability, costs nothing when the catalyst
     is genuine, and does not depend on prompt compliance. Same branch.
  d. **Then** replace the 1.5 hard gate with the already-ratified weighted
     composite score (see `qamc-weighted-scoring-architecture`). Do NOT do
     this before (a) — scoring a fabricated ratio more gently is not a fix.

**Blinding the ticker is fourth on this list, not first, and is arguably
done.** Do not spend more on it; the measured result is a negative one.

**Telling the desk its own record — BUILT AND LIVE, but currently blind.**
`630da15` shipped 2026-09-02 and IS in production: the PM now receives a
"Proposal Conversion (last 21d — what you asked for vs what you got)" block
naming what the machinery keeps refusing and why. **But the book was wiped
the same day**, and that block reads from the history the reset cleared —
the live database now holds 1 target and 1 trade, so the section renders
effectively empty and will stay near-empty until roughly three weeks of new
proposals accumulate. **The capability is shipped; the evidence it needs was
erased hours later.** Do not read a quiet Proposal Conversion block as "no
stuck loops" — read it as "no data yet".

**3. Accepted by the broker, never filled, cancelled — 6 of 68 (9%). WORKING AS INTENDED.**

Price protection behaving correctly, but it is a real cost: the slot was
consumed, the idea aged out, and nothing was bought. Worth revisiting the
repeg policy rather than the limit itself.

**4. A SECOND reward:risk floor at execution time, set to 1.2 — 4 of 68 (6%). WORKING AS INTENDED, BUT.**

Working as designed, but one quantity has two definitions with two different
numbers, and neither is doctrinally grounded. Fold into item 1(b); do not
resolve it separately.

**8. Stop placed on the wrong side of entry — 2 of 68 (3%). CHECKED, NOT A DEFECT.**

Full reasoning + test: `docs/INCIDENT_HISTORY.md`. Stop is
sided correctly at ingestion; the quote moves before construction —
refusal stands.

**10. Slots burned re-proposing names that never fill. PARTIALLY CLOSED,
re-measured 2026-09-03 — see `docs/INCIDENT_HISTORY.md`.**

The memory gap is fixed: `_build_blocked_proposals` (`src/pipeline.py`,
merged 2026-09-02 via `feat/blocked-trade-memory`) gives the PM a
`## Proposal Conversion` section naming its own repeat-offender names and
conversion rate. But it is diagnostic only by explicit design — it gates,
filters and caps nothing — so it cannot fully close this item by itself.
Whether it changes PM behavior is UNTESTED: an unrelated live desk reset
(`scripts/desk_reset.py`, 2026-09-02 18:19 UTC, real tool, working as
intended) wiped all decision-shaped proposal history the same day the fix
shipped, leaving under a day of post-fix data — one proposal (ORCL),
which filled. Not enough signal either way yet. Re-measure again once
several full trading days have accumulated. Actually stopping re-proposals
(vs. just showing them) would need a new gating threshold — that is an
owner decision, not made here.

**17. Backup alert channel — OWNER DECISION, not a defect. (Was: "the desk can switch itself off silently.")**

The original defect (hit live 2026-09-02: a database fault latched
paid-analysis off durably, and the alert about the latch also failed to
send, so the desk sat stopped with nobody told) is closed. All three parts
shipped 2026-09-03, and the one part left genuinely open — whether the
production box was actually running the fix, not just carrying it in the
repository — is now verified closed too (`docs/INCIDENT_HISTORY.md`,
2026-09-13): the desk-wide silence watchdog's systemd timer went uninstalled
on the box for over a week after shipping, was caught daily by the separate
unit-drift watchdog, and was installed and confirmed running 2026-09-13.

  a. SHIPPED 2026-09-03 — `LLMCostCircuitBreaker._run_with_infra_retry`
     separates a transient budget-read failure (retries with backoff) from
     a real, measured breach (latches immediately, unchanged). Full
     detail: `docs/INCIDENT_HISTORY.md` ("item 17(a)/(b)").
  b. SHIPPED 2026-09-03. A failed latch alert is now persisted and
     retried on any later boundary/process, instead of vanishing after one
     failed send. The one thing NOT built is a real second notification
     channel (beyond Telegram) — a new dependency/design tradeoff, not a
     retry-count choice. **This is the only remaining open point of item
     17: DEFERRED, no due date.** The decision and recommendation are
     recorded once, in `docs/BOARD_NOTES.md` ("item 17") — not duplicated
     here.
  c. SHIPPED 2026-09-03 — `src/silence_watchdog.py` +
     `scripts/silence_heartbeat.py`, alerting on "no completed session in
     N scheduled windows", desk-wide. Threshold RATIFIED at 2 (~1hr).
     Production deployment gap (timer never installed on the box) found
     2026-09-12, confirmed fixed 2026-09-13 — see
     `docs/INCIDENT_HISTORY.md`.

Related: `qamc-openrouter-pricing-spof` records the same latch reachable via
a stale price list. That path was fixed 2026-09-02.

**18. 70% of the PM's prompt was earnings-filing prose, not a conclusion — MEASURED 2026-09-02, PARTIALLY FIXED, core cause MERGED 2026-09-04 (PR #252), real follow-ons below.**

Rendered the real PM prompt for the first time (nobody ever had): 199,646
chars, 70% raw SEC-filing extraction from one seat, the actual BUY-eligible
list buried after it at 853 chars (0.4%). Root cause was two layers, not
one: earnings analysts were handing PM a completed extraction FORM instead
of a call (fixed in PR #252 — seat now returns `key_thesis`, a 2-3 sentence
call + falsifier; full 8-field extraction stays on disk for audit), and
there was no ranking rule anywhere in the rulebook at all (fixed 2026-09-03,
Phase 13, extended to all five seats 2026-09-03). Measured result:
205,607→98,351 chars, earnings share 68.1%→33.4%. Live-model check done
2026-09-04 (real AAPL filing through the real earnings prompt on the real
OneCLI-proxied model, coherent output, n=1). **Also invalidated by this:**
the earlier "analysts can run cheaper models" verdict — that measured
extraction, an easy task; concluding is a different, harder task and needs
re-measuring before relying on it again.

**Still genuinely open, not solved by the above:**
  - `familiarity_bias` is graded but never stated in the PROMPT (the
    catalyst-door existence-vs-direction gap itself was fixed 2026-09-03).
  - ~~Earnings is still the single largest prompt section post-fix~~ —
    DONE 2026-09-13. 38 of 65 filings were rendering a four-line verdict
    block with no direction, no thesis and the literal words "not disclosed
    by the analyst"; they are one roll-up line each now. Earnings
    32.5%→21.5% of the prompt and no longer the largest section. See the PM
    TEST GATE item 7 line above for what that leaves.
  - Section reorder (BUY eligibility to the top) deliberately NOT done —
    needs a paid `--replay-run` benchmark to verify, not authorised yet.
  - Whether R/R and net evidence join the production ranking composite —
    owner call, not decided. Sizing-path parity is item 30.
  - A spend cap on the OpenRouter key (mine to set via browser access, not
    the owner's) — still NOT built. This is an API-key-level cap outside
    our own code, the one leg of the cost-circuit replacement that never
    shipped; `cost_circuit.py` carries the flag. Needs the provider's exact
    limit options verified first.

Full trace, the rules-as-code reproduction (`python -m
ops.model_policy.deterministic_selection`), all exact figures, and the
standing warnings (model-behaviour fixes have repeatedly measured as
no-change; do not summarise from code comments, they've been wrong
before): `docs/INCIDENT_HISTORY.md` ("item 18a/18b/18c").

**19. The model's consistency is an ASSET — three uses. Do not start these before item 18.**

Recorded because the owner is right to be sceptical: these are secondary,
and item 18 is the real answer. But the consistency is measured, not hoped
for, and it would be wasteful to rediscover it.

**The measurement:** 5 blinded runs, two arms, quality identical to FOUR
DECIMAL PLACES. Post-fix, 5 more runs: 0.85/0.85/0.85/0.60/0.85, failing the
same check every time with the same two names.

  a. **Use it as a test instrument for item 18.** Same input gives the same
     output, so ANY change in its answer proves the input changed. That is
     precisely how to verify a pipeline change actually reached the model,
     which is what item 18 needs. A noisy model could not do this.
  b. **Stop paying for repeats where the answer does not vary.** The
     portfolio_manager seat is 93% of the LLM bill. Running one call
     instead of five, where consistency holds, is a direct saving. Measure
     first, on the seats where it holds.
  c. **Subtract the bias rather than argue with it.** The pull toward
     famous-but-weak names is stable and measurable — the same two names,
     every run. A consistent bias can be quantified and taken off the score
     arithmetically. Three attempts to fix it with WORDS all measured as
     no-change. Feed this into the already-ratified weighted composite
     score rather than another prompt rule.

**Also worth surfacing rather than suppressing:** the model clearly holds
knowledge about these companies that our stored evidence does not contain
(the blinding test could not remove the news facts that identify an issuer).
Where its view and our evidence DISAGREE, that is either a gap in our data
or a stale belief in its training. Both are worth seeing.

**Owner's caveat, recorded verbatim in spirit:** this may be grasping at
straws, and the real answer is fully understanding what the model receives.
That is item 18. Treat everything here as secondary to it.

**20. GATE THE DECISION ON EVIDENCE COVERAGE — owner's design, 2026-09-02. Do not trade on partial evidence.**

**Owner's ruling, and it overrides my weaker proposal.** I suggested letting
the run continue with reduced coverage and warning the PM which inputs were
hollow. That is wrong: *"the decision matrix is flawed — it's asking it to
make a decision when it doesn't have enough information to make an informed
logical decision."* A decision on incomplete evidence is not a degraded
decision, it is a fabricated one.

**The retry mechanism already exists and costs nothing to use.** `intra_check`
fires every 30 minutes, 09:30-16:00 ET. A skipped run costs half an hour, not
a day. There is no need to choose between "trade on garbage" and "lose the
session".

**It is also CHEAPER.** A run on bad evidence still spends a full
portfolio_manager call (~$0.55, and that seat is 93% of the bill) to produce
a decision nobody should act on. Checking coverage first is free.

**Design:**
  a. **Compute coverage BEFORE the expensive call.** Deterministic Python,
     no model: how many earnings reports are usable, how many technical
     reads survived validation, is smart money present at all.
  b. **Below threshold → do not decide.** Skip the run, record the coverage
     figures and which seats were short, spend nothing.
  c. **The next scheduled run tries again.** No new infrastructure.
  d. **THE SKIP MUST BE LOUD.** Retired item 11 was the desk producing zero
     proposals for a whole day and nobody noticing (closed 2026-09-13, see
     `docs/INCIDENT_HISTORY.md`). A silent skip is that bug again. A skip
     is an event to surface, not an absence to infer.

**The signal already exists — read it, do not rebuild it.** Every earnings
report already carries a `data quality` line, and 11 of them say outright
"insufficient information" or "filing text heavily truncated". The agents
ARE reporting that they did not get what they needed. It is couriered to the
PM as prose inside 140,000 characters instead of being extracted as a
status. **Pull the field the agent already writes.**

**Threshold is NOT an agent's to invent.** It is a risk judgement. Propose a
number with reasoning and have it ratified; do not let a coding agent pick
one, and do not ship a placeholder.

**30. The agreement ladder that prices position size has rungs nobody
derived, and four of its five rungs cannot bind — OPEN, owner call, reframed
2026-09-13.**

The item used to read "port the ranking path's per-seat weights onto the
sizing path". Reading the code changes the question, on three findings — full
reasoning in `docs/INCIDENT_HISTORY.md`, 2026-09-13.

  a. **A per-seat sizing weight is already forbidden, by a ratified rule with
     a passing guard behind it.** A confidence weight may only be derived
     from measured history (minimum 20 resolved calls per seat); there is
     nothing to derive from, and `tests/test_signed_dissent.py` fails on both
     the constant and the symmetry if anyone adds a weight table. The
     2026-09-03 amendment that let the RANKING use a published prior was
     scoped by the owner to the ranking module. Extending it is HIS call, not
     an engineering one — so nothing was changed here.
  b. **The ladder's rungs were never derived.** The measurement beside them
     counted how OFTEN each rung is reached (67% of 75 real targets at one
     net seat, 29% two, 4% three, none above). That is coverage. The stated
     reasoning fixes only a range for rung 1 — "not near 5, not much under 2"
     — and 3.0/4.0 sit inside it by choice. Weighting the count would mean
     inventing an interpolation rule to index a table that was itself
     invented.
  c. **Four of the five rungs are inert as configured.** Per-trade hard
     ceiling 5%; the PM's own restored conviction bands top out at 4%. Rungs
     3-5 are all 5.0 and can never reduce anything; rung 2 (4.0) can only
     bite on a request the prompt already forbids. Only rung 1 (3.0) can
     ever cut a position, and only over the 3-4% slice. This became true when
     item 32 restored the bands; nobody re-checked it then.

**The decision is therefore not "which weights".** It is whether a chosen
five-rung ladder should be pricing size at all, when four rungs are inert and
none of the five was read from anything. Porting the ranking prior across
would not fix the incoherence the item named either — ranking scores a
per-seat strength-plus-confidence composite while sizing counts +1/-1 votes
into a step function, so matching the numbers leaves the two paths still
measuring different things.

**32. The two drawdown systems have never been reconciled — OPEN, owner call,
unchanged since 2026-09-11.**

Everything else once on this item has landed and is written up; what is left
is one decision, described at the bottom. The 5% envelope itself was restored
2026-09-04 (an old unratified
position-size cap was binding first and collapsing delivered risk to ~1%); a
portfolio-level volatility-target overlay was investigated and REJECTED in the
same pass (`docs/OUTCOME.md`); the three loss alarms were rebuilt on a
volatility-relative basis 2026-09-11, owner call, at a PROVISIONAL sensitivity
of 3.0 that is explicitly not researched and is reversible. The PM conviction
bands were restored to 2.0-4.0% / 1.0-2.5% / 0.5-1.0% and merged by the owner
2026-09-10; this file carried a stale "PENDING REVIEW" note against them until
2026-09-13. Detail for all of it: `docs/INCIDENT_HISTORY.md`, 2026-09-04 and
2026-09-11.

**What is actually left.** The drawdown brakes measure rolling-window return;
the §11.2 ladder measures peak-to-trough. They were calibrated independently,
and nobody has decided whether the desk should have one drawdown response or
two. All three alarms are capped at the ladder's -20% owner-alert point, which
is a floor on the disagreement rather than agreement between them. At a
sensitivity of 3.0 that cap no longer binds below roughly 1.5%/session; it
stays as the guarantee for violent regimes.

**35. A stop-widening was observed in pre-clean-slate trade history (Visa, Aug 2026) — DEFERRED, not investigated further for now.**

Real broker order history found a protective stop (V, bought 2026-08-27)
CANCELLED and REPLACED with a wider one on 2026-08-31 — real, not a display
bug. Owner's call: this data predates this week's systemic fixes, was
recorded during active development/merging, and the whole book was later
manually liquidated on purpose to start clean. Pre-clean-slate trade
history should not be treated as reliable evidence of current behavior.
Watch for a recurrence once the desk resumes on a stable beta rather than
dig into this specific historical instance now.

No DECIDE BY — revisit only if it recurs.

**39. Opportunity-cost rotation — owner-requested, LIVE but never yet fired. `src/rotation.py`.** The risk ceiling blocks a candidate but never asks if it beats what is held. Two tiers. The CATEGORICAL tier (a holding that fails the desk's own entry rules today, so it would not be bought now) is the only one that can EXECUTE: `execution.rotation_enabled: true` since 2026-09-12, and it appends one zero-size PM target that travels the identical path as any PM-decided close — constructor, hard risk rules, AI Risk Manager per-symbol refusal, the item-25 holding-discipline claim check, and the protected-sell cancel-write-ahead discipline. No bypass and no new exempt reason category exists; verified by re-reading the exit gates 2026-09-13. The RANKED-MARGIN tier is surfaced to the PM as text and can never execute. **Never executed against a live broker** — no trading timer has run on the box since ~2026-09-03, so the first real rotation is also its first end-to-end proof; watch it.

**39(a). The 25% rotation score margin is an invented constant — OPEN research question, not an owner call.** *Verdict 2026-09-13: the SHAPE is sourced, the NUMBER is not.* `ROTATION_MARGIN_PCT = 0.25` is documented as "the conservative end of a 5%-25% range", but none of the cited sources measures the quantity this desk applies it to.

**The exact open question:** how much better must a new candidate's composite verdict score be than an incumbent holding's before swapping them is worth the round-trip cost — expressed on the verdict score, which is the unit this desk actually ranks on?

**Already searched and ruled out — do not repeat this.** Grinold & Kahn establish that a no-trade region EXISTS under transaction costs; they hand over no number, and their breakeven is in expected-return-versus-cost units this desk does not compute. FTSE Russell's banding is a real production rule of exactly this shape, but its band is a percentile of index-membership rank, not a margin on a signal score. The 2026-09-13 literature sweep on rebalancing tolerance bands (Alpha Architect, Kitces, Morgan Stanley's 10-20% buffers) returns only ALLOCATION-DRIFT bands — how far a position's WEIGHT may wander from its target weight — which is a different quantity with a different unit, and importing its number would be that literature's figure doing a job it never measured. Backtesting a replacement against this desk's own history is FITTING and settles nothing; it is named here only to be ruled out.

**Why this is not currently deciding money:** the categorical tier is checked first and needs no margin, and the ranked-margin tier is information-only, so the invented 25% today gates only whether a comparison is PRINTED in the PM's prompt. It must not be promoted to an execution gate until the question above is answered. **What would settle it:** a published study measuring the turnover-versus-decay trade-off of cross-sectional rank swaps as a function of the SIGNAL gap, not the weight gap; failing that, deriving the breakeven from this desk's own measured round-trip cost (spread plus slippage on the two names involved) against the score-to-expected-return mapping — which the desk does not compute today, and building that is the real prerequisite.

**52. What, if anything, should gate an insider trade on its SIZE — REFRAMED 2026-09-13, no longer an owner call.** *Was: "insider-cluster size should be relative to a filer's holdings, not an absolute dollar filter — owner call." The premise has changed and the item is restated rather than closed.*

**The question, answerable:** should an insider transaction be admitted or refused on any measure of its size — absolute dollars, or size relative to the filer's own holdings — and if so, read off what?

**Where the current numbers came from:** invented. `min_transaction_value_usd: 100000` and `external_min_transaction_value_usd: 250000` in `config/settings.yaml` are flat dollar cutoffs with no source beside either. They are the desk's only size test.

**Already searched and ruled out — do not repeat this.** The Alldredge & Blank paper (J. Financial Research, 2019) that corrected the cluster window is the same paper previously cited for "judge size relative to the insider's own holdings". Read properly, its size bands are **net, per stock, per quarter** — an aggregate over a filer's whole quarter in one name. They do not license a per-transaction admission cutoff of any size, relative or absolute, and a per-transaction gate built from them would be that paper's number used for a job it never measured. An admission gate was therefore deliberately NOT built on 2026-09-13; that restraint was correct and is not the open part. **CORRECTED 2026-09-13 — the "no holdings data" premise was wrong and is now disproved.** Every SEC Form 4 the desk downloads states the filer's holding immediately after the trade; the desk already parses and stores it (verified against a live EDGAR daily index: 77 of 77 open-market P/S rows carried `sharesOwnedFollowingTransaction`). Trade size relative to the filer's own holding is now computed for buys and sells and reported on every observation as `holdings_fraction` / `holdings_fraction_band`. So a relative measure is measurable and is measured. What stays open is only the gating question above — and note the second finding from that work, which narrows it: a per-transaction cutoff on the holdings ratio was BUILT, then REMOVED on 2026-09-13, because Scott & Xu (FAJ 2004) mark only 50% as a significance boundary and measure the sub-10% band as significantly POSITIVE. Do not reintroduce one without a source that measures a per-transaction boundary. Detail: `docs/INCIDENT_HISTORY.md`, 2026-09-13.

**What would settle it:** a published study that measures the predictive content of a SINGLE insider transaction as a function of its size — per transaction, not netted per quarter. Failing that, this desk's own record once enough Form 4 observations exist to compare outcomes above and below a candidate cutoff (which is FITTING, so it settles nothing on its own and is named here only to be ruled out). Best outcome is a reformulation that needs no cutoff: the classification path already sorts routine from opportunistic filings on non-size grounds, and the honest answer may be that size gates nothing and the flat dollar filters should be deleted.

**Cost while unanswered:** two invented dollar cutoffs silently discard insider filings, and nobody can say whether they discard signal or noise. A $90,000 purchase by an officer whose entire position is $200,000 is thrown away; a $300,000 purchase by someone holding $80m is kept. The direction of the error is unknown, which is the actual problem.

**53. The fractional remainder's stop is re-placed automatically while the desk is paused — BUILT AND TESTED, NOT YET RUNNING ON THE BOX. Narrowed 2026-09-14, found 2026-09-12.** Owner ruled fix-it, closing BOARD_NOTES 53's three options: the daily path re-places the missing DAY stop instead of only reporting it. It calls the SAME `repair_stop_coverage` the in-session sweep calls (one home, no second order path), at the level on the position's own last BUY, gated on the broker's published calendar (`is_trading_day` / `get_session_open` / `get_session_close`) and failing closed when it cannot be read. It only ADDS an order — nothing in it can sell, resize, cancel or zero a position. A failed placement alerts on its own once-per-day marker. **Why still OPEN:** 06:15 ET is hours before the bell and a fractional DAY stop cannot be placed into a shut market, so the repairing run is a new unit — `quant-agent-coverage-sweep.{service,timer}`, the same `*:0/30` tick the session timers use, self-gating on the calendar — and **it is not installed on the box**. Until then this is code that never runs — how item 17c sat dead for ten days. Unit-drift reports it `undeployed` daily; close when it appears in `list-timers`. **Not fixable in code:** the remainder is still unprotected OVERNIGHT — this broker accepts a fractional order only as DAY (measured 2026-09-01, code 42210000), so each re-placement buys one session. The only cures are whole shares (declined 2026-09-02) or not holding the fraction.

**55. What IS a structural level — what makes a turning point, and how wide is its zone? OPEN. One third of it is now ANSWERED and shipped; the other two thirds are sharpened, not solved. Filed 2026-09-13, worked twice the same day.** *Consolidates two questions deliberately left unanswered on 2026-09-13; they are one question about one object and must not be split again.*

**The question, answerable in three parts:** (a) how many bars either side must a bar dominate before it counts as a swing point? (b) how far either side of a level's reported price does that level's zone actually extend? (c) how many touches make a level a level?

**Part (c) is CLOSED — 2026-09-13, second pass.** `MIN_TOUCHES = 2` in `src/data/levels.py` is no longer a convention. Two points are the fewest that can define a horizontal line at all, and the published construction of this exact object uses the same figure: Tsinaslanidis, *Technical Trading Strategies, Pattern Recognition and Financial Risk Management* (PhD thesis, University of Macedonia, 2012 — the published method of Zapranis & Tsinaslanidis 2012a, *Applied Financial Economics* 22(19)), §4.4: "Only price areas (bins) with frequencies greater or equal to two are considered as HSAR." Raising it is excluded by that work's own MEASUREMENT rather than by preference (§4.6.1, 733 NASDAQ/NYSE names, 1990-2010): "results indicate that these 'strengths' play no major role in predicting trend interruptions" — on NASDAQ, two-local levels were hit 26,868 times and bounced 60.99%, three-local levels 6,661 times and bounced 61.04%. Pinned by `tests/test_level_match_zone.py::test_min_touches_is_two_and_that_one_is_sourced`. Do not re-open and do not "tighten" it to 3.

**Where the remaining numbers came from:** convention, honestly labelled as such in the code. (a) `PIVOT_WINDOW` = 3 in `src/risk/trailing.py` and `PIVOT_WINDOW` = 5 in `src/data/levels.py`. (b) `CLUSTER_TOLERANCE_PCT` = 1.0 in `src/data/levels.py`.

**What the second pass ADDED — the archetype is now academically sourced, and the numbers are PLACED against measurement.** The desk's construction is not a home-made one. It is, step for step, the published HSAR method: symmetric rolling-window pivots, grouped into bins of equal PERCENTAGE width, a bin with enough members being a level (Tsinaslanidis 2012 §§4.3-4.4). That the level is a band rather than a price is sourced too — Bulkowski via the same §4.2: "Support and resistance are not individual price points, but rather thick bands of molasses that slow or even stop price movement," from which the thesis infers "a support or a resistant level is an area of prices, rather than a specific individual price level, in where local peaks and bottoms reside." So the SHAPE is right and is now cited in the code. Two placements follow, and both are new information:

  * **The zone.** That literature does not derive the width either — §4.4 leaves it a user input: "The third variable 'x' is the desired percentage distance of each bin." What it does give is a measured insensitivity range: 3% illustrated, and footnote 32 records "desired distances of 2%, 4% and 5% are also implemented", with the body stating "Any further parameterization does not affect the empirical findings." `_cluster` chains a pivot in within 1.0% of the cluster ANCHOR, so a desk cluster spans at most 1.0% — HALF the narrowest bin ever tested there. The match zone built from it (`level_zone_halfwidth`, ±1%) spans 2.0%, exactly that narrowest tested bin. The desk therefore sits at or below the bottom edge of the only measured range that exists for this constant.
  * **The window.** Same source, same construction — "in order to characterize the closing price observed at time t (Pt) as a regional peak, when a rolling window with a length of 50 days is used, this price has to be greater than the 25 preceding and 25 following days simultaneously" — and it too reports the outcome "robust to any different parameterization", across 50/100/150-day windows, i.e. 25/50/75 bars EITHER SIDE. That range does not contain 3 or 5. It is evidence the object is insensitive at multi-month swing scale and complete silence at the multi-day scale a stop is actually placed on. It also reads CLOSES, where this desk reads highs and lows. So it does not license either window and must not be cited as if it did.

**Already searched and ruled out — do not repeat any of this.** *First pass, 2026-09-13, the window:* TA-Lib's `FRACTAL` defaults both arms to 2 with no rationale stated; MetaTrader 5's fractal doc defines five bars and gives no reason; fxssi records that five became standard because it shipped as a MetaTrader 4 default, which is a distribution fact and not a measurement; LuxAlgo's swing reference says outright there is no universally best setting. *Second pass, 2026-09-13, the zone, every source named so nobody re-fetches them:* Osler (2000), *Support for Resistance*, FRBNY Economic Policy Review — the origin of the bounce-frequency test and cited by everything downstream, but it evaluates levels PUBLISHED by six dealing firms and so never has to define a zone width of its own; ruled out as a source for (b). Osler (2003), *Currency Orders and Exchange Rate Dynamics*, FRBNY Staff Report 125 — explains WHY zones exist (stop-loss and take-profit orders cluster at round numbers) and gives no width; ruled out. Zapranis & Tsinaslanidis (2012) / Tsinaslanidis (2012) — the closest match to this desk's construction and the source of everything above; leaves x a user input; ruled out as a derivation, kept as a placement. Bulkowski, `thepatternsite.com/SAR.html` and `/TallCandleSAR.html` — the "thick bands of molasses" phrase originates with him and he quantifies nothing; his tall-candle study reports only "Reversals are evenly distributed over the candle body", which says no sub-location within a bar is privileged but gives no zone width; ruled out. Brock, Lakonishok & LeBaron (1992) — the canonical 1% band in this literature is a whipsaw filter on a moving-average crossover, not a support-zone width, and is chosen not derived; ruled out, and do not treat the coincidence with the desk's 1% as a source. `arXiv:2507.01971` (DeepSupp) — states a 3% figure only as an evaluation tolerance for one metric and derives nothing; ruled out. *Structural facts, settled, not to be re-derived:* the two windows never meet — no module imports both, pinned by `tests/test_pivot_window_independence.py` — and a bar dominating 5 bars either side necessarily dominates 3, so the trailing window sees strictly more. Making both 3 or both 5 was rejected: that is picking a number. Moving `CLUSTER_TOLERANCE_PCT` to the literature's illustrative 3% is rejected for the same reason — adopting a foreign default is the same unsourced act in the other direction. Re-tuning the *match tolerance* against the width was ruled out earlier and the tolerance is instead derived from the width itself (`level_zone_halfwidth`), which made the pair CONSISTENT and did not make the width RIGHT.

**What would settle it, now stated precisely enough to run.** The published bounce test is fully specified and has never been run at this desk's own parameters. Take the desk's universe and its own bars; identify pivots with the desk's windows and cluster at the desk's tolerance; call it a "hit" when price enters the zone from outside, a "bounce" when it leaves the way it came and a "failure" when it leaves the other way; compare the bounce frequency against artificial levels drawn at random distances from spot, exactly as Tsinaslanidis §4.5 does. Then sweep the tolerance across 0.5%, 1%, 2%, 3%, 5% and the window across 3, 5, 10, 25 bars either side. This is a READING and not a fit: it measures whether the object the desk has defined is a real feature of its own instruments, and at what width it stops being one — it does not optimise a constant against P&L, and it must not be turned into one by scoring returns instead of bounces. Two outcomes are both acceptable answers to this item: the desk's settings show a bounce edge over random and the width is confirmed as read from its own instruments; or the edge is flat across the whole sweep, in which case the honest conclusion is that the width does not matter and the item closes by saying so. A separate and stronger prize remains available if either sweep is flat: reformulate so no percentage is stated at all — the level's zone becomes the span of the pivot BARS that made it (a pivot is a bar with a high and a low, not a price), which needs no constant and is read entirely off the instrument.

**Cost while unanswered:** 1% is the desk's definition of "the same level" everywhere, and since 2026-09-13 it is also the slack on whether a stop counts as level-backed — which decides whether the ATR floor moves that stop, which changes reward:risk and therefore position size on every risk-sized trade. On a $200 stock the match zone is $4 wide; two bounces $1.90 apart are one level and two bounces $2.10 apart are two, and nothing says that is where the line belongs. The second pass narrows the cost rather than removing it: the desk is now known to be running at or below the tightest setting anyone has measured, so if the object is width-sensitive at all, the desk is on the edge where it would show. The windows cost less because they never meet, but 3 and 5 both still disagree with the vendor archetype's own default of 2 and sit an order of magnitude below the only academically tested range.

**56. Is a stop too wide, and read off what? Three published rules disagree and the desk chose the one that refuses nothing. OPEN, filed 2026-09-13.** *Covers the 2.5x ATR fallback stop AND the reach cap that now gates it — the same question, asked once.*

**The question, answerable:** what is the widest a stop may be, expressed as a quantity read off the instrument, and does the desk's own fallback stop pass its own cap?

**Where the current numbers came from:** `min_stop_atr_multiple: 2.5` is published swing-trading doctrine, cited in `config/settings.yaml` (2026-09-10) — but as a magnitude range, not a derivation, and it replaced a 1.5 that came from this desk's own suspect trade history. `MAX_REACH_ATR_MULTIPLE = 1.5` and `MAX_HORIZON_SESSIONS = 60` in `src/data/levels.py` are the cap, and are inherited: the 1.5 was written for the TARGET derivation ("deliberately looser than the measured-move projection") and carries no source, and the stop-width gate reuses it because it was the only instrument-read width already in the tree.

**Already searched and ruled out — do not repeat this.** 2026-09-12/13, sources fetched: Kullamägi states the stop should not be wider than the stock's own ATR or ADR; Chandelier and Van Tharp put trailing stops at ~2-3x ATR; Van Tharp's sizing (position = risk budget / stop distance) is arithmetic, not an empirical claim, and prescribes a smaller position rather than a refusal. Two candidate caps were tried and rejected FOR CONSEQUENCES, not for evidence: Kullamägi's one-day range would refuse the desk's own 2.5-ATR fallback outright, and the fallback band as its own cap broke 38 pinned tests by refusing every "wider stop, smaller position" trade the ratified sizing rule requires. **That is the unresolved core: a published rule and a ratified setting contradict each other, and a third, looser number was adopted instead of settling which is right.** Also already ruled out: "no floor, no trade" — falsified 2026-09-12 against George & Hwang (2004) and Bulkowski, since a stock near its 52-week high is exactly the stock with nothing computed beneath it. Do not re-propose it.

**What would settle it:** a source that measures stop survival as a function of width in ATRs over a multi-day hold — the point past which a wider stop stops buying survival and only buys a smaller position. That single measurement resolves both halves at once, because it either vindicates 2.5 or vindicates Kullamägi. Separately and more cheaply: the reach cap's 1.5 needs its own answer, since it is currently a target-derivation number doing risk-refusal work.

**Cost while unanswered:** measured 2026-09-12 on the real universe at a 20-session horizon, the reach cap refuses 4 of 101 names (MRVL, VLO, OKLO, NUE) and at the 60-session cap refuses none — a gate that binds on 4% of a normal day and nothing at all on a long horizon. If Kullamägi is right the desk is placing stops roughly 2.5x too wide and sizing every unbacked-stop trade correspondingly small; if the ratified 2.5 is right the desk is fine and the cap is decoration. Both cannot be true and the desk currently behaves as though the question does not exist.

**57. Four of the agreement ceiling's five rungs cannot bind, and the measurement quoted beside it is not a derivation. OPEN, filed 2026-09-13.**

**The question, answerable:** what should the risk ceiling be at each level of seat agreement — and should the schedule have five rungs at all, given four of them are unreachable?

**Where the current numbers came from:** invented, with a real measurement standing next to them that does not derive them. `agreement_ceiling_pct: [3.0, 4.0, 5.0, 5.0, 5.0]` in `config/settings.yaml`, spec §9.4, signed 2026-09-02.

**Already searched and ruled out — do not repeat this.** The measurement in the comment is real but is a COVERAGE COUNT, not a derivation: against production `agent_logs` 2026-08-25..28, 67% of opening/increasing targets carried exactly one aligned source (always technical), 29% two, 4% three, and none ever reached four or five. It says how often each rung is used; it says nothing about what the ceiling at that rung should be. Two structural facts make most of the schedule inert and should not be rediscovered: rungs 3-5 all read 5.0, which is the hard `max_position_risk_pct` cap, so they narrow nothing; and no observation in the measured window reached rung 4 or 5 at all. Only rungs 1 and 2 have ever bound. Note also that the top rung sits at 5.0 against a 4% top conviction band elsewhere in the config — check that interaction before treating rung 3 as live either.

**What would settle it:** evidence that independent-seat agreement predicts outcome — either published work on analyst/signal-agreement and forecast accuracy, or this desk's own record once it has enough rung-2 and rung-3 observations to compare. Better still, a reformulation: if agreement cannot be shown to earn size, the schedule should collapse to the hard cap and be deleted, which is a real and permitted answer.

**Cost while unanswered:** 67% of all targets are sized against an invented 3.0% ceiling rather than the ratified 5% envelope — that single rung is doing almost all the sizing on this desk, and no one can say why it is 3 and not 2 or 4. Meanwhile the schedule reads as a five-tier design and is really a two-tier one, so anyone reading it forms a false picture of how size is decided.

**60. The exit path shares its risk-review seat with the buy plan, wearing five stood-down or inverted checklist exceptions — OPEN, owner call, deferred 2026-09-13 while PR #343 (merged) repaired the seat's honesty about the exceptions rather than replacing it.** The AI Risk Manager audits both the morning BUY plan and the position reviewer's SELLs, but it was built and tuned for the buy plan only. Today's repair stopped it lying to itself on the exit path — it had been telling itself, on every exit review, that the PM's mandatory `continuity_check` and `premortem_check` audit steps were skipped, when those fields do not exist on the position reviewer's schema at all — but it did not give the exit path a reviewer of its own. Four checklist items are now stood down on that path as not applicable (the PM-only reasoning-chain audit; the $0.0 risk/reward geometry check, since an exit has no entry to measure a ratio against; sizing sanity, since there are no BUYs or SHORTs on this path to size; and Tech-block verification, since no TechAnalyst call runs on the midday/close loop that produces one to check). A fifth, event risk, is inverted rather than stood down: an imminent binary event argues for refusing a BUY (it carries less risk through the event) but for closing a SALE (refusing carries the position THROUGH it) — the seat's prompt now says so explicitly (`src/agents/risk_review_mode.py`). Two of the seat's three levers are discarded entirely on this path: `modifications` and `scale_all_buys` are applied only in the morning `RiskStage` (`_apply_risk_modifications`); the exit call site's own `_exit_verdict` is unused. Refusal is the only thing that does anything here. That leaves four deterministic Python gates plus one narrowed AI veto between a wrong exit and the book, and each gate is narrower than it looks: `holding_discipline_claim_check` returns "ok" whenever the position is not `protected` and passes every UNVERIFIABLE claim by design; the metric-contradiction veto is guarded by `and metric_deltas` in `pipeline.py` and never runs without recorded prior metrics for that symbol; the noise band is bypassed by any reason citing external information, which the named-trigger gate all but requires to fire; and the named-trigger gate itself checks that the reason uses recognised words, not that the claim is true. None of the four, alone or together, can catch a plausibly-worded, deterministically-clean, wrong exit — that gap is now named in the seat's own prompt as the job its remaining checklist item exists to cover. The archived record cannot yet say whether this matters: exactly THREE exit reviews exist in the archived database (rows 296, 319, 330), all pre-fix, all APPROVED, zero modifications, 8 of 8 exits allowed — evidence the seat has not yet subtracted value, not evidence it adds any. The direction of harm is the asymmetric one: a veto on the morning path stops a purchase, which costs nothing; a veto on this path stops a SALE, and a refused exit leaves a position whose thesis has broken on the book overnight, protected only by the broker stop. **The decision (BOARD_NOTES 60):** should the exit path get its own reviewer — its own prompt, its own output schema — instead of a buy-plan auditor wearing exceptions? Deliberately deferred rather than answered: the repair that landed makes the shared seat truthful about what it cannot see; it does not settle whether a shared seat is the right design at all.

**62. Three ceilings that shape order size live only in the Portfolio Manager's prompt, with no settings key and no recorded derivation — OPEN, found 2026-09-13 while rendering PM's limits from config (PR #349).** Every other number on that sheet now renders from `config/settings.yaml`; these three cannot, because no setting exists to render. They are: (a) the **earnings-queued 1% RISK cap** on a BUY in a name that has `JUST FILED` (`config/prompts/portfolio_manager.md`, the sizing formula's `queued_cap` and the hard-rule table's row 3); (b) the **momentum-leader starter sleeve's 1.0% RISK per-name ceiling**; (c) the **10% cash floor** the sheet's worked example measures against. What was searched, and found: `grep -rn` across `src/` and `config/settings.yaml` for a settings key or a constant behind any of the three returns nothing — there is no `cash_floor`/`min_cash` anywhere in the repo, and no key for either 1% figure. The one piece of enforcement that exists does not match what the sheet says: `TradingPipeline._clamp_queued_earnings_buys` (`src/pipeline.py`) caps the resulting **position WEIGHT** at a `max_pct` defaulting to **5.0**, and its only call site (`src/pipeline_stages.py`) passes no override — so the belt behind the sheet's "1% risk" is a 5% weight cap, which is neither the same quantity nor the same number. The other two have no deterministic backstop at all. `tests/test_risk_prompt_limits_live.py` exempts all three from the hand-typed-limit check, pointing here; **that exemption is a place to record the question, not an answer to it.** Under the desk's no-arbitrary-numbers rule a live ceiling must be read off the instrument or cited to a published source, and none of the three has either on record. **What would settle it:** for each of the three, a derivation or a published source for the number, or a decision that the ceiling should not exist. For (a) specifically, whether the intended quantity is risk or weight — and if the number survives, all three become settings and render like the rest of the sheet. Deliberately NOT answered inside PR #349: that PR removes second homes for numbers that already have a first one; deciding what an un-derived number should be is a different question and this one is the owner's.

**63. `signal_weight` cannot say "pay attention, and the sign is the other way" — OPEN, no source found, carried out of item 52.** One scalar in `[0,1]` does two jobs: it is the ranking sort key and the dollar multiplier deciding what reaches the analyst seat. It has no way to express direction. Scott & Xu (FAJ 2004) measure an insider sale under 10% of the holding at **+0.68%** size/B-P-adjusted quarterly excess return, significant at 1% — a mildly *bullish* fact arriving on a *sell* row. Today that row gets weight 1.0, identical to an insider dumping 80% of a position at −0.81%; before 2026-09-13 it got 0.0 and vanished from the ranking. Both are wrong, in opposite directions. **Not a number to pick.** Choosing a multiplier that splits the difference would be fitting, and the ratio and band are already reported on every observation so the seat can read the sign itself — this item is about whether the *deterministic* ranking should also know it. **Searched and ruled out:** Scott & Xu themselves (they report band returns, never a weighting scheme); Cohen/Malloy/Pomorski, whose routine/opportunistic split is a binary with no magnitude and no direction; the desk's own history, which has too few insider-sourced fills to measure anything. **What would settle it:** a published source that scores insider signals on a signed scale rather than sorting them into bins, or enough of this desk's own outcome data to read a separation directly — neither exists yet. Until one does, the ratio stays reported and unweighted. Detail: `docs/INCIDENT_HISTORY.md`, 2026-09-13.

**64. The backtest rations the risk budget alphabetically, and cannot do otherwise until it has a candidate ranking — OPEN, found 2026-09-13 while building the best-ranked-first rationing rule (retired item 49; see `docs/INCIDENT_HISTORY.md`, 2026-09-14).** `src/backtest/engine.py` builds every day's candidates and hands `allocate_risk_budget` one `RiskRequest` per candidate at `config.risk.max_position_risk_pct` — the SAME number for all of them. The allocator's pre-decision ordering is largest-request-first with an alphabetical tie-break, so with every request identical the tie-break is the ONLY thing ordering them: on any day the budget binds, the backtest funds candidates in alphabetical order. That work fixed the production path by spending the budget down `rank_verdicts`' own order, and deliberately did NOT touch this one: the backtest is signal-driven and produces no analyst verdicts, so there is no ranking to spend down and inventing a score to stand in for one is exactly what the no-arbitrary-numbers rule forbids. **The consequence:** any backtest run on a day where total requested risk exceeds `max_portfolio_risk_pct` measures a desk that picks trades by ticker spelling — so its results on those days do not describe the desk that now runs in production, and neither the old nor the new production rule can be evaluated by backtesting until this is closed. **What would settle it:** either the backtest gains a deterministic per-candidate score derived from the same signal machinery it already computes (and that score has to be read off something, not fitted), or the engine is honestly documented as unable to evaluate rationing behaviour and every result is reported alongside how many of its days had a binding budget. Nothing was searched for yet beyond confirming the requests are uniform, which was read directly off the code.

**65. Four of the five analyst seats have no strength scale of their own, and the deletion of the fake ones did not answer whether they should — OPEN, owner call, opened 2026-09-13 by the review of PR #348.**

`rank_verdicts` scores a candidate on two things per seat: how far the seat leans (`magnitude`) and how sure it is (`conviction`). Only Technical states a lean — its rating rungs are a strength it actually publishes. News, macro, smart_money and earnings do not, so each of them now reports `NO_STATED_STRENGTH` (0.0) and reaches the ranking through its weighted conviction alone. Nothing is invented, and nothing is borrowed — which is the improvement over both prior states (three unsourced tables, then one borrowed rung).

**The exact open question.** Should those four seats be given a real strength scale — a field in their own schema that measures how far the read leans, distinct from how confident it is — or is "direction plus confidence" genuinely all any of them can say? Today the desk has assumed the second by default, because it is the only answer that requires inventing nothing. That is a defensible default and a poor decision record.

**What was searched and ruled out, so nobody redoes it.**

  a. Deriving a lean from a field the seat already reports. Ruled out and DELETED, 2026-09-13: for news, macro and smart_money it was the same field the verdict hands to `conviction`, so the composite counted one signal twice at three different unsourced spacings. Detail: `docs/INCIDENT_HISTORY.md`, 2026-09-13, item 31.
  b. Borrowing Technical's `buy` rung (0.5) for the seats that have no rungs. Ruled out and DELETED the same day on review: a number read off another seat's scale is not read off this seat's instrument. It is the same failure as (a), one step quieter.
  c. Fitting a spacing to this desk's own resolved calls. Forbidden by `qamc-no-fitting-only-reading`, and impossible anyway — the conviction ledger is far short of the ratified 20-resolved-calls-per-seat bar (`_CONVICTION_OUTCOME_MIN_N`, `src/storage/db.py`), which is itself not yet wired.
  d. Citing a published source for a cross-seat strength spacing. Nothing to cite. The 2026-09-03 literature reviews behind `SEAT_WEIGHT` produced a sourced ordinal ranking of seat RELIABILITY and explicitly no cross-category ratio; none of them speaks to how far a given read leans.

**What evidence would settle it.** Either (1) a seat's own schema is extended with a strength field the analyst must state and justify per call — the same shape Technical already has, which makes the number read from that call rather than chosen for the seat; or (2) the conviction ledger clears the 20-resolved-call bar per seat and a lean can be measured out-of-sample. Until one of those exists, 0.0 stands and the ranking is a breadth-and-confidence ranking, which is what it should be described as.

**Do NOT resolve this by picking a number**, and do not resolve it by removing the four seats from the ranking — the whole point of Phase 13 was that all five seats reach the ordering.

**66. The ranking score is now coverage-sensitive, and `src/rotation.py` compares two names on it — OPEN, watch item, opened 2026-09-13 by the review of PR #348.**

`rank_verdicts` aggregates seats by weighted SUM as of 2026-09-13 (it was an average; the average made a second AGREEING seat lower a candidate's rank — `docs/INCIDENT_HISTORY.md`, same date). A sum is the shape the desk's own edge implies, and this item is not a proposal to undo it. It is the consequence nobody should discover in production:

  * A name's score now rises and falls with how many seats currently cover it. A name bought when it had a live earnings filing and a confirmed smart-money flow will, weeks later, be covered by Technical alone — and score lower for that reason, with nothing about the name having changed.
  * `rotation.py` Tier 2 compares the weakest HELD name against the strongest NEW one on exactly this score, at a 25% relative margin. The margin is a ratio, so the change of scale does not affect it. Coverage decay on a held name does: it makes rotation OUT of a maturing position structurally easier over time.

**Unresolved, and deliberately not decided here:** whether that is right. It is arguably exactly right for an aggressive-growth desk that wants its capital in the most-currently-confirmed names, and arguably a slow bleed of turnover driven by the earnings calendar rather than by the market. Nothing has been measured — the desk has too little resolved history to measure it, and rotation is not yet enabled. **What would settle it:** once auto-rotation runs, count how many Tier 2 rotations were driven by the held name's coverage lapsing rather than by its own signals weakening. If that share is material, the fix is to compare held-vs-new on seats both names share, not to go back to an average.

**68. The doc-conflict resolver silently deletes live board items, and has done it twice in one night — OPEN, found 2026-09-14.** Every branch that closes a board item edits the same three places, so every parallel branch collides there, and a scratchpad resolver applies a fixed rule: on `docs/WORK.md` and `docs/BOARD_NOTES.md`, take the UNION OF THE DELETIONS. That rule is right when each side deleted a different item and wrong in every other case, and it cannot tell the two apart.

**What it has already destroyed.** (a) Items 55-59 were deleted from a branch where neither side had retired them, and written into the retired-numbers line as though closed; the repair then appended a second byte-identical copy of each instead of reinstating the originals, and the duplicate keys read to the board parser as five MISSING items. (b) Two agents working in parallel independently numbered a new finding 63 — one about `signal_weight` being unable to express an inverted sign, one about the backtest rationing alphabetically. Faced with two different items under one number, the resolver deleted both. Restored by hand as 63 and 64. (c) A third collision, found while merging this very item into the queue: this item itself was independently filed as 65 by one branch while another branch's review notes had already claimed 65 for an unrelated finding (seat strength scales) and landed first — renumbered here to 68, the next number not already retired.

**Why it is dangerous rather than annoying.** Both failures leave a file that is valid markdown with no conflict marker in it, so `tests/test_no_conflict_markers_in_docs.py` passes. A vanished item is indistinguishable from a finished one, and the whole point of the no-parked-questions rule is that a question must survive until it is ANSWERED. This tool quietly reverses that. The only reason both were caught is that the board's own orphan test noticed BOARD_NOTES prose with no matching item, which catches the deletion only when the prose survives.

**Already ruled out.** Hand-resolving: it is the same three places every time and a human does it worse under repetition. Abandoning parallel branches: the board only clears at this rate because branches run in parallel. Raising the conflict-marker test's coverage: it already passes on both failure shapes, so it is the wrong instrument.

**What would settle it.** A resolver that is item-aware rather than hunk-aware: parse both sides into a set of numbered items, take the union of the ITEMS, require every number present on either side to be present afterwards exactly once, and STOP for a human when the same number carries different text on the two sides (the collision case, which is a renumber, never a delete). It must assert the post-conditions and fail loudly rather than write a plausible file. Until it exists, every doc merge must be followed by a per-section count of every item number — three independently numbered lists live in this file, so items 4 and 8 legitimately appear twice.

**Retired item numbers — never reuse.** 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 20, 24, 25, 28, 29, 31, 33, 34, 36, 37, 38, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 54, 58, 59, 61, 67, 90, 101, 200 in this queue, and 1, 2, 3, 5, 6 in the PM test gate, were resolved and deleted from this file once written up in `docs/INCIDENT_HISTORY.md`. This file carries what is still wrong; the history file carries what went wrong. Item 38's still-open follow-up survives as item 52, whose own unresolvable residue is item 63. Items 55-59 were briefly and incorrectly listed here as retired by a mis-resolved merge on 2026-09-13 (fix/rehearsal-cost-ceiling-reproduces) — no incident write-up for them exists at the time, `docs/BOARD_NOTES.md` never stopped carrying their prose, and their WORK.md content was restored; they were OPEN, not retired, until 59 was genuinely closed here (this merge) with its own write-up — 55-58 remain open. Item 52 was also briefly and incorrectly caught in that same list despite its own paragraph remaining open above; corrected here — it is not retired. Items 62 and 63 were independently opened with different content by two branches at once (PR #348's review notes and PR #349's PM-limits work); PR #349's landed first, so PR #348's two collided items were renumbered 65 and 66 on merge. Item 65 collided again the same way when this PR's own filing of the resolver defect independently claimed 65; renumbered to 68 here, since 67 is already retired.

## Evidence-only follow-ups

- news-narrative factual drift;
- `actual_provider` attribution oddity.

Do not interrupt natural validation for these unless current evidence shows they materially distort decision quality, truthfulness, or operator understanding.

`get_latest_price` is **not** on this list solely because its request omits `feed`; that concern has been reconciled. Reopen only on concrete production evidence.

- invalid_if: 9/3 log.

