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
  **STRUCK 2026-09-14 — this paragraph used to say "the blocking dependency
  is a benchmark re-run … therefore an owner call to authorise", and that
  framing is stale and was actively harmful.** It invited exactly the
  proposal he had already refused: on 2026-09-13 he ruled that no model test
  runs while any PM-gate item is open, and the recommendation to spend ~$5
  and settle it was put to him once already and was wrong. The spend is of
  course his money and nobody may spend it without him — that is not in
  question and does not need restating as a pending "authorisation" he owes
  anyone. **Nobody proposes this run to him. He raises it or it does not
  happen.** Weight it against the fact that the
  `portfolio_manager` seat is ~93% of the LLM bill, so this is also the
  largest available saving. See `qamc-llm-cost-concentration`.
  **NOT DECIDED, and it is a FIRST run — not a re-run. Corrected 2026-09-14
  after checking the stored results rather than repeating the wording.**
  There has never been a PM model comparison on the measurement that matters.
  Multi-model runs exist only on the hand-built `pm_constrained` scenario,
  which is saturated (nearly every model scores full marks, so it separates
  nothing), and on `pm_production_scale`, whose own README says it cannot
  measure stock-picking because every candidate gets an identical analysis.
  `pm_selection` — the only scenario built from a real trading day — has only
  ever been run against ONE model. Re-scoring the stored outputs instead is
  not available: they are truncated to the first 1,500 characters and 3 of 8
  trials carry nothing at all. **What actually invalidated the old numbers**
  is prompt churn (25 commits have touched
  `config/prompts/portfolio_manager.md` since 2026-09-01, and it is roughly a
  fifth longer) plus a grader keyed to the retired reward:risk
  floor — the second of those is FIXED as of 2026-09-14, gate item 8. The
  incumbent `openai/gpt-5.5` stays until a first run exists.

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

**7. PM-input shape/volume redesign — NOT A GATE ON THE MODEL TEST (corrected 2026-09-14). The volume half is DONE and re-measured; what is left is not volume.** **Why it is not a gate:** a model comparison shows every candidate model the IDENTICAL frozen input, macro block included, so the size or shape of that block cannot change which model wins. It is a one-model prompt question, not a between-model one. The line that once said this needed "the paid `--replay-run` benchmark" named something that does not exist: `--replay-run` is a flag on the REHEARSAL RIG (`scripts/rehearse.py`), and `ops/model_policy/benchmark_models.py` has no such flag. A prompt change needs a benchmark run on the same scenario before and after, which is not the owner's to authorise. **Measured:** the null-content slice shipped 2026-09-13 (100,968 chars over 25 sections, of which 22,094 = 21.9% were content-free; the "70%" of item 18 was exactly 70.4%; per-section table in `docs/INCIDENT_HISTORY.md`, "item 18d"). **Re-measured from scratch 2026-09-14 on origin/main before any change: 87,016 chars, and all four filler markers now occur zero times** — the +1,083 against the 85,933 then recorded is Candidate Ranking growing 8,753→9,836 as item 10's per-drop reasons land, i.e. text that explains an outcome, not filler. **The macro audit hook, evidence recorded so it is not lost:** across 56 archived `portfolio_manager` calls, 27 carried macro's full `reasoning_chain` under an instruction to audit it for logic errors, and ZERO responses named that chain or reported a macro logic error. The structural reason was that the PM's output schema had no field such a finding could go in. It now has one (`reasoning_chain.macro_audit`, see item 18 — a PROMPT change resting on that structural argument, NOT on a measured improvement). **Caveat, stated rather than buried:** the archive ends 2026-09-02 and 27 calls is a modest sample — that is "no evidence it works", not proof it cannot. **What is left:** whether the seat actually uses that channel, which only a before/after benchmark run can answer. The two largest remaining sections, Technical Analysis (16,736) and Independent Source Agreement (11,902), are both already bounded and both scale linearly with the number of candidates covered — there is no honest cap to put on either, so the lever is how many names get covered, not how each one renders. Earnings, news and tech all now hand over call + conviction + thesis + falsifier. Do NOT re-open this as a size problem.
**ITEM 8 CLOSED 2026-09-14** (`docs/INCIDENT_HISTORY.md`). Its premise was
false by construction — every benchmark input is frozen on disk and no live
seat is called anywhere in the harness — and the real defect it was standing
in front of, a grader still marking against the retired reward:risk floor, is
fixed. **Item 7 is NOT a gate on the model test** and the claim that it was
has been struck from it: a comparison shows every model the identical frozen
input, so the shape of that input cannot change which model wins. It stays
listed here under its own number because it is genuine open work and the
board reads this section by number.

**None of this authorises running the benchmark, and nobody proposes it to
the owner.** He ruled on 2026-09-13 that no model test runs while any PM-gate
item is open; item 7 is open. **A known limit on what the test would measure,
recorded 2026-09-14 so it is not discovered mid-spend:** the only
real-day scenario, `pm_selection`, runs on a fixture where zero of 59 rows
carry `computed_levels`, so the STRUCTURAL reward:risk the current rule reads
cannot be computed for any name on it. Admission does not depend on that
quantity (verified — nothing is refused by the payoff rule that a neutral
rating does not already refuse), so the scenario still measures selection
against the desk's live admission rules; what it CANNOT measure is whether a
model reads payoff geometry the way the desk now does. A fixture that could
exists in the archive — production `run-bba4d4f3`, 2026-09-02, 63 of 64
analyses carrying computed levels and all 34 actionable candidates with a
computable structural ratio — and capturing it is unstarted work, not a gate.

Detail below, under "DATA QUALITY AUDIT" and "PM-INPUT ARCHITECTURE".

<!-- END PM TEST GATE -->

