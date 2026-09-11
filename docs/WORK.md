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

- [ ] DECIDE BY 2026-09-16 — Which model runs the `portfolio_manager` seat?
  **DATE MOVED 2026-09-02, reason recorded — not a silent deferral.** The
  re-measure this decision waits on DOES NOT EXIST: the newest file in
  `ops/model_policy/results/` is dated 2026-09-01, i.e. pre-rewrite and stale
  by this block's own terms. Verified by listing the directory, in this repo
  and on the live desk. Deciding without it would be picking a model from
  numbers we already wrote down as invalid.
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

- [ ] DECIDE BY 2026-09-17 — What should the macro `regime_shift`
  freshness bar be, given real FRED lag (measured `staleness_days=2` on
  all 5 daily series, gate requires `<=1`)? Deliberate risk-tolerance
  choice, not a calibration an agent should pick. Options: (a) loosen to
  match real FRED cadence, (b) keep it deliberately near-unreachable, (c)
  source VIX same-day instead of lagged FRED. Full measurement:
  `docs/INCIDENT_HISTORY.md`. Not decided.

**RECONFIRM AFTER A FEW DAYS LIVE — item 14(c)'s call-count cap, owner
instruction 2026-09-03.** Shipped at `max_calls_per_session: 40`, set from
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
Index into the audit below, for the board — struck-through = cleared.

**~~1. Earnings seat data quality — FIXED.~~**
**~~2. Smart money seat data quality — FIXED.~~**
**~~3. Tech analyst seat data quality — FIXED.~~**
**4. News analyst seat data quality — PARTIALLY FIXED, one gap open.**
**~~5. Evening analyst seat data quality — FIXED.~~**
**6. Macro analyst seat data quality — OPEN, owner risk-threshold call needed.**
**7. PM-input shape/volume redesign (bounded recommendation, not raw reasoning) — NOT STARTED.**
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
normal run summary, and severity is carried in TEXT, never colour — the
owner is red/green colour blind.** Deliberately not deduplicated: a
still-broken seat should keep alerting, not go quiet.

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

### Landed 2026-08-31 — moved to the incident history

Six 2026-08-31 incident/deployment records now live in
`docs/INCIDENT_HISTORY.md` (append-only, never trimmed) instead of being
deleted here to make room, which is how this file stays under its
100,000-byte cap without losing the record of what went wrong.

### Landed 2026-09-03 — RM-modification safety guards, FIXED

A risk-manager "modification" could silently cancel a SELL/COVER exit or
ship a stop/target edit that broke the R/R or noise-band floor a fresh
decision would have to clear. Both guards now live in
`_apply_risk_modifications`. Full detail and tests: `docs/INCIDENT_HISTORY.md`.

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
Stop-geometry half fixed, see item 33 (floor now 2.5x ATR). Not yet
re-measured against the same 68.*

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

**2. Thirteen proposals died with no explanation anywhere — 13 of 68 (19%). PARTIALLY FIXED.**

The constructor-dropped share of this bucket is FIXED (the reason always
existed as a log line, now persisted — going forward only). Full reasoning:
`docs/INCIDENT_HISTORY.md` ("funnel item 2").

**Still open:** the separate 9-item `order_not_placed` shape (an order was
built and then nothing else appears in any record) — structurally different,
looks like an interrupted run, not investigated yet. Until both are closed,
treat this page's percentages as having a residual blind spot.

**3. Accepted by the broker, never filled, cancelled — 6 of 68 (9%). WORKING AS INTENDED.**

Price protection behaving correctly, but it is a real cost: the slot was
consumed, the idea aged out, and nothing was bought. Worth revisiting the
repeg policy rather than the limit itself.

**4. A SECOND reward:risk floor at execution time, set to 1.2 — 4 of 68 (6%). WORKING AS INTENDED, BUT.**

Working as designed, but one quantity has two definitions with two different
numbers, and neither is doctrinally grounded. Fold into item 1(b); do not
resolve it separately.

**5. Allocation rounds to zero shares — 3 of 68 (4%). CHECKED, NOT A LIVE DEFECT.**

Full reasoning + regression test: `docs/INCIDENT_HISTORY.md`. Fractional
sizing already prevents this; the 3 hits predate it.

**6. No structural level from which to derive a target — 3 of 68 (4%). TOO NEW TO CLASSIFY.**

All three are from 2026-09-02, i.e. one day old. Re-measure before acting;
this may be a new regression from that day's ship.

**7. AI Risk Manager vetoes the entire plan for incoherence — 2 of 68 (3%). TOO STRICT.**

Notable because it is reproducing AFTER a fix intended to stop exactly this.
One veto discards every trade in the plan, so its cost is superlinear.

**8. Stop placed on the wrong side of entry — 2 of 68 (3%). CHECKED, NOT A DEFECT.**

Full reasoning + test: `docs/INCIDENT_HISTORY.md`. Stop is
sided correctly at ingestion; the quote moves before construction —
refusal stands.

**9. Tail causes — 3 of 68 combined. WORKING AS INTENDED.** Moved to
`docs/INCIDENT_HISTORY.md`, 2026-09-11.

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

**11. Fourteen outright agent failures, ten of them on 2026-08-25 alone. DEFECT.**

The desk produced **zero proposals that entire day** and this was not noticed
at the time. A day of total silence looks identical to a quiet market from
every surface we have. Related to item 2.

The related-but-separate silent-feed-outage watchdog once parked on
`wt/empty-levels` is now built and merged (it does not fix or explain
2026-08-25 itself — see the caveat in its own docstring). Every run now
counts what share of resolved symbols came back with no structural level or
no bars at all, persists it, and pages the owner directly when the share
crosses a threshold too high to be a coincidental quiet market. Full
detail: `docs/INCIDENT_HISTORY.md`, 2026-09-03 entry.

**~~12. The desk's own funnel reporting misattributes vetoes. FIXED 2026-09-03.~~** See `docs/INCIDENT_HISTORY.md`, 2026-09-03 entry.

**14. Replace the budget guard — SHIPPED on `feat/replace-budget-reservation`.**

Per-call cost reservation deleted entirely (it held ~2.6x what it actually
spent, stopping the desk on money never spent — full reasoning in
`docs/INCIDENT_HISTORY.md`, "item 14"). Replaced with (b) a settled-cost cap
checked before and after each call, and (c) a plain per-session call-count
cap as the real defence against a runaway loop. (a), an API-key-level spend
cap outside our code, is NOT built — flagged in `cost_circuit.py` pointing
back here, needs the provider's exact limit options verified first.

**(c)'s number (40) is measured, not a placeholder** — set from real
production data (worst COMPLETE session on record made 14 calls; see the
RECONFIRM note above and `config/settings.yaml`'s own comment on
`max_calls_per_session`). Everything else in this item is implemented and
tested.