**DATA QUALITY AUDIT — 2026-09-02, owner priority: this pillar (garbage in,
garbage out) must work before anything else.**

Owner's instruction: every one of the 5-6 shared analyst seats (tech, news,
macro, earnings, smart_money, evening) has data-quality issues — empty
fields, silent death, or empty data passed to the PM as if it were real.
Audited from real production logs, not assumed. Ranked by measured severity:

(Items 1-6's full write-ups already live in `docs/INCIDENT_HISTORY.md`;
see the struck-through index above for status.)


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

**The 2026-09-01 end-of-session state (Phase 12.1-12.3 ship record, the
`allow_margin` flip sequencing, the 90%-sector-ceiling gate, and the deploy
benchmark run) moved to `docs/INCIDENT_HISTORY.md`, 2026-09-14 — finished and
superseded (the ladder test it asked for now passes in the live suite;
`allow_margin` has been `true` since 2026-09-02) and moved to stay under this
file's byte cap, not deleted.**

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
- **Never hand-resolve a conflict in `docs/WORK.md`, `docs/BOARD_NOTES.md` or
  `docs/INCIDENT_HISTORY.md`.** These three files are wired to
  `scripts/resolve_doc_conflict.py` as a git merge driver (`.gitattributes` +
  `scripts/git_merge_driver_docs.sh`), so a normal `git merge`/`git rebase`
  runs it automatically once the clone has registered the driver — the
  one-time, per-clone `git config` command is in README.md "### Install".
  **A clone that skips that command is unaffected**, not silently unsafe: git
  falls back to its own ordinary conflicted merge (real conflict markers, a
  human resolves by hand) for these three files exactly as it would for any
  other file, because a merge driver named in `.gitattributes` with no
  matching `merge.<name>.driver` configured is simply not used. If a merge on
  these files ever surfaces plain conflict markers instead of a clean
  auto-resolve or a `REFUSING TO WRITE` message, that means the driver is not
  registered in this clone — check `git config --get merge.docsmerge.driver`
  before resolving by hand. The resolver merges numbered ITEMS rather than
  blocks of text, refuses to write unless every item on either side survives
  exactly once in its own list, and stops for a human on a number collision —
  which is a renumber, never a delete. Refusing is a correct outcome;
  resolving it by hand instead is how five live items were deleted (item 68,
  closed 2026-09-14, `docs/INCIDENT_HISTORY.md`). The explicit CLI
  (`--from-index`, or `--kind`/`--base`/`--ours`/`--theirs`/`--out`) still
  works directly, for a merge run somewhere the driver isn't configured, or
  for a dry run.
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

**17. Backup alert channel — OWNER DECISION, deferred, no due date.** The only open point: there is no second notification channel beyond Telegram, so an alert that cannot reach Telegram reaches nobody. A new dependency/design tradeoff, not a retry-count choice. Decision and recommendation: `docs/BOARD_NOTES.md` ("item 17"). Everything else on this item shipped 2026-09-03 and was verified running on the box 2026-09-13 — `docs/INCIDENT_HISTORY.md` ("item 17(a)/(b)", and 2026-09-13).

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
    not decided, not an owner call (label removed 2026-09-14, see
    `docs/INCIDENT_HISTORY.md`). Sizing-path parity is settled and no
    longer an item: retired items 30/57 derived the sizing ladder from
    the ratified envelope, and per-seat WEIGHTS there stay refused.
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

**20. GATE THE DECISION ON EVIDENCE COVERAGE — owner's design, 2026-09-02.
PARTLY BUILT 2026-09-14. Do not trade on partial evidence.**

**Owner's ruling, and it overrides my weaker proposal.** I suggested letting
the run continue with reduced coverage and warning the PM which inputs were
hollow. That is wrong: *"the decision matrix is flawed — it's asking it to
make a decision when it doesn't have enough information to make an informed
logical decision."* A decision on incomplete evidence is not a degraded
decision, it is a fabricated one.

**BUILT (2026-09-14) — the categorical half, which needs no number.** The
morning session now refuses to reach the Portfolio Manager at all when a
seat's answer was LOST, and that word is doing precise work. `src/evidence_gate.py`
sorts every value any seat writes into `data_status` into one of three
categories: a usable answer arrived; the seat answered and the honest answer
is empty (no Form 4 filings today is a fact, not a gap); or the answer was
LOST — the call raised, the response would not parse, the provider failed,
the generation was cut off, or every analyzed filing came back with no
content. Only the third refuses the run. That line is categorical, so it
needs no threshold, cannot drift, and cannot be fitted to the desk's own
history. It reads the statuses the seats already self-report and adds no
second notion of coverage. A test enumerates the vocabulary, so a seat that
gains a new status word must classify it in the same diff rather than
silently widening or narrowing the gate.

The skip is loud three independent ways: its own owner alert, the existing
standalone `maybe_alert_data_quality` page off the `data_status` in the
result, and a session status classified as a warning rather than a quiet day.
It drops no candidate and emits no target — it returns before any target
exists, so it cannot produce the 0%-target-means-SELL shape. Every symbol
that reached a technical read gets its own durable, machine-readable row
saying why the desk never decided on it.

**MEASURED BITE, because a refusal gate whose bite is unknown must not
ship.** Replayed against the desk's own `agent_logs` + `specialist_evidence`:
**5 of the 27 historical morning runs that actually reached the Portfolio
Manager (19%) would have been refused**, on 4 of 13 trading days. Four are
one recurring upstream bug — the news analyst returning non-JSON — on 08-17
(twice), 08-18, 08-25, and again in two saved parse-failure records dated
2026-09-04. The fifth is the smart-money SEC provider failing with no
findings on 08-26. The bite is dominated by ONE fixable seat; fixing it is
not this item, but expect the gate to fire until it is.