**15. We cannot tell a stale price from a live one — POSITION-MARK SLICE SHIPPED, QUOTE/BARS SLICE STILL OPEN.**

Full reasoning: `docs/INCIDENT_HISTORY.md` ("item 15"). Shipped: held
positions now carry real provenance, never fabricated as fresh (Alpaca
supplies no mark timestamp, so `freshness` is correctly tagged
`"unknown"`). Still open, the bigger half — tagging live quotes and
historical bars the same way, which is what items 5/9/11 actually need:
needs an owner decision between two competing `read_price_bars`
implementations (`rescue/price-provenance` branch), a real architecture
choice, not a mechanical merge.

**16. The afternoon spending reserve — MOOT, deleted with item 14.** Moved to
`docs/INCIDENT_HISTORY.md`, 2026-09-11.

**17. The desk can switch itself off silently — DEFECT. Observed, not theorised.**

Hit live 2026-09-02 while running a benchmark on a scratch copy. A database
that could not be opened tripped the paid-analysis emergency latch, which is
DURABLE — it survives restarts and requires a human to clear a file before
any paid analysis runs again. **And the alert about it failed too**, printing
"cost-circuit unavailable alert was not delivered to Telegram".

So the failure mode is: desk stops thinking, nobody is told, and it stays
stopped until a person happens to look. On an unattended desk that is a day
(or a weekend) of no trading that presents as a quiet market.

**Three distinct defects, and they compound:**

  a. **SHIPPED 2026-09-03 — `LLMCostCircuitBreaker._run_with_infra_retry`**
     separates a transient budget-read failure (retries with backoff) from
     a real, measured breach (latches immediately, unchanged). Full
     detail: `docs/INCIDENT_HISTORY.md` ("item 17(a)/(b)").
  b. **SHIPPED 2026-09-03.** A failed latch alert is now persisted and
     retried on any later boundary/process, instead of vanishing after one
     failed send. A real second notification channel (beyond Telegram) was
     NOT built — a new dependency/design tradeoff, not a retry-count
     choice. **DEFERRED, no due date — see the note above.** (This line
     used to carry its own "DECIDE BY 2026-09-17" text; PR #234 deferred
     the decision and removed the DECISIONS PENDING copy but missed this
     duplicate. One status, recorded once, above.)
  c. **SHIPPED 2026-09-03 — `src/silence_watchdog.py` +
     `scripts/silence_heartbeat.py`**, alerting on "no completed session in
     N scheduled windows", desk-wide. Threshold RATIFIED at 2 (~1hr), not
     the 6 shipped with — see `docs/INCIDENT_HISTORY.md` ("item 17c").

All three parts of item 17 are now shipped; the remaining open point is the
second-channel decision under (b) above.

Related: `qamc-openrouter-pricing-spof` records the same latch reachable via
a stale price list. That path was fixed 2026-09-02; **this one was not** —
the latch itself is the shared hazard, not any single route into it.

**18. 70% of the PM's prompt was earnings-filing prose, not a conclusion — MEASURED 2026-09-02, PARTIALLY FIXED, core cause MERGED 2026-09-04 (PR #252), real follow-ons below.**

Rendered the real PM prompt for the first time (nobody ever had): 199,646
chars, 70% raw SEC-filing extraction from one seat, the actual BUY-eligible
list buried after it at 853 chars (0.4%). Root cause was two layers, not
one: earnings analysts were handing PM a completed extraction FORM instead
of a call (fixed in PR #252 — seat now returns `key_thesis`, a 2-3 sentence
call + falsifier; full 8-field extraction stays on disk for audit), and
there was no ranking rule anywhere in the rulebook at all (fixed 2026-09-03,
Phase 13, extended to all five seats by item 31). Measured result:
205,607→98,351 chars, earnings share 68.1%→33.4%. Live-model check done
2026-09-04 (real AAPL filing through the real earnings prompt on the real
OneCLI-proxied model, coherent output, n=1). **Also invalidated by this:**
the earlier "analysts can run cheaper models" verdict — that measured
extraction, an easy task; concluding is a different, harder task and needs
re-measuring before relying on it again.

**Still genuinely open, not solved by the above:**
  - `familiarity_bias` is graded but never stated in the PROMPT (the
    catalyst-door existence-vs-direction gap itself was fixed 2026-09-03).
  - Earnings is still the single largest prompt section post-fix (~35
    filings/day at 4 lines each is real volume) — cutting/summarising it
    further is queued, not done.
  - Section reorder (BUY eligibility to the top) deliberately NOT done —
    needs a paid `--replay-run` benchmark to verify, not authorised yet.
  - Whether R/R and net evidence join the production ranking composite —
    owner call, not decided. Sizing-path parity is item 30.
  - A spend cap on the OpenRouter key (mine to set via browser access, not
    the owner's) — see item 14(a).

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
  d. **THE SKIP MUST BE LOUD.** Item 11 is the desk producing zero proposals
     for a whole day and nobody noticing. A silent skip is that bug again.
     A skip is an event to surface, not an absence to infer.

**The signal already exists — read it, do not rebuild it.** Every earnings
report already carries a `data quality` line, and 11 of them say outright
"insufficient information" or "filing text heavily truncated". The agents
ARE reporting that they did not get what they needed. It is couriered to the
PM as prose inside 140,000 characters instead of being extracted as a
status. **Pull the field the agent already writes.**

**Threshold is NOT an agent's to invent.** It is a risk judgement. Propose a
number with reasoning and have it ratified; do not let a coding agent pick
one, and do not ship a placeholder.

**25. "Don't sell a protected position without a named reason" is prompt-only, same shape as the PM's catalyst gap — DONE 2026-09-04.** Nothing open. Detail: `INCIDENT_HISTORY.md`, 2026-09-04 "a sell whose stated reason is provably untrue now actually gets stopped", and 2026-09-03 "item 25."

**28. `test_rehearsal_reproduces_cost_ceiling.py` is broken on main — STILL BROKEN, this file's own FIXED claim was wrong.** Marked FIXED 2026-09-04 (config keys the test forced no longer exist, after item 14's cost-circuit rewrite) but re-verified directly 2026-09-10, three separate times against a clean `origin/main` checkout: this test still fails, identically, every time. Whatever landed did not actually resolve it, and nobody re-checked the claim before writing FIXED. Needs someone to actually read the failure and re-diagnose it — not re-apply the same fix that already didn't work. See `docs/INCIDENT_HISTORY.md`, 2026-09-04 "acceptance test broken on main by deleted cost-circuit config keys" for the (incomplete) original diagnosis.

**29. The analyst scorecard was already built and is already live — item withdrawn 2026-09-03, corrected after being written up as new work in error.** See `docs/INCIDENT_HISTORY.md`, 2026-09-03 "the analyst scorecard got written up as missing work; it already existed."