**STILL OPEN — question 1, the counting half, and it is yours, not an
agent's.** Design (a) also asked for "how many earnings reports are usable,
how many technical reads survived validation" — a count, compared against a
minimum. That minimum was NOT invented and NOT shipped, per your own ruling
below. What was searched: no published source states how many of five
research seats a discretionary equity desk needs before a decision is sound,
and the desk's own trade history cannot supply one without fitting a number
to it, which this document forbids outright. What would settle it: your
ratified number, or a decision that partial coverage should never gate at
all and the categorical rule above is the whole of item 20.

**STILL OPEN — question 2, found while building this.** The intraday scan
writes `not_run_intraday` BOTH when it chose not to re-fetch a seat AND when
the carry-forward was empty because this morning's seat failed. "Not asked"
and "asked and lost" wear one word there, so the gate is deliberately NOT
applied to the intraday path — it would be classifying a state the data
cannot distinguish. Splitting that value in `_carry_forward_macro` /
`_carry_forward_news` is a small, separate change.

**CORRECTION to this item's own premise, verified 2026-09-14.** It says a
skipped run "costs half an hour, not a day" because `intra_check` retries
every 30 minutes. `run_intra_check` re-runs neither research nor the PM. The
only thing that can decide again is the intraday scan inside it, which looks
only at symbols that moved >=3% since the last close, capped at 5. On a quiet
day a refused morning is closer to a lost day. That does not change the
ruling — a fabricated decision is worse than none — but a refusal costs more
than this item assumed.

**The signal already existed and is now read, not rebuilt.** The earnings
`data quality` line the agents were already writing is extracted as a status
(`_classify_earnings_status` → `content_missing`) rather than couriered to
the PM as prose, which is what this item asked for.

**Threshold is NOT an agent's to invent.** It is a risk judgement. Propose a
number with reasoning and have it ratified; do not let a coding agent pick
one, and do not ship a placeholder.

**32. Should the desk have ONE drawdown response or two — OWNER CALL, the only thing left on this item.** *Reconciliation half closed 2026-09-14; the daily breaker was rebuilt later the same day, which removes the severity inversion below. Everything under the question is context for answering it, not work.*

**The question.** The desk has two drawdown mechanisms. The three loss alarms measure the account's return over a rolling window (today / 5 sessions / 20 sessions) against a multiple of the normal daily move of the book actually held. The §11.2 de-levering ladder measures how far the account is below its own equity high-water mark, against fixed percentages. Should they be merged into one response, or deliberately kept as two?

**Recommendation, unchanged and now stronger: keep them as two.** Merging is a redesign, not a repair, and it means re-deciding trip points already set once. They also no longer do the same KIND of thing — one halts the desk, the other trims excess exposure — which is far easier to justify than two mechanisms racing to sell the same book. Worth revisiting when there is live evidence of how each behaves; there is none yet.

**What is now established, so this can be answered without re-deriving it** (full detail, `docs/INCIDENT_HISTORY.md` 2026-09-14, two entries):

- **They cannot contradict each other.** A tripped daily breaker cannot block the ladder's de-levering (exits fail open, entries fail closed); the two cannot double-sell the same shares (the ladder runs first and refreshes the broker snapshot before the breaker is evaluated); and all three alarm thresholds are capped at the ladder's -20% owner-alert point, with the 5-day clamped to the 20-day, so `|1d| <= |5d| <= |20d| <= 20%` holds at every volatility. The √time scaling is applied consistently across all three, with no second convention anywhere.
- **The severity inversion is GONE — the sharpest half of this question is answered rather than open.** The daily breaker trips soonest (~3 sigma, about a 3% loss on a 1%/session book) and its response used to be the most drastic the desk has: force-liquidate every position and abandon the session. As of 2026-09-14 that response is a HALT — cancel resting entries, refuse new risk for the session, VERIFY per-position stop coverage at the broker, file a per-symbol reason, alert — and it closes, resizes and zeroes nothing. The ordering is now the sane one: the shallow alarm stops the desk, the deep one trims the book.
- **Why the liquidation went rather than being re-tuned.** It submitted LIMIT orders 1% through the market and then restored the original stops on any leg that did not fill, so on a correlated gap — the one day a whole-book dump could be argued for — it cancelled every protective stop, sold nothing, and put the stops back. It filled only on ordinary days, i.e. it worked only when it was not needed. It had also never fired: zero emergency rows in the archive over 13 days of P&L whose worst day is -0.46%, against a trip point reconstructed near -0.7% of equity. Nor was it the defence against a broker-initiated liquidation: at `max_gross_exposure_x: 2.0` against a 25% maintenance requirement the book can fall 33.3% before a margin call — a week, not a day. `_enforce_gross_ceiling` is what stands there, and it is untouched. (`_force_delever` is not it: it returns `[]` whenever `allow_margin` is true, which it has been since 2026-09-02.)
- **Both previously-open defects on the daily breaker are FIXED, same change, and no trip level moved.** Its denominator counted the CASH PARK: `normalized_holding_weights` was the one risk calculation in the module that did not take a `cash_park_symbol`, and the park was 78% of the gross weight the volatility yardstick was measured over on the archived book. Excluded now, which TIGHTENS the reconstructed daily trip point from about -0.75% to about -0.70% of equity — small, because parked cash barely moves, and in the safe direction. And its numerator (whole-account daily P&L, including realised losses on closed positions, commissions and spread) did not measure the same object as its denominator; it reads the held book now — but ONLY when the held book is what produced the limit. `daily_loss_limit_pct` has three rungs and two of them are percentages of the ACCOUNT, so the engine reports which rung governed and the numerator follows it; the reverse mismatch would have disabled the breaker on a day that ended flat after selling everything at a loss. Sensitivity, cap and fixed fallback are all untouched, and no new number was introduced.
- **Still open, and not a blocker on this decision.** The 5-day and 20-day brakes cannot evaluate until 6 and 21 evening runs have been recorded, and a paused desk accrues none; they now say so instead of printing a null. The halt carries a stated residual of its own: it is only safe if the per-position stops are genuinely live at the broker. Item 35's broker audit (closed the same day) is what makes reading the BROKER non-negotiable rather than optional — it proved `trades.stop_loss` is written once at entry and never updated, so the archive is not evidence about coverage in either direction. Two live reasons the answer can be "no" survive it: a stop can be moved by a maintenance action outside the desk's own trading code, leaving no trade row behind (three were moved that way on 2026-08-31 to lift grandfathered stops to the minimum distance), and a sub-share remainder cannot hold an overnight stop at this broker (item 53).
- **The inherited anchor.** `drawdown_5d_risk_multiple = 3` is an April-2026-era constant that everything else scales from and has never been validated. `drawdown_vol_sensitivity = 3.0` is an owner risk-appetite value, correctly labelled provisional in five places. The ladder's six numbers (-8/-15/-20 and 1.5/1.0/0.5) are ratified, not derived.

No DECIDE BY. Nothing is blocked on the answer.

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

**55. What IS a structural level — what makes a turning point, and how wide is its zone? OPEN. One third of it is now ANSWERED and shipped; the other two thirds are sharpened, not solved. Filed 2026-09-13, worked twice the same day.** *Consolidates two questions deliberately left unanswered on 2026-09-13; they are one question about one object and must not be split again.*

**The question, answerable in three parts:** (a) how many bars either side must a bar dominate before it counts as a swing point? (b) how far either side of a level's reported price does that level's zone actually extend? (c) how many touches make a level a level?

**Part (c) is CLOSED — 2026-09-13, second pass.** `MIN_TOUCHES = 2` in `src/data/levels.py` is sourced, not conventional: Tsinaslanidis (2012) §4.4 uses the same figure to build this exact object, and its own §4.6.1 measurement over 733 NASDAQ/NYSE names (1990-2010) finds level "strength" does not predict trend interruptions. Pinned by `tests/test_level_match_zone.py::test_min_touches_is_two_and_that_one_is_sourced`. Do not re-open and do not "tighten" it to 3. Full quotations and the hit/bounce counts: `docs/INCIDENT_HISTORY.md`, 2026-09-14 (moved from here for the byte cap).

**Where the remaining numbers came from:** convention, honestly labelled as such in the code. (a) `PIVOT_WINDOW` = 3 in `src/risk/trailing.py` and `PIVOT_WINDOW` = 5 in `src/data/levels.py`. (b) `CLUSTER_TOLERANCE_PCT` = 1.0 in `src/data/levels.py`.

**What the second pass ADDED — the archetype is now academically sourced, and the numbers are PLACED against measurement.** The desk's construction is not a home-made one. It is, step for step, the published HSAR method: symmetric rolling-window pivots, grouped into bins of equal PERCENTAGE width, a bin with enough members being a level (Tsinaslanidis 2012 §§4.3-4.4). That the level is a band rather than a price is sourced too — Bulkowski via the same §4.2: "Support and resistance are not individual price points, but rather thick bands of molasses that slow or even stop price movement," from which the thesis infers "a support or a resistant level is an area of prices, rather than a specific individual price level, in where local peaks and bottoms reside." So the SHAPE is right and is now cited in the code. Two placements follow, and both are new information:

  * **The zone.** That literature does not derive the width either — §4.4 leaves it a user input: "The third variable 'x' is the desired percentage distance of each bin." What it does give is a measured insensitivity range: 3% illustrated, and footnote 32 records "desired distances of 2%, 4% and 5% are also implemented", with the body stating "Any further parameterization does not affect the empirical findings." `_cluster` chains a pivot in within 1.0% of the cluster ANCHOR, so a desk cluster spans at most 1.0% — HALF the narrowest bin ever tested there. The match zone built from it (`level_zone_halfwidth`, ±1%) spans 2.0%, exactly that narrowest tested bin. The desk therefore sits at or below the bottom edge of the only measured range that exists for this constant.
  * **The window.** Same source, same construction — "in order to characterize the closing price observed at time t (Pt) as a regional peak, when a rolling window with a length of 50 days is used, this price has to be greater than the 25 preceding and 25 following days simultaneously" — and it too reports the outcome "robust to any different parameterization", across 50/100/150-day windows, i.e. 25/50/75 bars EITHER SIDE. That range does not contain 3 or 5. It is evidence the object is insensitive at multi-month swing scale and complete silence at the multi-day scale a stop is actually placed on. It also reads CLOSES, where this desk reads highs and lows. So it does not license either window and must not be cited as if it did.

**Already searched and ruled out — do not repeat any of this.** For (a) the window: TA-Lib `FRACTAL`, MetaTrader 5, fxssi, LuxAlgo — every one a default or a distribution fact, none a measurement. For (b) the zone: Osler (2000) and (2003), Zapranis & Tsinaslanidis (2012), Bulkowski, Brock/Lakonishok/LeBaron (1992), arXiv:2507.01971 — none derives a width; the 1% coincidence with BLL's whipsaw filter is NOT a source. *Structural facts, settled:* the two pivot windows never meet (`tests/test_pivot_window_independence.py`), a 5-bar dominator necessarily dominates 3, and harmonising them — or adopting the literature's illustrative 3% — was rejected as picking a number. Every source named, with what each does and does not say: `docs/INCIDENT_HISTORY.md`, 2026-09-14 (moved from here for the byte cap).