**30. The sizing path still owes the same amendment the ranking path just
got — deliberately NOT done yet, owner should decide scope first.**

2026-09-03: `src/verdicts.py::SEAT_WEIGHT` (ranking/tiebreak only) moved
from equal-weight to a research-informed prior — owner-amended §13.3.
`src/risk/rules.py::SEAT_WEIGHT` (the §9.4 signed sum that actually PRICES
position size) was deliberately left untouched in the same pass, for a real
reason, not an oversight: `agreement_ceiling_for_score` indexes a discrete
ceiling schedule by an INTEGER net-agreement count. A per-seat float weight
turns that into a fractional score, which needs the schedule itself
redesigned (round to nearest int? interpolate between rungs?) — a second,
separate risk-logic decision, not a drop-in constant swap. Flagged rather
than bundled in.

**31. All five seats now reach the ranking, not just Technical — 2026-09-03.**
News, macro, earnings, smart_money each got a `to_verdict()`, wired into
`rank_candidates` via `_collect_seat_verdicts` (one bad entry drops only
that seat, never the run — caught and fixed a real gap: an
`EarningsAnalysis`'s own `symbol` is LLM-declared and can diverge from the
pipeline's ground-truth wrapper symbol, now dropped on mismatch). Known
simplification: macro's verdict is one broad read applied to every symbol,
not the sector-adjusted stance `build_evidence_registry` already computes
elsewhere in the same prompt. Also open: three of the four new seats'
magnitude mappings are reasoned but unmeasured judgment calls, flagged by
their own authors, not yet independently reviewed.

**32. The ratified 5% per-trade risk envelope was not actually being delivered — MOSTLY FIXED, one real judgment call left.**

An old, unratified position-size cap bound before real risk-based sizing
ever did, collapsing delivered risk to ~1%. **Fixed and merged
2026-09-04**; a portfolio-level volatility-target overlay was investigated
and REJECTED in the same pass (`docs/OUTCOME.md`). Resolved detail:
`docs/INCIDENT_HISTORY.md`, 2026-09-04.

**PM conviction-band restoration — PENDING REVIEW, NOT rejected.**
Corrected 2026-09-04: the original PR (#259) was mechanically
auto-closed by GitHub as a side effect of an unrelated branch deletion
(its base branch was deleted when #258 merged) — the owner never saw or
judged its content, was asleep at the time, and did not close it. Real
content restored on a fresh PR from the same commit. Bands proposed to
widen back to their pre-compression 2.0-4.0%/1.0-2.5% range now that the
notional-cap bug they were compressed for is fixed. Still needs real
review and the owner's actual sign-off — treat as open, not decided.

**Drawdown alarms rebuilt on a volatility-relative basis — FIXED
2026-09-11, owner call.** The three loss alarms (daily circuit breaker,
5-day and 20-day brakes) were each a fixed percentage of equity. The
2026-09-04 fixes made those percentages track the real risk unit and made
them √time-consistent, but they were still frozen numbers. **The owner
refused a recalibration**: a fixed percentage is only right for the
volatility regime it was chosen in, markets are not stationary, and a
recalibrated frozen number has the identical flaw. So the BASIS changed,
not the calibration — each alarm now trips at a multiple of how much **the
book actually held** normally moves in a day, reconstructed from its real
holdings' market price history at their real weights, recomputed every
session and scaled per window by √time.

**Corrected same day, before merge — the load-bearing half.** The first
implementation measured the ACCOUNT's own equity curve. Owner rejected it:
the post-reset account ramps from cash for weeks, a mostly-cash account
barely moves, so the measurement would have been far too small and the
alarms far too tight — firing constantly once actually invested. And the
account's record is a record of malfunction anyway. Holdings work from day
one. Reasoning: `docs/INCIDENT_HISTORY.md`, 2026-09-04 and 2026-09-11.

Settled vs. provisional — read before citing either half:

- **SETTLED (architecture).** Stationarity flaw gone, the yardstick never
  touches the desk's own performance record, no warm-up needed, √time
  expressed once rather than as drift-prone per-window constants.
- **PROVISIONAL (sensitivity).** 3.0, owner, 2026-09-11. Reversible and
  explicitly NOT researched or validated — no citable standard exists. At
  a ~1%/session book: -3.0% daily, -6.7% over 5d, -13.4% over 20d (was
  -6.7% / -15% / -20%). It replaced 6.7, which measurement showed left the
  daily breaker firing only on a ~6.7σ session — dormant.
- **NOT touched, deliberately.** Position sizing. Volatility here is only
  the alarm's yardstick; volatility-target sizing stays REJECTED
  (`docs/OUTCOME.md`).

**STILL OPEN, OWNER CALL — full reconciliation of the two drawdown
systems.** Unchanged, and the reason all three alarms are capped at the
§11.2 ladder's -20% owner-alert point: the brakes measure rolling-window
return, the ladder peak-to-trough, calibrated independently, and nobody
has decided whether the desk should have one drawdown response or two.
The cap is a floor on the disagreement, not agreement. At 3.0 it no longer
binds below ~1.5%/session; it stays as the guarantee for violent regimes.

**Conviction-band question — DECIDED 2026-09-11, owner call:** restore
the pre-compression bands. See item 32's conviction-band entry below.

**33. The two "is this trade worth the risk" checks disagreed with each other — FIXED, pending review.**

The PM's eligibility check read the model's self-reported reward:risk;
order construction separately derived it from the real structural target
and shipped stop — each "fixed" in isolation, never checked against the
other. Confirmed on a real trading day: the names passing each check
DIDN'T INTERSECT AT ALL. **Fixed 2026-09-04, PR #257** — PM eligibility
now reads the same derived target construction uses, re-measured against
real data (0/0 overlap on two separate real samples before the fix).

**Uncovered by that fix, and now FIXED IN TURN — the entry stop-width
floor was structurally broken, not merely tight.** Old floor (3.0× ATR,
never derived from anything) cleared 0 of 42 real candidates and, for range
trades (this desk's majority setup), 0 of 222 real signals at any stated
horizon. Full 2026-09-04 measurement: `docs/INCIDENT_HISTORY.md`.

**Floor history: 3.0 (undated) -> 1.5 (2026-09-04) -> 2.5 (2026-09-10),
current.** The 1.5 was Sweeney MAE analysis on this desk's own ~2-week trade
signals — later found to overlap the window some seats misreported
confidence/data quality in, so no longer trusted as the sole basis for a
risk-of-ruin number. **2.5 instead comes from published swing-trading
doctrine** (fixed entry stops run 2.5-3.0× ATR for a multi-day hold),
independent of this desk's own data. Applies ONLY when no real level backs
the stop — a level-backed stop is always honoured at its own distance,
never this number (see item 1's clarification above).

Setup scalers (breakout ×1.00, range ×0.90 — corrected 2026-09-04 from a
backwards ×0.85/×1.15) are unchanged by the base move. Reachable floor now
2.14-3.00× ATR. Known, disclosed tension: the 1.5 reward:risk floor needs
roughly `sqrt(hold_sessions) >= 1.5 x effective_multiple` to clear — ~10
sessions at the tightest case (matches this desk's real observed holds),
~20 at the widest (a real ask). Not eliminated, moved into a range this
desk's stated horizons can plausibly satisfy. **Re-measure once honest
post-fix trade history exists** — not a permanent constant. Full
derivation: `docs/INCIDENT_HISTORY.md`, 2026-09-10.

**Known consequence, since RESOLVED — see item 47.** (This paragraph
previously said margin was off and >100% notional was unreachable
regardless; both were already false when written. See item 47 and
`docs/INCIDENT_HISTORY.md`, 2026-09-11, for the correction and the real
number.)

**34. Exit management barely managed, and ranking was close to alphabetical — FIXED, pending review.**

The noise buffer that vetoed an early discretionary exit came out about as
wide as the actual stops on most pre-stop-fix positions, so nearly all
real exits this month were the broker's stop firing, not a judgment call.
Ranking ties broke alphabetically on most real days — an undisclosed bias
toward early-alphabet tickers. "Range" trades never protected gains until
price fully reached target. **Fixed 2026-09-04, PR #256** — exit buffer
now scales with real trading sessions held (√sessions, matching the
target model; a same-day review caught the first version of this fix
using calendar days instead, over-widening the band across every
weekend — corrected before merge, residual gap: still counts a market
holiday as a session), ties break on real reward:risk quality, range
trades get a standard +1R breakeven ratchet. All three pending review,
not pending a decision.

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

**36. Congressional (House + Senate) trading data added to smart-money — SHIPPED 2026-09-04.** Full detail: `docs/INCIDENT_HISTORY.md`, 2026-09-04.

**Still genuinely undecided:** `smart_money.congress_enabled` remains False (off) — flipping it to True has not been ratified.

**37. Eleven PRs open at once tonight (#249-#262) — merge ORDER matters, see `docs/INCIDENT_HISTORY.md`.**

**~~38. Insider cluster window was 7x the cited research — FIXED 2026-09-04.~~**
Detail: `docs/INCIDENT_HISTORY.md`. Separate, NOT fixed here:
the same paper says size should be relative to holdings, not an absolute
dollar filter — needs holdings-size data QAMC doesn't have; owner call.

**39. Opportunity-cost rotation — owner-requested. `src/rotation.py`.** The risk ceiling blocks a candidate but never asks if it beats what is held. PM's prompt surfaces one comparison — weakest held vs. strongest new-with-no-room — when existing book risk is past the tradeable floor. 25% score margin gates it (PROVISIONAL, cited, `SEAT_WEIGHT`/31's posture); an ineligible holding needs no margin. Surfaces only, never edits. Design in `docs/INCIDENT_HISTORY.md`.

**41. A persistently broken ticker in the intraday scan could fail silently forever — FIXED 2026-09-10.** The BRK-B fix (item covered in `docs/INCIDENT_HISTORY.md`, "QAMC Pipeline Autopsy") stopped one bad symbol from crashing the whole 101-symbol scan, but a symbol Alpaca can't return snapshot data for was still indistinguishable from "this stock just didn't move today" — silently and permanently excluded from every scan, with zero owner visibility. New `intraday_symbol_health` table now counts consecutive misses per symbol (independent per symbol, reset on any successful tick) and fires a standalone owner alert at 3 consecutive misses (~90 minutes), re-alerting at most once every 24 hours while the symbol stays broken rather than paging every 30-minute tick for an already-known problem. Full derivation and test coverage: `docs/INCIDENT_HISTORY.md`, 2026-09-10.

**42. Order-fill detection was a fixed-interval REST poll from 1992, not the real-time mechanism Alpaca actually offers — REPLACED 2026-09-10.** `wait_for_order_terminal` asked "has this order filled yet?" once a second in a loop for up to a fixed timeout — the timeout had already been raised twice (15s -> 30s) after real trades (OXY, NVDA) were cancelled unfilled while still working. The owner's direct challenge — "I doubt the majority of people using this API just set up a simple timer like it's 1992" — was correct and took one documentation search to confirm: Alpaca's own docs recommend its real-time `trade_updates` websocket for exactly this, specifically instead of polling. Now: the stream is watched first and a fill/cancel/reject is detected the instant Alpaca reports it (no more guessing a wait duration for the common case); REST polling remains as the fallback ONLY if the stream itself cannot connect at all, preserving the old reliability guarantee. The timeout constant (`_ENTRY_FILL_TIMEOUT_S`) still exists but is now purely that fallback's ceiling, raised to the originally-researched 90s since a generous fallback now costs nothing. **New standing principle recorded because this shape will recur:** `docs/OUTCOME.md`, "Check what the platform already solved, before tuning your own workaround" — before adding a timeout/retry/poll around any third-party API, check whether that API's own docs already describe the real mechanism. Full derivation and test coverage: `docs/INCIDENT_HISTORY.md`, 2026-09-10.

**43. Smart-money evidence was gated by calendar age instead of correlation with current evidence — REDESIGNED 2026-09-11, owner call.** A stale insider/congressional trade (>7 days) was force-relabeled "historical," an island judged only on its own age. Owner's framing: one weighted piece of a bigger picture — no correlation, it just changes the decision matrix; real correlation, stronger weight. Real research backs dropping the age gate: Seyhun (1986) found ~1/4 of an insider purchase's return realizes in 5 days, ~1/2 still unrealized after a month; real M&A run-ups start months early. `support_eligible` is now purely STRUCTURAL; whether it supports a target is decided by the PM's grounding validator finding at least one other current source agreeing on direction — same test used everywhere else. Fetch window widened 7 -> 90 days (matches `EARNINGS_STANCE_MAX_AGE_DAYS`, not a new number). Detail: `docs/INCIDENT_HISTORY.md`, 2026-09-11.

**44. "Correlation breach" is an accepted exit reason that nothing can verify — OPEN, found 2026-09-11 by audit.** The hard-trigger phrase gate (`src/pipeline.py:250`) accepts `"correlation breach"`, and `exit_guard.py:299` classifies it as a claim of external information. Nothing anywhere computes a correlation-breach EVENT. `correlation_clusters` (`src/data/correlation.py`) measures |r| >= 0.7 clusters at decision time; it does not detect a break. So after PR #299 wired `holding_discipline_claim_check` into the midday/close executor, regime-flip and bearish-state-change claims are adjudicated against real data and this one still returns unverifiable by construction — which by design passes. Net: the phrase remains a free exit from a protected position. NOT a wiring defect; the thing to check against does not exist. Building it means defining what a breach IS (which correlation, over what window, versus what baseline) — every one of those is a number, so `qamc-no-arbitrary-numbers` applies and this is an owner call, not an implementation task.

**45. Two pivot windows disagree about what a "swing low" is — OPEN, found 2026-09-11 by audit.** `src/risk/trailing.py:103` sets `PIVOT_WINDOW = 3` and its comment claims it matches `src/data/levels.py` "so 'a higher low' means the same thing in both places". `src/data/levels.py:39` sets `PIVOT_WINDOW = 5`. Verified by direct read. Consequence: the trailing-stop ratchet can see a higher low the structural-level detector does not, and vice versa. Neither number carries a derivation. Do not "fix" by copying one onto the other — picking 3 or 5 is picking a number.

**46. `level_match_atr_tolerance` fails its own stated justification — OPEN, found 2026-09-11 by audit.** `config/settings.yaml:623` sets `0.25` and justifies it as "at least" the 1% level-cluster zone width. At this book's own stated median ATR of 2.56%, 0.25 ATR is 0.64% — under the 1% it claims to cover, by ~1.6x. Governs whether a stop counts as level-backed and is therefore exempt from the ATR stop floor, so it feeds directly into item 1's geometry. Either the tolerance or the justification is wrong; both are numbers, so owner call.

**47. `risk.max_position_pct`'s cash-only justification was already false when written — RESOLVED 2026-09-11, owner call.** 100's comment asserted `allow_margin: false` made >100% notional unreachable; `allow_margin` had been `true` since 2026-09-02, two days before that comment (PR #258, 2026-09-04). Derived replacement: 20 (ladder emergency rung) / 0.60 (median of 5 real dated single-session idiosyncratic collapses) = 33, the largest single bet where that median disaster stays under the ladder's -20% owner-alert rung. Owner reviewed and set **65** instead — his own risk-appetite call, not a data disagreement; rejected 33 as too tight for a desk that avoids penny/micro-cap names, correction on the table first (the 5 reference disasters were all liquid, non-penny names). At 65 the same median disaster costs ~39%, past the alert rung rather than under it — knowing trade-off, not an oversight. `max_position_pct: 65` in settings.yaml, `ConstructorConfig.max_position_pct` and `pipeline.py`'s wiring default kept in sync. Detail: `docs/INCIDENT_HISTORY.md`, 2026-09-11.

---

### Re-measure gate — TWO different questions, two different costs

**No item above may be struck through on a code review.** But do not confuse
the fast question with the slow one. Owner's correction, 2026-09-02, and it
is right: *"we only need maybe a couple of runs in the morning to determine
if it's still not pulling the trigger on longs or shorts for no reason."*

**THE FAST CHECK — one or two sessions. Use this for item 1.**
"Is the desk still refusing trades for no good reason?" is BINARY and it is
answerable the next morning the market is open. A session either produces
entry proposals that survive to an order, or it kills them at the R/R gate
again. One clean morning tells you whether the geometry fix worked; two tells
you it was not a fluke. Do not wait two weeks to learn this.
  - What to look at: the proposals that session, and for each one whether the
    R/R gate blocked or halved it. If the gate is still doing the killing,
    the fix did not work — that is the whole test.
  - **A zero-proposal session does NOT answer this**, it only means nothing
    got that far. Distinguish "refused" from "never offered", or you will
    read item 11's silent-failure mode as a pass.

**THE SLOW CHECK — weeks. Use this for the funnel percentages.**
Re-running `scripts/blocked_proposals_census.py` and claiming the SHARES
moved (25% → x%) needs a comparable window, and the book was wiped
2026-09-02 so that window starts from empty. Until roughly two weeks of
sessions exist, any restated percentage is an estimate. Say so when making
one. This is a bookkeeping bar, not a gate on shipping the next fix.


**~~A held SHORT makes its sector look SMALLER to the risk engine~~ — DECIDED
AND BUILT 2026-09-01.** Long and short sector exposure are now tracked
independently, each against the same limit, per spec §12.2 (build record in
`docs/QAMC_REMEDIATION_SPEC.md` §12.2). Full decision record moved to
`docs/INCIDENT_HISTORY.md`, 2026-09-11.

Shipped alongside it, spec §12.3: the sector limit moved **40% → 75%**, with
the absolute ceiling at 90%. **The 90 is not owner-ratified** — it was chosen
at build time because 1.5x a 75 target gives a meaningless 112.5 — and is open
for the owner to move.


**Margin interest tracker — REQUIRED before Phase 11.2's margin goes on.**
Owner asked for this directly on 2026-09-01: he wants to see, cumulatively,
what leverage actually costs, "to see if it's worth it".

The problem it solves: **paper will not teach this lesson by itself.** Alpaca's
own docs confirm paper does NOT simulate short borrow fees (their comparison
table lists it "Coming Soon"), and whether paper simulates MARGIN INTEREST is
not documented either way. So a paper account run at 2x may show all of
leverage's upside and none of its cost — the single most misleading thing this
project could learn.

Build our own, from the broker's own numbers:
- Alpaca live rate **6.25%** non-elite / 4.75% elite, charged as
  `(settlement-date overnight debit balance x rate) / 360`, **on the end-of-day
  balance only — intraday leverage is free.**
- Accrue daily, store it, and report it cumulatively. At a sustained 2x on
  ~$9,800 equity that is ~$1.71/day, ~$614/yr — **6.25% of equity the book must
  out-earn before leverage contributes anything.**
- **Settle the open question empirically on the first night a debit balance is
  carried:** check the account's `INT` activities. If Alpaca posts a real
  charge, use theirs and stop estimating. If nothing appears, keep our estimate
  and LABEL IT AS AN ESTIMATE everywhere it is shown.
- Surface in the morning Telegram alert and on the dashboard, alongside
  overnight gross exposure.

**Status 2026-09-01: PARTIALLY SHIPPED.** `src/margin_interest.py` +
wiring gives a live, read-time ESTIMATE — correct formula, correct
end-of-day-debit-only balance, labelled `ESTIMATE` on every surface it
reaches, plus the broker-`INT`-activity empirical check. Two spec bullets
above are NOT done: it is not accrued daily into storage and reported as a
**cumulative** total (every number is today's snapshot, recomputed fresh,
nothing persisted); and it reaches the morning Telegram alert and
`GET /account` only — no dashboard UI surfaces it yet, so "on the
dashboard" is still open. Both remain before this item can be called
complete.



**Next, in order (set 2026-08-30) — start here**

Two owner decisions ratified 2026-08-30 (inverse ETFs stay tradeable; paid
news sources refused permanently) moved to `docs/INCIDENT_HISTORY.md`,
2026-09-11.


Single ordered list of outstanding work. A session resuming cold should start
here. Items are ordered by dependency first, then by value per unit of effort.
Rationale for the trading items is in `docs/QAMC_REMEDIATION_SPEC.md`; evidence
for the analyst items is in `docs/AGENT_ROLE_AUDIT.md` and
`docs/RESEARCH_FINDINGS.md`.

**Landed (2026-08-27)** — cut 2026-08-31 to stay under the size cap that
`tests/test_status_board.py::test_work_md_stays_under_a_hundred_thousand_bytes`
enforces. It was ~21KB of PR-by-PR narrative for work merged and deployed four
days earlier: Phase 0 (CI as a real gate), governance and document-authority
tiers, branch hygiene 110->27, Phases 1/1b/2a/2b/3 and the Form 4 insider work.
Every item is recorded with evidence and re-check commands in
`docs/phases.yaml` — the authority for what shipped. The ratified owner
decisions from that day are live, not finished, and stay below.

**Owner decisions, 2026-08-27 (ratified in session, not inferred)**

- **Phase 3-before-Phase-2 reorder, and the §3.5 reviewer-model question** —
  both resolved this day, moved to `docs/INCIDENT_HISTORY.md`, 2026-09-11.
- **Standing autonomy grant.** The owner instructed that work should not halt
  at phase gates for approval. Proceed through this backlog — implement, test,
  PR, merge, deploy to PAPER, verify — and interrupt only for something
  unusually significant: live-capital activation, new paid dependencies,
  secrets/credential redesign, destructive infrastructure, material
  architecture outside current authority, or evidence that a ratified decision
  was wrong.
- **Market data stays on IEX; SIP is NOT authorized.** Alpaca's Algo Trader
  Plus (~$99/mo) would give consolidated NBBO quotes. Verified 2026-08-27 that
  this account is IEX-only (a SIP request returns "subscription does not
  permit querying recent SIP data") and that IEX top-of-book is frequently
  unusable — CCJ quoted bid $92.96 / ask $107.10, a 15% spread, mid-session —
  while Alpaca fills against NBBO. Rex's decision: *"this is paper trading,
  this is proof of concept. Slippage is not really a big concern... when we
  move to real money that's something to look at again."* **Revisit before any
  live-capital activation** — execution-quality numbers measured under IEX are
  not trustworthy.
- **Fractional shares are IN — reversed back in and built 2026-09-01**
  (spec §11.1), behind `execution.fractional_enabled` (default on) — moved to
  `docs/INCIDENT_HISTORY.md`, 2026-09-11.
- **The desk must deliberate, not just filter** — see `Phase 9` in
  `docs/QAMC_REMEDIATION_SPEC.md`. Rex: *"We have agents doing research and
  analysis. If something has high conviction or strong candidacy it should be
  debated amongst all the agents. We're trying to create a trading desk with
  synergy, not a technical analysis trade bot."* Sequenced AFTER Phase 2b.
- **The $1.50/day LLM budget is not a hard boundary.** Rex, 2026-08-27:
  *"There is significant flexibility on the daily budget if the cost benefit
  makes sense... throwing money at something is not the solution, it has to be
  carefully weighed cost benefit. Also there are clever ways of solving
  problems that don't always require more money."* Raised to **$2.75/day** on
  the live box the same day (`llm_cost_circuit.daily_cost_limit_usd`, with
  `daily_reserved_exposure_limit_usd` 1.90 -> 3.20) because a single
  `intra_check` had consumed $0.43 of a $1.50 day by 10:02 ET and a second
  would have starved the midday/close/evening sessions that carry every
  Phase 3 exit fix. **Rebalance before increasing further** — see the measured
  breakdown below.
- **The per-trade risk ceiling is 5% of equity, confirmed.** Phase 2b raises it
  from the constructor's current `risk_budget_pct = 0.5` default — a tenfold
  increase in per-trade risk (~$50 → ~$500 at risk on a $9.9k book). The owner
  confirmed 5% is the ratified envelope and that the 0.5% figure was a
  constructor default nobody chose. Floor stays 0.5%; total stays 25%,
  correlation-adjusted. **This envelope has since been implemented** as Phase
  2b (`75c0233`, `feat/pm-flex-routing`, merged and deployed) — see the
  landed section above.

**Measured LLM spend (10 days to 2026-08-27) — read before proposing any budget change**

**These figures predate the flex-routing and intra_check fixes below** (both
merged and deployed as part of `feat/pm-flex-routing` / PR #113) — they are the
baseline those changes were made against, not current production numbers.

$6.73 total across 48 sessions. **$5.84 of it is the Portfolio Manager: 87%.**
**This window is contaminated and is not a clean baseline.** It includes
several runaway looping incidents that burned tokens — the reason the LLM
cost circuit breaker was added — so the per-seat shares below are inflated
by an unknown amount and should not be read as the PM's steady-state share
of spend. A clean baseline needed re-measuring once the current tranche (flex
routing, the `intra_check` fix) was deployed. **It has been — see immediately
below; use those numbers, not these.**

**CLEAN BASELINE, measured 2026-09-02 from `llm_budget_days` and `agent_logs`
on the live desk.** Dates used: **2026-08-27 to 2026-09-02**, which is the
whole post-contamination record. 2026-08-31 is included but its ledger is only
meaningful after the operator's phantom-charge correction that afternoon;
2026-09-02 is a partial day (through 14:01 ET). 2026-08-29/30 was a weekend.

| Day | Settled spend | Note |
|---|---:|---|
| 2026-08-27 | $1.0200 | |
| 2026-08-28 | $0.7394 | |
| 2026-08-31 | $1.4578 | includes three operator-triggered morning re-runs |
| 2026-09-01 | $0.7793 | |
| 2026-09-02 | $0.8958 | partial, to 14:01 ET |

**~$0.73–$1.14 on an ordinary day against a $2.75 ceiling — the desk is using
about a third of its budget.** No trend: the range is flat across the five
days and the variation is run-count, not per-call drift.

**The concentration is confirmed, and it got MORE concentrated, not less.**
Over 2026-08-27..09-02: **portfolio_manager is $4.2475 of $4.5646 — 93.0% of
spend on 35 of 153 calls.** Every other seat combined is $0.3171. The eight
seats that moved to Google-direct `gemini-3.5-flash-lite` on 2026-08-31 now
cost **$0.00** (free tier) — 35 calls, zero dollars. So the *entire* remaining
LLM bill is one seat's model choice plus two small OpenRouter stragglers
(`tech_analyst` $0.209 pre-migration, `risk_manager` $0.023).

**Do not re-litigate "the research desk is cheap" from this.** It is now
cheap because it is free, which is a routing fact, not an efficiency one. The
only lever that moves the bill is the PM seat: its input size, its cadence, or
its endpoint. A weaker PM model is ruled out by the owner and that ruling
stands.

**One measurement to act on before any budget change.** Provider-reported cost
for `openai/gpt-5.5` has been a **median 0.38x** of the pinned $5/$30 estimate
across the 32 calls since 2026-08-28 (the flex endpoint plus cache reads).
Reservations and the reserved-exposure ceilings are still sized off the pinned
rate, so every PM reservation is ~2.7x what the seat is actually billed. That
is what produced the 2026-08-28 hold at $1.9118 on a call that cost ~$0.25,
and the response was to raise the ceiling 1.80 -> 2.60. **The ceiling is being
loosened to accommodate a bad estimate.** Pricing reservations at the flex
rate is NOT the fix — fallbacks are enabled, so a saturated flex tier lands on
the $5/$30 endpoint and the reservation would then under-cover the dearest
possible outcome. Owner decision, not a patch.

| Mode | Runs | Avg/run | Dominated by |
|---|---:|---:|---|
| `morning` | 20 | $0.221 | PM $3.65 (83% of the mode) |
| `intra_check` | 10 | **$0.222** | **PM $2.19 (99% of the mode)** |
| `evening` | 7 | $0.004 | evening + news |
| `close` | 7 | $0.003 | news + position_reviewer |
| `earnings_preprocess` | 4 | $0.004 | earnings |
| `midday` | 7 | $0.003 | news + position_reviewer |

Two facts worth acting on:

1. **`intra_check` cost the same as a full morning run** ($0.222 vs $0.221)
   while doing almost none of the work — $0.003/run on research, $0.219 on the
   PM call — and it was also the session the PM was *deliberately blindfolded*
   in (`portfolio_manager.py` returned a technical-only evidence registry when
   `session_type == "intra_check"`, though macro and news were already in
   memory). 33% of all spend, on blindfolded scanning. **Fixed** — `fb88e08`
   carries the morning's macro/news forward instead; nothing is re-fetched, so
   the per-run cost this table shows should not change materially, but the PM
   is no longer deciding on a technical-only slice of the evidence.
2. **The whole research desk costs 5.5%.** Technical — the *only* source of
   trade discovery today — is 4.6% of spend. The inference that "the system
   pays 87% to arbitrate a shortlist produced by its cheapest component, so
   fix the allocation before raising the ceiling" rests on the contaminated
   window above and is **not established** — it may still be roughly true,
   but it cannot be asserted as a finding until it's checked against a clean
   measurement. `16f6535` routes the PM's `openai/gpt-5.5` calls through
   OpenRouter's `openai/flex` endpoint at half the per-token price (same
   model weights), so the PM's per-run cost should roughly halve (~$0.22 →
   ~$0.11 on `morning`, ~$0.22 → ~$0.11 on `intra_check`) once this merges
   and deploys — that decision is correct regardless of the PM's exact share,
   since it's the same model at half price. Whether the research desk still
   needs to cost more of the total is a separate question that a clean
   baseline, not this one, has to answer.

1. **Execution: bounded re-peg — BUILT, SHIPPED DARK, AND MOSTLY INERT, merged
   from `feat/bounded-repeg` (PR #144 opened against `main`, 2026-08-29).**
   `execution.repeg_enabled` (default **false**), `repeg_max_attempts`
   (default 2, schema-capped at 5), the `pending_repegs` write-ahead queue and
   its session-start drain, `broker.replace_entry_limit` /
   `resolve_replacement_chain`, and
   `place_entry_protection(superseded_filled_qty=...)` so a fill that landed
   under a superseded order id still gets a stop. A partially filled order is
   NEVER replaced (that is how one idea gets bought twice), a rejected
   replacement means the order filled and the chase stops, and every
   ambiguous branch leaves the order working.

   **Read this before enabling it.** Since PR #111 a BUY limit is submitted
   AT the slippage ceiling whenever a quote is available, so there is nothing
   to walk toward and the re-peg is a no-op by construction for those
   entries. It only has room where the limit was set BELOW the ceiling —
   today that means the quote was unavailable at submission and the analyst's
   entry price was used. Turning the flag on will therefore do approximately
   nothing until the entry pricing policy changes. Deciding whether entries
   should peg tighter than the ceiling (and then be walked up) is a policy
   question that reverses part of #111's reasoning and was deliberately NOT
   taken here.
2. **Lazy Prices 10-K year-over-year diff.** Text similarity only, no model. The
   filings are already downloaded and stored.

3. **Phase 7 — measurement.** Backtester and conviction calibration. Must enforce
   post-training-cutoff evaluation windows for any LLM signal — contamination is
   the dominant failure mode in this literature. **Correction 2026-08-29: the
   backtester landed; conviction calibration (whether the AI's stated
   conviction predicts outcome) did not** — see "Landed (2026-08-29)" in
   `docs/INCIDENT_HISTORY.md`.
4. **Analyst upgrades.** News cascade — **stage 1 (dedup) is DONE**
   (`src/data/news_dedup.py`); stage 2 (novelty scoring against a rolling
   48–72h per-ticker buffer) and stage 3 (a model on the residual only) remain,
   and the seam for them is `NewsCluster.novelty`. Also: deterministic macro
   regime with the model confined to FOMC text; earnings multi-quarter trends.
   Several need new data sources and an owner decision first.

   Note for whoever picks up stage 2: the measured duplication rate is small.
   Across 589 archived articles the old stage removed 4.2% and the new one
   removes a further 1.2% — so dedup is a correctness fix (it stops one story
   reading as N confirmations), **not** a cost saving. Two of nine feeds
   (Reuters, AP) are dead, which suppresses exactly the wire-syndication case
   dedup targets; the true rate is unknown until those are fixed.

**Identified 2026-08-28, not yet fixed**

**Found and reported, NOT fixed here (out of this task's scope, real defects/gaps for someone to pick up):**

- **`NewsIntelligenceReport.market_sentiment` (`src/models.py`) rejects a value the real news analyst LLM uses regularly, and silently discards the entire news report when it does.** It's `Literal["bullish","bearish","neutral"]` with only case normalization, no vocabulary mapping. Measured against the full retained `agent_logs` history: **9 of 54 real news_analyst responses (16.7%) used a non-enum value** — `mixed` (7x), `mixed-to-bearish` (1x), `risk-off` (1x). Each one makes `NewsAnalystAgent.analyze()`'s `NewsIntelligenceReport(**parsed)` raise, caught generically, discarding macro_narrative, pm_briefing, state_changes and stock_news for that entire session — even though the same method has explicit, deliberate per-entry isolation for `state_changes` and `stock_news` specifically so one bad entry can't take down the whole report (see its own docstring). That protection was never extended to the top-level `market_sentiment`/`confidence` fields. Roughly one in six real news-analyst sessions in the retained history lost its entire news product to this.
- **The rig cannot rehearse `earnings_preprocess` at all.** `ops/rehearsal/runner.py`'s `SESSIONS` mapping and `run.py`'s `--session` choices list only `morning`/`midday`/`close`/`evening`/`intra_check`. `TradingPipeline.run_earnings_preprocess()` is a real, scheduled (08:00 ET), LLM-calling session — the *only* place 10-Q/10-K filings get analyzed — with the same structural shape (RunContext, trading-day gate, cost-session activation, protection-restore drain) as the five modes the rig supports. This is undocumented anywhere in the rig's code, tests or docs; it appears to be an oversight, not a deliberate scope cut. Consequence: a defect specific to earnings preprocessing — like the earnings-extraction bug fixed in PR #115 the same week — would be invisible to this harness.
- **A failed broker read cannot be rehearsed.** `RehearsalTradingClient`/`RehearsalDataClient` never raise on `get_account()`/`get_positions()`; only `get_asset()` and `close_position()` are wired to fail. Production's `except Exception: return {"status": "broker_error", ...}` path in `run_morning`/`run_intra_check` is therefore completely untested by this harness. Consistent with the module's own documented scope, but worth naming since "a broker read that fails" is exactly the kind of resilience case this rig should be able to exercise.

Two rehearsal-rig behaviors checked and confirmed as non-defects (offline
`--source-data`-less runs fail closed at the pricing gate; a flattened
sandbox book correctly makes the reconciler decline to guess) moved to
`docs/INCIDENT_HISTORY.md`, 2026-09-11.

#### SMALLER, RECORDED
- OneCLI: OpenRouter spend from a live rehearsal would be real money on the same account, but the rehearsal runs its own cost-circuit database, so production would under-count the true daily bill.
- OneCLI: production's Alpaca secret matches `*.alpaca.markets`, which also covers the paper host, so both credential sets match the same address. The gateway fails closed on the ambiguity. Narrowing the production pattern risks breaking live credential resolution and was deliberately left for the owner.

- `feat/news-dedup` — still unmerged; disposition being decided separately.

#### THE NEW STOP RULE REJECTED FOUR BUYS ON ITS FIRST DAY — RESOLVED 2026-09-01
Moved to `docs/INCIDENT_HISTORY.md` (2026-08-28 four-candidate reward:risk
rejection; answer was that the targets were never real measurements).

#### RECURSION FAULT IN THE BAR FETCH
`broker.get_bars failed for DSPC: maximum recursion depth exceeded` — 14 times on 2026-08-28, all for the same symbol. Contained (the call returns an empty list rather than crashing the session) but it is a real fault, not noise. DSPC is a delisted warrant, so the trigger appears to be the fallback path handling a symbol with no data.

#### DELISTED WARRANTS REACHING THE DATA LAYER
Five symbols returned "possibly delisted; no price data found" on 2026-08-28: DSPC, SXTPW, NRSNW, LIMNW, ERNAW. All are warrants. They should not be reaching a bar fetch at all — this is universe/admission hygiene, and it is also what triggers the recursion fault above.

**Set aside — small, easily forgotten**

- `db_reads.get_recent_agent_logs` uses `SELECT *`, and `GET /agents/{agent_name}`
  returns 20 rows as `recent_calls`. PM prompts run 13KB-190KB, so that response
  could reach several MB. Harmless today because nothing in `frontend/src/`
  calls the route — fix before anything does, by trimming the large columns
  from the LIST query and keeping them on the detail route.
- `docs/architecture/MODEL_ROUTING_POLICY.md` carries a token-count figure that
  is stale since the Tech Analyst prompt grew. Annotated with a measured
  estimate; not re-derived with `ops/model_policy/project_session_cost.py`.
- Re-examine whether the LLM Risk Manager seat is additive once the drawdown gate
  is deterministic — see `docs/AGENT_ROLE_AUDIT.md`.

### Natural Alpaca Paper validation

Natural validation continues in parallel. The substantive acceptance item remains evidence that QAMC behaves coherently in ordinary Alpaca Paper markets:

**opportunity discovered → evaluated → defensible bullish/bearish/neutral decision → executed when eligible → managed/exited → measured**.

Success is not a target number of trades. Do not manufacture opportunities, force orders, weaken risk controls, or hindsight-tune the system to create evidence.

Use the existing Mission Control, journal and Telegram read-side evidence to determine:

- what opportunity was discovered;
- what the specialists, Portfolio Manager and AI Risk Manager concluded;
- what deterministic risk/funding/execution did;
- why an eligible trade did or did not execute;
- how any resulting position was managed/exited;
- what the measured result and missed-opportunity evidence show.

When QAMC does not trade, the reason should be specific and defensible rather than an unexplained absence of activity.

### Future — portfolio hedging (not scheduled, do not start yet)

Owner asked on 2026-09-01 for this to be written down so it is not lost. It is
NOT approved work.

**Prerequisite: the desk must be stable and actually trading first.** On
2026-09-01 it found 38 actionable signals and placed zero orders. Hedging a
book that will not trade is the wrong problem in the wrong order.

**No hedging logic exists anywhere in QAMC.** Verified 2026-09-01 — every
occurrence of "hedge" in `src/` and `config/prompts/` is incidental prose or
the `is_bearish_hedge` flag, which labels a bearish idea and sizes nothing.

**Why the inverse ETFs stay** (owner reconfirmed 2026-09-01, "that could be
utilized as a hedge"): none of `SH`/`SDS`/`PSQ`/`SQQQ` is shortable at the
broker, so they are only usable as longs — exactly what a hedge sleeve wants —
and shorting has never once executed (45 trades in the ledger, zero
`SHORT`/`COVER`). They are the only bearish tool that has ever worked. Revisit
removing them only after a real short fills and exits cleanly.

**Options, cheapest first, none decided:** an index hedge sleeve sized off
measured book beta; net long-minus-short as an explicit sized output of the
macro read (the natural continuation of spec Phase 10.2); hedging the dominant
correlation cluster using the measurement `src/data/correlation.py` already
computes for the cluster risk budget; bounded protection into a known binary
event (FOMC/CPI/earnings), whose calendars already exist.

**Traps to design around:** a hedge is a position, not an exemption — it
carries a stop and appears in exposure maths like anything else. Inverse and
leveraged ETFs decay through daily rebalancing, so any sleeve needs a maximum
holding period. Under spec Phase 11.2's margin cap a hedge consumes gross
exposure, competing with what it protects — decide deliberately whether it
counts. And do not let a hedge become a way to avoid selling a loser; that is
the failure mode this invites.

## Evidence-only follow-ups

- news-narrative factual drift;
- `actual_provider` attribution oddity.

Do not interrupt natural validation for these unless current evidence shows they materially distort decision quality, truthfulness, or operator understanding.

`get_latest_price` is **not** on this list solely because its request omits `feed`; that concern has been reconciled. Reopen only on concrete production evidence.

- invalid_if: 9/3 log.