**What would settle it, now stated precisely enough to run.** The published bounce test is fully specified and has never been run at this desk's own parameters. Take the desk's universe and its own bars; identify pivots with the desk's windows and cluster at the desk's tolerance; call it a "hit" when price enters the zone from outside, a "bounce" when it leaves the way it came and a "failure" when it leaves the other way; compare the bounce frequency against artificial levels drawn at random distances from spot, exactly as Tsinaslanidis §4.5 does. Then sweep the tolerance across 0.5%, 1%, 2%, 3%, 5% and the window across 3, 5, 10, 25 bars either side. This is a READING and not a fit: it measures whether the object the desk has defined is a real feature of its own instruments, and at what width it stops being one — it does not optimise a constant against P&L, and it must not be turned into one by scoring returns instead of bounces. Two outcomes are both acceptable answers to this item: the desk's settings show a bounce edge over random and the width is confirmed as read from its own instruments; or the edge is flat across the whole sweep, in which case the honest conclusion is that the width does not matter and the item closes by saying so. A separate and stronger prize remains available if either sweep is flat: reformulate so no percentage is stated at all — the level's zone becomes the span of the pivot BARS that made it (a pivot is a bar with a high and a low, not a price), which needs no constant and is read entirely off the instrument.

**Cost while unanswered:** 1% is the desk's definition of "the same level" everywhere, and since 2026-09-13 it is also the slack on whether a stop counts as level-backed — which decides whether the ATR floor moves that stop, which changes reward:risk and therefore position size on every risk-sized trade. On a $200 stock the match zone is $4 wide; two bounces $1.90 apart are one level and two bounces $2.10 apart are two, and nothing says that is where the line belongs. The second pass narrows the cost rather than removing it: the desk is now known to be running at or below the tightest setting anyone has measured, so if the object is width-sensitive at all, the desk is on the edge where it would show. The windows cost less because they never meet, but 3 and 5 both still disagree with the vendor archetype's own default of 2 and sit an order of magnitude below the only academically tested range.

**56. Is a stop too wide, and read off what? The THRESHOLD is still unread. OPEN, narrowed 2026-09-14.** *The 2.5x ATR fallback stop AND the reach cap that gates it — one question. Answered parts are in `docs/INCIDENT_HISTORY.md` 2026-09-14; read it first.*

**The question, now precisely one thing:** at what probability of being touched inside the trade's own horizon does a stop stop being a stop?

**Already settled, do not re-derive — detail in INCIDENT_HISTORY 09-14.** (a) The filed premise was FALSE: an ATR multiple means nothing until the horizon is fixed, and restated as touch probability Kullamägi's 1x ATR cap over the 3-5 sessions his own text gives is 35.7-47.5% against the desk's 37.2% at 20 sessions. "The desk's stops are 2.5x too wide" is WITHDRAWN. (b) `MAX_REACH_ATR_MULTIPLE = 1.5` was estimating targets AND refusing trades; split into `max_target_reach_atr_multiple` and `max_stop_width_reach_atr_multiple`, same value — hygiene, not an answer; neither is derived. (c) The gate refuses at a CONSTANT **1.67%** touch probability at every horizon, since cap and reading both scale with sqrt(H), and `2.5 <= 1.5*sqrt(H)` for every `H >= 3` means it can never fire on the desk's own fallback. (d) `src/data/levels.py::touch_probability` records that reading on every stop the constructor sizes; its docstring carries the derivation and citations. Pinned by `tests/test_stop_width_gate.py::TestStopWidthReadingAndSeparation`.

**Already searched and ruled out — do not repeat; reasons in INCIDENT_HISTORY 09-14, names and links only here.** Kullamägi (reconciled; a cap on an intraday-timed entry, not a multi-day survival measurement). Chandelier and Van Tharp at 2-3x ATR (conventions). Van Tharp sizing (arithmetic; answers width with a smaller position, not refusal). **Maximum Adverse Excursion (Sweeney)** — the archetype that looks like the answer — ruled out as FITTING, and unavailable on one live trade and 53 archived rows (https://www.luxalgo.com/library/concept/mae-mfe-distributions/). **The ACADEMIC literature was searched separately on 2026-09-14**, after item 55 showed the 09-12 pass had read only vendor material, and it says the threshold is open work: Han, Zhou & Zhu, flat 10% swept at 5/15%, *"we leave the search for optimal stop-loss strategies as future research"* (https://www.cicfconf.org/sites/default/files/paper_811.pdf); Kaminski & Lo (J. Financial Markets, 2014) reviewed as giving *"little guidance"* at these horizons, its reviewer's own level swept over *"γ = 0.5-5%"* (http://arc.hhs.se/download.aspx?MediumId=593). Adopting their numbers imports a foreign default — the unsourced act in the other direction, declined by item 55. Do not re-propose "no floor, no trade" (falsified 2026-09-12, George & Hwang 2004 and Bulkowski).

**What would settle it:** a published measurement of stop survival stated as a PROBABILITY, not a multiple. The reading is built; only the threshold is missing. Two reformulations needing no threshold, to try FIRST: (i) refuse when the stop's touch probability is below the TARGET's reach probability computed the same way on the same instrument — a probability-form reward:risk test REPLACING the arithmetic ratio; (ii) accept that no threshold exists and DELETE the width gate, since §2.1 sizing already answers a wide stop with a smaller position. Deletion is permitted and is not the lazy hack — that would be switching it off without (a) and (c).

**Cost while unanswered:** small, now quantified — the gate fires only past 6.7x ATR on a three-week trade and never on the desk's own fallback, so it under-refuses. Whether anything SHOULD be blocked is uncosted.

**60. The exit path's two refusal layers point in OPPOSITE directions, and this item asked for both at once. OPEN; re-scoped 2026-09-14 — the schema half shipped, the contradiction is what is left.** As written it could not be answered. It named the hole as UNDER-refusal — no deterministic gate "can catch a plausibly-worded, deterministically-clean, wrong exit" — then gave OVER-refusal as the reason for caution: a refused sale leaves a broken-thesis position on the book overnight behind only the broker stop. Closing the named hole needs MORE refusal; the caution needs less. Both cannot be the priority, and choosing neither produced the deferral. **The measured record says the direction was assumed backwards.** Re-verified 2026-09-14 against the archived database: three exit-path risk reviews exist in all — rows 296 (2026-08-31 19:32:50, `close-100065e1`), 319 (2026-09-01 17:03:31, `midday-a40ca27b`) and 330 (2026-09-01 19:32:32, `close-0e9129f1`). The AI seat saw 8 exits and approved 8 — zero refusals, zero modifications. The deterministic layer, which runs AFTER it speaks, blocked the SAME 8: `intraday_evaluations` holds 7 `exit_blocked_inside_atr_noise_band` and 1 `exit_blocked_no_named_trigger` across those three run ids and no other status on them. So the AI refused 0 of 8, Python refused 8 of 8, and **no discretionary exit has ever reached the broker through this path.** The only refusal behaviour ever measured here is over-refusal by the deterministic layer — the opposite failure from the one named. The under-refusal hole describes the gates truthfully but is unreachable until an exit survives them; none has. **A related incoherence, verified 2026-09-14 in `pipeline.py`.** An unavailable, unparseable or verdict-less model makes `_risk_review_exits` FAIL OPEN — every exit proceeds unreviewed, owner-ratified 2026-08-27 so an outage cannot trap a dead position. But `_reason_cites_hard_trigger` is a case-insensitive SUBSTRING match over the reviewer's prose, and a miss DROPS the exit — FAILS CLOSED (archived row 135, V, `close-0e9129f1`). The desk trusts an exit MORE when the model is absent than when it is present and phrases its reason without a recognised keyword. However the fail direction is settled, it must be the same answer in both places. **"Wait until real trading produces more exit reviews" is NOT valid and must not be re-proposed.** More approvals cannot settle a prompt or seat question: `src/agents/risk_review_mode.py` and the 2026-09-13 `docs/INCIDENT_HISTORY.md` entry both record that no rig here can validate a prompt rewrite — it replays recorded answers into a changed prompt and passes regardless. And the sample is SELECTED by the gates under question: those 8 exits are what the upstream reviewer produced, and every outcome was set by the gates being judged — the self-referential shape `docs/OUTCOME.md` names as the `pace` failure, the desk's largest identified P&L defect. **Settled.** "A buy-plan auditor wearing exceptions" no longer holds: PR #343 gave this path its own prompt, this PR its own schema (`src/agents/risk_review_mode.py` carries the account), and neither needed new data. **The ARCHITECTURE question left:** which layer owns refusal here, and can the two be made coherent about fail direction? Not "should the exit path get its own reviewer" — it has one. A deterministic keyword layer overrides an AI seat that has never once disagreed with it, refuses on the words rather than the claim, and fails CLOSED where the seat it overrides fails OPEN. **What would settle it:** reconciling the two fail directions — no new data needed, both are stated desk positions and they contradict each other; and why the noise band blocked 7 of 8 exits, a live over-refusal question with real evidence, unlike the under-refusal hole, which has none. **Owner's, kept separate:** how readily the desk should block a SALE at all — risk appetite, unreadable off any instrument. The architecture and market-structure halves are NOT his. No number is proposed here.

**60 — a related gate hole is CLOSED (2026-09-14): the ATR band's only non-redundant domain now consults `check_structural_protection`, additively.** `docs/INCIDENT_HISTORY.md`, 2026-09-14. **Still open:** the questions above, and the band's own multiple (item 70).

**63. `signal_weight` cannot say "pay attention, and the sign is the other way" — OPEN, no source found, carried out of item 52.** One scalar in `[0,1]` does two jobs: it is the ranking sort key and the dollar multiplier deciding what reaches the analyst seat. It has no way to express direction. Scott & Xu (FAJ 2004) measure an insider sale under 10% of the holding at **+0.68%** size/B-P-adjusted quarterly excess return, significant at 1% — a mildly *bullish* fact arriving on a *sell* row. Today that row gets weight 1.0, identical to an insider dumping 80% of a position at −0.81%; before 2026-09-13 it got 0.0 and vanished from the ranking. Both are wrong, in opposite directions. **Not a number to pick.** Choosing a multiplier that splits the difference would be fitting, and the ratio and band are already reported on every observation so the seat can read the sign itself — this item is about whether the *deterministic* ranking should also know it. **Searched and ruled out:** Scott & Xu themselves (they report band returns, never a weighting scheme); Cohen/Malloy/Pomorski, whose routine/opportunistic split is a binary with no magnitude and no direction; the desk's own history, which has too few insider-sourced fills to measure anything. **What would settle it:** a published source that scores insider signals on a signed scale rather than sorting them into bins, or enough of this desk's own outcome data to read a separation directly — neither exists yet. Until one does, the ratio stays reported and unweighted. Detail: `docs/INCIDENT_HISTORY.md`, 2026-09-13.

**64. The backtest rations the risk budget alphabetically, and cannot do otherwise until it has a candidate ranking — OPEN, found 2026-09-13 while building the best-ranked-first rationing rule (retired item 49; see `docs/INCIDENT_HISTORY.md`, 2026-09-14).** `src/backtest/engine.py` builds every day's candidates and hands `allocate_risk_budget` one `RiskRequest` per candidate at `config.risk.max_position_risk_pct` — the SAME number for all of them. The allocator's pre-decision ordering is largest-request-first with an alphabetical tie-break, so with every request identical the tie-break is the ONLY thing ordering them: on any day the budget binds, the backtest funds candidates in alphabetical order. That work fixed the production path by spending the budget down `rank_verdicts`' own order, and deliberately did NOT touch this one: the backtest is signal-driven and produces no analyst verdicts, so there is no ranking to spend down and inventing a score to stand in for one is exactly what the no-arbitrary-numbers rule forbids. **The consequence:** any backtest run on a day where total requested risk exceeds `max_portfolio_risk_pct` measures a desk that picks trades by ticker spelling — so its results on those days do not describe the desk that now runs in production, and neither the old nor the new production rule can be evaluated by backtesting until this is closed. **What would settle it:** either the backtest gains a deterministic per-candidate score derived from the same signal machinery it already computes (and that score has to be read off something, not fitted), or the engine is honestly documented as unable to evaluate rationing behaviour and every result is reported alongside how many of its days had a binding budget. Nothing was searched for yet beyond confirming the requests are uniform, which was read directly off the code.

**65. Four of the five analyst seats have no strength scale of their own, and the deletion of the fake ones did not answer whether they should — OPEN, opened 2026-09-13 by the review of PR #348.** *Owner-call label removed 2026-09-14, see `docs/INCIDENT_HISTORY.md`.*

`rank_verdicts` scores a candidate on two things per seat: how far the seat leans (`magnitude`) and how sure it is (`conviction`). Only Technical states a lean — its rating rungs are a strength it actually publishes. News, macro, smart_money and earnings do not, so each of them now reports `NO_STATED_STRENGTH` (0.0) and reaches the ranking through its weighted conviction alone. Nothing is invented, and nothing is borrowed — which is the improvement over both prior states (three unsourced tables, then one borrowed rung).

**The exact open question.** Should those four seats be given a real strength scale — a field in their own schema that measures how far the read leans, distinct from how confident it is — or is "direction plus confidence" genuinely all any of them can say? Today the desk has assumed the second by default, because it is the only answer that requires inventing nothing. That is a defensible default and a poor decision record.

**What was searched and ruled out, so nobody redoes it.**

  a. Deriving a lean from a field the seat already reports. Ruled out and DELETED, 2026-09-13: for news, macro and smart_money it was the same field the verdict hands to `conviction`, so the composite counted one signal twice at three different unsourced spacings. Detail: `docs/INCIDENT_HISTORY.md`, 2026-09-13, item 31.
  b. Borrowing Technical's `buy` rung (0.5) for the seats that have no rungs. Ruled out and DELETED the same day on review: a number read off another seat's scale is not read off this seat's instrument. It is the same failure as (a), one step quieter.
  c. Fitting a spacing to this desk's own resolved calls. Forbidden by `qamc-no-fitting-only-reading`, and impossible anyway — the conviction ledger is far short of the ratified 20-resolved-calls-per-seat bar (`_CONVICTION_OUTCOME_MIN_N`, `src/storage/db.py`), which is itself not yet wired.
  d. Citing a published source for a cross-seat strength spacing. Nothing to cite. The 2026-09-03 literature reviews behind `SEAT_WEIGHT` produced a sourced ordinal ranking of seat RELIABILITY and explicitly no cross-category ratio; none of them speaks to how far a given read leans.

**What evidence would settle it.** Either (1) a seat's own schema is extended with a strength field the analyst must state and justify per call — the same shape Technical already has, which makes the number read from that call rather than chosen for the seat; or (2) the conviction ledger clears the 20-resolved-call bar per seat and a lean can be measured out-of-sample. Until one of those exists, 0.0 stands and the ranking is a breadth-and-confidence ranking, which is what it should be described as.

**Do NOT resolve this by picking a number**, and do not resolve it by removing the four seats from the ranking — the whole point of Phase 13 was that all five seats reach the ordering.

**70. One underived `1.0` is doing two different jobs in the exit path, and neither is read off anything — OPEN, filed 2026-09-14 while wiring the structural check into thesis-invalidation exits (item 60).** `NOISE_BAND_ATR_MULTIPLE = 1.0` in `src/risk/exit_guard.py` sets how far an adverse move must travel before it stops being noise, and is reused inside `check_structural_protection` as the margin a close must clear to count as beyond its backing level. `absolute_min_stop_atr_multiple: 1.0` in `config/settings.yaml` sets how tight a stop is allowed to be. They are the same round number in the same unit answering two different questions, and neither has a derivation or a cited source on record. That they agree is a coincidence of both being 1, not a relationship anyone established — nothing in the code ties one to the other, so a future change to either silently breaks whatever alignment is being assumed. **Deliberately NOT fixed in the item-60 PR**, which adds information to a gate and changes no number. **What would settle it:** for each of the two independently, a published measurement of the quantity it claims to bound (an ATR multiple at which adverse moves stop being noise; an ATR multiple below which a stop sits inside ordinary daily range), or a decision that one of them should be derived from the other and made a single named constant. Nothing has been searched for yet beyond confirming that neither number is referenced to anything in-repo.

**75. How the desk banks a winner is set by three typed numbers, and it gives back a fixed slice of every run-up — OPEN, filed 2026-09-14 after an owner question on ORCL.** The desk places NO take-profit order (verified: `AlpacaBroker.submit_order` builds only limit/market requests; `take_profit` is a reference). Profit is banked only when the trailing stop loss ratchets past entry, or a reviewer sells. That trail runs on three unsourced numbers: 1.25×ATR14 distance (2026-07-16; its settings comment wrongly says MEASURED), a 2% minimum step (`MIN_RATCHET_PCT` and the prompt's `≥1.02×`, 2026-05-13), and a ~4-day cooldown restraining the step. **The case:** ORCL entry 146.27 on 2026-09-02, high 170.70 on 09-08. With the trail running it exits 161.98 on 09-09 (+10.7%); the give-back from the peak equals the trail distance by construction. (Actual: ~−1.6%, because the desk was paused.) **Researched 2026-09-14, fetched sources:** a "trailing take profit" is real mainly on crypto-bot platforms and is mechanically a trailing stop that arms once in profit, not a rising ceiling; none of the stock brokers checked offer it, and **Alpaca cannot** — bracket take-profit legs must be limits and trailing stops are single-order only (docs.alpaca.markets/us/docs/orders-at-alpaca). **Position reached, not yet ruled:** no profit ceiling on BREAKOUT trades (they pay for the losers); a sell at the defended level on RANGE trades is legitimate because that price is read off the chart; the cooldown should go, since after a spike it adds give-back. **Strongest objection:** range trades are the most common setup, and a ceiling there sells exactly when a range becomes a breakout. And deleting the 2% step and cooldown still leaves the unsourced 1.25. **What would settle it — a FIX, not a patch:** a trail distance read off the instrument or a cited source rather than typed; the minimum step and cooldown removed together or each justified; an explicit rule for whether a range trade sells at its level. Changing any one number alone is the patch this item exists to avoid. Unsourced inventory: `docs/INCIDENT_HISTORY.md`, 2026-09-14.

**71. When a stop moves, the desk never updates its own record of it — OPEN, found 2026-09-14 while auditing items 35 and 69.** `trades.stop_loss` is written ONCE, at entry, and is never written back when a protective stop is cancelled, replaced, widened or tightened — by the trailing path, by the coverage repair, or by any out-of-band call. The broker is the only place the live level exists. **This is not cosmetic: it has already manufactured two false defects.** Items 35 (V) and 69 (DIS) were both filed as "the position traded through its own stop and is still held", and both dissolved on the broker record — in each case the archive was quoting a level that had been cancelled days earlier. **The consequence that matters beyond bookkeeping:** any analysis of stop distance, R-multiple at exit, or coverage drawn from `trades` is measuring a number that may be days stale, and nothing in the schema says so. The reviewer's live `distance_to_stop` was right throughout, so the divergence is silent. **What would settle it:** write the new level back on every path that changes a stop (one home already exists — every replacement funnels through `AlpacaBroker.replace_stop_loss` or the coverage repair), plus a reconciliation that compares the recorded level against the broker's live stops and reports a mismatch rather than letting it accumulate. An out-of-band call leaves no row for a write-back to catch, hence the reconciliation too. **Do NOT re-file a "traded through its stop" observation as a defect until this is closed** — the archive cannot support that claim in either direction.

**72. The only real-day benchmark scenario cannot see payoff geometry, and a better day is sitting in the archive uncaptured — OPEN, filed 2026-09-14.** `pm_selection`, the sole scenario built on an actual production day, runs on a fixture where ZERO of 59 analysis rows carry `computed_levels`. The structural reward:risk the desk's live admission rule reads therefore cannot be computed for any name on it. Admission does not depend on it (verified), so the scenario still measures selection; it CANNOT measure whether a model reads payoff geometry the way the desk now does. **This is a limit on what the model test reports, not a gate on running it** — and it must be stated in the result rather than discovered afterwards. **The replacement already exists and needs only capturing:** production run `run-bba4d4f3`, 2026-09-02 — 63 of 64 analyses carry computed levels and all 34 actionable candidates have a computable structural ratio. **What would settle it:** capture that run as a frozen fixture the same way `pm_selection` was captured, re-point the scenario, and confirm the structural ratio is non-null for every actionable candidate. No number is chosen and no threshold moves; this is data capture.

**Retired item numbers — never reuse.** 0, 2, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 33, 34, 35, 36, 37, 38, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 53, 54, 57, 58, 59, 61, 62, 66, 68, 69 in this queue, and 1, 2, 3, 4, 5, 6, 8 in the PM test gate, were deleted once written up in `docs/INCIDENT_HISTORY.md`. Item 10 retired 2026-09-14 (`docs/INCIDENT_HISTORY.md`, that date) — every constructor drop path files a machine-readable reason and an AST guard fails on a new one that does not. Item 38's open follow-up survives as item 52, whose own unresolvable residue is item 63. Reconstructed from git history 2026-09-14 after corruption; 1, 3, 8 and 20 are NOT retired in this queue (live items; 8 is live here AND retired in the PM test gate, a legitimate cross-scheme split); 67, 90, 101, 200 never existed. Next free number is 76: 71 (stop write-back), 72 (benchmark fixture capture), 73-74 (2026-09-11 audit) and 75 (trailing exit numbers) were filed 2026-09-14. PM test gate's own item 4 (news analyst data quality) closed 2026-09-14 and is retired in that scheme only — the funnel queue's own item 4 is unrelated and stays live. Item 53 retired 2026-09-14 — the half-hourly stop-coverage sweep is installed, enabled and has repaired a real position on the box; its overnight fractional gap is a STANDING BROKER LIMITATION, not an open item, and must not be re-filed. Items 35 and 69 retired 2026-09-14 — both were the same illusion (the archived `trades.stop_loss` column is never written back on a stop cancel/replace, so it can look like a position is trading through an unfired stop); `docs/INCIDENT_HISTORY.md`, that date, "the Visa position that 'traded through its own stop' never did". Full account, and every renumbering the corruption forced (62/63, then 65, then 68/69/70): `docs/INCIDENT_HISTORY.md`, 2026-09-14, "the retired-item-numbers line was quietly corrupted, and it was making the corruption worse".

## Evidence-only follow-ups

- news-narrative factual drift;
- `actual_provider` attribution oddity.

Do not interrupt natural validation for these unless current evidence shows they materially distort decision quality, truthfulness, or operator understanding.

`get_latest_price` is **not** on this list solely because its request omits `feed`; that concern has been reconciled. Reopen only on concrete production evidence.

- invalid_if: 9/3 log.

