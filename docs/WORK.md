# QAMC Current Work

## Active finish line

### Session start — read this first

## DECISIONS PENDING — CI FAILS WHEN ONE GOES OVERDUE

**Do not delete a line to make the build pass. Decide it, then record the
decision and remove the line in the SAME commit.** Format is fixed —
`- [ ] DECIDE BY YYYY-MM-DD — question` — because
`test_no_pending_decision_is_overdue` parses it.

This block exists because a decision was deferred here once and forgotten. On
2026-08-28 this file said, of the reward:risk floor: *"gather a week of these
rejections first, then decide which of the two numbers is wrong."* Nobody came
back. Four days later the desk reviewed 38 qualified signals and placed zero
trades for exactly that reason.

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

- [ ] DECIDE BY 2026-09-09 — Level quality bar for Phase 12.1.
  **DATE MOVED 2026-09-02, and the item got MORE load-bearing, not less.**
  `wt/levels-wire` merged the same day and now scores level strength by TOUCH
  COUNT (the unmeasured 252-session recency half-life is gone), which changes
  top-6 level selection on 66 of 99 symbols. Touch count is now the primary
  ranking signal AND the unresolved quality bar, so this compounds.
  It is deferred only because `wt/rr-geometry` is actively editing stop and
  level logic and two writers must not collide — start this the moment that
  branch lands.
  **This is NOT an owner-picks-a-number question.** Standing project rule:
  market-structure thresholds come from published technical-analysis
  literature, never from a model's training-recall, never from speculation,
  and are never handed to the owner to guess. Research the doctrine, propose
  the number WITH its source, then ratify. A stop is now
  honoured if it sits on a computed structural level, but a level currently
  qualifies on **two touches ever, anywhere in ~3 years** — so two old swing
  points can justify a very tight stop. **The 3x ATR floor used to hide this
  by widening such stops; 12.1 honours them instead, which makes the bar for
  "is this a real level" load-bearing in a way it never was before.** Needs a
  recency and/or touch-count requirement. Risk judgement, owner's call, NOT an
  agent decision — no agent may pick a threshold here.

**DATA QUALITY AUDIT — 2026-09-02, owner priority: this pillar (garbage in,
garbage out) must work before anything else.**

Owner's instruction: every one of the 5-6 shared analyst seats (tech, news,
macro, earnings, smart_money, evening) has data-quality issues — empty
fields, silent death, or empty data passed to the PM as if it were real.
Audited from real production logs, not assumed. Ranked by measured severity:

1. **Earnings — worst, fixed.** ~20 of 67 filings recovered under 1,600
   chars from a 184k-char filing (failed section match, not sparse data);
   12 including MSFT/AAPL/GOOGL/BAC/CVX/NFLX extracted ZERO figures. Root
   cause: text-regex heading match is inherently unreliable across filers.
   Fixed by pulling the numbers from SEC's structured XBRL API instead,
   independent of the text matcher — verified against live SEC data.
   Separately, a fabricated valuation claim (P/E, market cap invented
   despite no price data given) was detected but never closed the loop —
   the same bad number re-served from cache for days (KO, MTZ). Now
   redacted and the cache self-heals. See PR merging
   `fix/earnings-data-quality`.
2. **Smart money — fixed.** Token ceiling (1200) was 13x smaller than
   every other seat with no measured justification; a real call truncated
   in production. Resized to 3000 from measured production usage, and
   truncation now gets its own `data_status` value instead of hiding
   inside "empty". See PR merging `fix/smart-money-tokens`.
3. **Tech analyst — already fixed by an earlier 2026-09-02 session**
   (`thesis_invalid_if` null crash); confirmed no recurrence post-deploy.
4. **News analyst — root cause NOT found yet.** One production failure
   was structural (4 required top-level fields missing entirely, not one
   malformed item) but the raw LLM payload isn't captured anywhere, only
   the post-hoc log line. Needs raw-response capture wired in before this
   can be diagnosed properly — do not guess a fix without it.
5. **Evening analyst — flagged by a peer session (17 validation
   failures), NOT YET AUDITED.** Next up.
6. **Macro analyst — no defect.** Its warnings are a sanity-check working
   as designed (rejects an LLM-claimed regime shift on insufficient fresh
   indicators).

**Also shipped: a bad analyst seat now gets its OWN Telegram alert.**
Before this, `data_status` anything but "ok"/"empty" only showed up as one
line inside the routine session-result message — exactly what the alert
rule below forbids. See PR merging `feat/data-quality-alert`.

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

**PHASE 11 IS HALF-BUILT AND NONE OF IT IS MERGED.** Four agents were dispatched;
the process exited before three of them finished. Their work is committed on
their own branches and pushed, labelled WIP UNVERIFIED. **It is untested and
unreviewed — do not merge any of it without running the suite.**

| branch | what it holds | state |
|---|---|---|
| `worktree-agent-a133d12fa507af335` | margin interest tracker | committed by the agent, suite result never reported |
| `worktree-agent-a68a5aa21271ccbea` | 11.2 gross cap + de-levering ladder | WIP, interrupted, UNVERIFIED |
| `worktree-agent-ab445cf7c4b9aa358` | 11.1 fractional sizing + stop guards | WIP, interrupted, UNVERIFIED |
| `worktree-agent-af17eb92755512448` | sector cap fails loudly on unresolved sector | WIP, interrupted, UNVERIFIED |

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

**Config drift is closed (2026-08-28).** `config/settings.yaml` in git now
matches the production box byte for byte. Until this change the box carried
five hand-edited values that existed nowhere in git, so any deploy that lost
the stash/pop step would have silently reverted them — including the two that
were raised specifically to end the 2026-08-28 outage. Reconciled:

| setting | was in git | now (and live) |
| --- | --- | --- |
| `intraday_scan.enabled` | `false` | `true` |
| `llm_cost_circuit.daily_cost_limit_usd` | `1.50` | `2.75` |
| `llm_cost_circuit.session_reserved_exposure_limit_usd` | `1.80` | `2.60` |
| `llm_cost_circuit.daily_reserved_exposure_limit_usd` | `1.90` | `5.50` |
| `llm_cost_circuit.max_paid_sessions_per_mode_per_day` | `2` | `8` |

**The Mission Control URL — and a stale preview that was masking a week of
work (2026-08-28).**

- The correct, production Mission Control address is
  `https://ovh-vps.wallaby-bowfin.ts.net/cockpit/`. Tailscale Serve proxies
  tailnet-only port 443 to the qamc API on `127.0.0.1:8800`.
- The qamc API binds loopback-only by design (`QUANT_AGENT_API_HOST=127.0.0.1`
  in `quant-agent-api.service`). Tailscale Serve, not the bind address, is what
  makes it reachable. Do not "fix" reachability by rebinding the service.
- `http://100.111.170.97:8810/cockpit` is NOT Mission Control. It was
  `ops/preview/branch_preview.py`, the ephemeral branch-preview server,
  running as the parked `dev` account out of
  `/home/dev/projects/quant-agent-dashboard`. Its own module docstring states
  it has no systemd unit and no auto-start and is meant to be killed after a
  review session.
- It was started 2026-08-21 16:16 ET and was still running on 2026-08-28,
  seven days later. It served a bundle built 2026-08-21 09:43 containing no
  dockview layout key at all — predating PR #120 entirely. None of the cockpit
  trader-view work (PR #120, pass 2 via PR #130, pass 3 via PR #137) was
  visible at that address.
- The orphaned process (PID 2267757) was killed on 2026-08-28. Port 8810 is
  now closed. The production URL was re-checked immediately afterward and
  returned HTTP 200.
- **Diagnostic worth keeping:** to tell the two apart in one step, compare the
  hashed bundle filename returned by `curl -sk
  https://ovh-vps.wallaby-bowfin.ts.net/cockpit/` against whatever else claims
  to be the cockpit. Different filenames mean something other than production
  is being served.
- **Consequence for `feat/telegram-links` (PR #136):** it defaults
  `notifications.mission_control_url` to the stale
  `http://100.111.170.97:8810/cockpit` in both `config/settings.yaml` and
  `src/config.py`. That is being corrected to the HTTPS tailnet host before
  merge; note it here so the reason is on record.
- State plainly that this is the likely explanation for the operator
  repeatedly seeing old cockpit code after deploys that had in fact landed
  correctly.

**A `git checkout` on the box is not a deploy.** The API holds the
cockpit bundle, so `/cockpit` keeps serving the old one until
`quant-agent-api.service` is restarted. Always restart it and then
confirm the hashed bundle filename the server returns matches the one on
disk under `src/api/static_cockpit/assets/`.

Two corrections to the 2026-08-28 notes recorded elsewhere in this file: the
git baseline for `daily_reserved_exposure_limit_usd` was `1.90`, not `3.20`
(`3.20` was itself an earlier uncommitted box value), and `daily_cost_limit_usd`
was also a git delta — the box had been running `2.75` against a committed
`1.50`.

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


**Two findings from 2026-08-27 that outlive this PR:**

1. **The model benchmark harness was broken two independent ways and nothing
   detected either.** `ops/model_policy/scenarios.py` stopped importing when
   Phase 1 (`138edd2`) made `setup_type` required — same day — AND it had
   carried a backslash inside an f-string expression since `2016c9b`
   (2026-08-14), which is a SyntaxError on Python 3.11, the project's declared
   floor and what CI runs. Local dev is 3.12, so it parsed here and never
   there. `tests/test_ops_scripts_importable.py` now imports every module
   under `ops/` and `scripts/` and rejects 3.12-only f-strings, because
   `pytest` collects `tests/` only and that blind spot is what let both sit.
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

- **Short selling ships finished and enabled, not behind a flag.** An earlier
  draft proposed shipping Phase 5 stages 2-3 disabled by default. The owner
  rejected that: this is a paper account that resets, markets are closed,
  there are no users and no real money, and a disabled feature is unvalidated
  code — the point of reaching the finish line is to surface the next layer
  of bugs. The gate is completeness and verification, not a switch.
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

Six incident and deployment records from 2026-08-31 (the desk going dark at the
open, the provider refusals, the phantom charges, the reward:risk gate, the
Gemini routing move, and the deploy itself) now live in `docs/INCIDENT_HISTORY.md`, along with everything else
finished that this file used to carry.

They were moved rather than trimmed. WORK.md has a hard 100,000-byte cap, so
finished incidents were being deleted to make room for new work — which meant
the record of what went wrong disappeared exactly as it became history. The
defect log is append-only and is never trimmed.

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

The single largest cause; nothing else is close. 10 died before an order was
built, 7 were killed by the AI Risk Manager citing the 1.5 floor by name.

Independently confirmed 2026-09-02, and the count UNDERSTATES it: where the
floor does not reject outright, the Risk Manager **halves the allocation**
instead ("Halve allocation per R/R enforcement policy" appears verbatim on
XLF, XLE x2, XLB). So the floor both blocks and shrinks, and only the blocking
half is counted above.

It rests on a fake number. Evidence, verbatim from the record: *"PM's reasoning
assumes R/R 1.67 but the executed order has R/R 1.18"* — the same trade,
evaluated twice, with different stop geometry. Cause is the ATR stop FLOOR
overwriting a structural stop, widening the risk denominator and crushing the
ratio. See `qamc-rr-geometry-defect`.

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

**2. Thirteen proposals died with no explanation anywhere — 13 of 68 (19%). NO RECORD.**

Second-largest "cause", and it is not a rule at all — it is a hole in the
record. Nearly a fifth of all ideas vanish with no table row and no surviving
log line explaining why. One run produced real database rows while emitting
zero log lines.

Until this is closed, **every percentage on this page has a 19% blind spot**,
and any future claim that a fix "worked" is unfalsifiable to that margin.
Treat closing it as a prerequisite for trusting the re-measure, not as
optional instrumentation.

Also in this bucket: a 9-item `order_not_placed` shape where an order was
built and then nothing else appears in any record. That looks like an
interrupted run, not a deliberate no-trade.

**3. Accepted by the broker, never filled, cancelled — 6 of 68 (9%). WORKING AS INTENDED.**

Price protection behaving correctly, but it is a real cost: the slot was
consumed, the idea aged out, and nothing was bought. Worth revisiting the
repeg policy rather than the limit itself.

**4. A SECOND reward:risk floor at execution time, set to 1.2 — 4 of 68 (6%). WORKING AS INTENDED, BUT.**

Working as designed, but one quantity has two definitions with two different
numbers, and neither is doctrinally grounded. Fold into item 1(b); do not
resolve it separately.

**5. Allocation rounds to zero shares — 3 of 68 (4%). CHECKED, NOT A LIVE DEFECT.**

Full reasoning + regression test: `docs/INCIDENT_HISTORY.md` ("funnel item
5"). Fractional sizing already prevents this; the 3 hits predate it.

**6. No structural level from which to derive a target — 3 of 68 (4%). TOO NEW TO CLASSIFY.**

All three are from 2026-09-02, i.e. one day old. Re-measure before acting;
this may be a new regression from that day's ship.

**7. AI Risk Manager vetoes the entire plan for incoherence — 2 of 68 (3%). TOO STRICT.**

Notable because it is reproducing AFTER a fix intended to stop exactly this.
One veto discards every trade in the plan, so its cost is superlinear.

**8. Stop placed on the wrong side of entry — 2 of 68 (3%). DEFECT, upstream.**

The refusal is correct and must stay. The defect is whatever produced a
wrong-sided stop in the first place, which is unfixed and unlocated.

**9. Tail causes — 3 of 68 combined. WORKING AS INTENDED.**

Insufficient cash (1), a quote 14.6% off reference rejected as dirty data (1),
outright broker rejection (1). Not material; do not spend time here.

**10. Slots burned re-proposing names that never fill. NOT YET DIAGNOSED.**

NVDA proposed **9 times, filled once**. JPM, VLO and PATH proposed 3 times
each and filled **zero**. The desk has no memory of having already been
refused, so it re-litigates the same names while genuinely new ideas go
unexamined. Partially addressed on `wt/stuck-loops` (merged 2026-09-02) —
**re-measure before assuming it is closed.**

**11. Fourteen outright agent failures, ten of them on 2026-08-25 alone. DEFECT.**

The desk produced **zero proposals that entire day** and this was not noticed
at the time. A day of total silence looks identical to a quiet market from
every surface we have. Related to item 2 and to the blind-data-day work
parked on `wt/empty-levels`.

**12. The desk's own funnel reporting misattributes vetoes. DEFECT, pre-existing.**

`_outcome` in `src/pipeline.py` blames every originally-proposed symbol when
the Risk Manager vetoes a plan — including symbols the constructor had already
dropped before the Risk Manager ever saw them. The census script corrects for
this internally; **the source does not**, so the dashboard and any future
funnel report are wrong in the same direction. Reported, deliberately not
fixed, because it is out of scope of the census that found it.

**13. One number is carrying three different meanings. DEFECT, latent.**

Not from the census — this one is structural, and it was raised in
conversation, never written down, and then lost to a context compaction. The
owner asked for it back by name. Recording it here so that cannot happen a
second time.

A target weight of **0%** currently means three incompatible things and the
plumbing cannot tell them apart:
  1. *"Do not open this"* — a refusal. Nothing should happen.
  2. *"Close what is held"* — a real exit instruction.
  3. *"Open a short"* — a new position in the other direction.

A zero-weight entry reads to the delta loop as meaning 2. So a rule that
refuses to BUY something can silently SELL a position nobody asked to sell.
It does not error; it just liquidates. The signed-dissent rule shipped
2026-09-02 works around this by DROPPING a refused target rather than sizing
it at zero — but that is one caller remembering, and every future rule that
can refuse a target inherits the same trap.

**The fix is borrowed, not invented.** `pysystemtrade` (Rob Carver) solves
exactly this with an override algebra: an override is not a number, it is a
value in a small ordered set, and combining two of them is defined by rule
rather than by arithmetic. Its order is absorbing —

    no_trading  >  close  >  reduce_only  >  (a plain multiplier)

— so combining any two overrides yields the more restrictive one, always, and
"do not trade" can never be diluted back into "trade a bit" by multiplication.
Applied here: make the three intents DISTINCT VALUES with defined combination
rather than three readings of one float, so the mistake stops being
expressible instead of relying on each caller to remember. That is the whole
point of the pattern — the guarantee is structural, not procedural.

Sequence it AFTER item 1. It is latent (no measured loss yet), while item 1
is costing 25% of all proposals now.

**14. Replace the budget guard — OWNER-APPROVED 2026-09-02. Design decided, not a tuning exercise.**

**The decision: stop predicting what a call will cost. Delete the per-call
reservation layer.** Replace it with three things:

  a. **A spend cap on the OpenRouter API key itself.** Outside our code, so
     no bug of ours can defeat it. **VERIFY the exact limit options the
     provider offers before relying on this** — I have not confirmed them.
     Do this FIRST: it is independent, costs nothing, and turns everything
     below into a tuning question rather than a safety one.
  b. **Stop when real money actually spent today hits the cap.** Settled
     cost only. No estimate, so nothing to be wrong about.
  c. **Stop when one session exceeds N calls.** This is the real defence
     against the runaway loop that burned money in August — a loop is
     defined by call COUNT, not call price, and counting cannot be wrong
     about a rate.

**Why the current design has to go rather than be retuned.** It reserves
money before each call at a price it has to guess. The token estimate was
fixed 2026-08-28 (`3153c87`, merged and live) — but the PRICE per token is
still the pinned worst-case rate, and real calls settle at a median 0.38x
of it. So it holds ~2.6x what it spends and stops the desk on money that was
never spent.

**The evidence it is net negative.** Measured spend is ~$1/day against a
$2.75 ceiling. This guard has never once prevented a real overspend. On
2026-09-02 it stopped the desk THREE times in one hour — once on a durable
latch needing a manual reset, twice on a projection ($2.69 projected against
$0.55 actually spent). A guard that causes more outages than it prevents
losses is costing money, not saving it.

**The objection, and why it does not hold.** A settled-cost check is
lagging: you only know after the call returns. But the maximum overshoot is
ONE call — under a dollar. Weigh that against a desk switched off for a day.

**Do not price reservations at the cheap flex rate as a shortcut.**
`allow_fallbacks` is on, so a saturated flex tier lands on the full rate and
the reserve would under-cover exactly the dearest outcome. That is the trap
that makes this a redesign rather than a one-line change.

Sequence with item 17 — both are about a guard that stops the desk for
reasons that were never true, and (a) above removes the need for the latch
that item 17 is about.



The only real-money item on this page. Measured 2026-09-02 over a clean
window (2026-08-27 to 09-02, the first window uncontaminated by the runaway
loops).

Actual spend is **$0.73–$1.14/day against a $2.75/day ceiling** — comfortable.
But **the portfolio_manager seat is $4.25 of $4.56 — 93% of the entire bill,
on 35 of 153 calls.** The eight seats moved to Google-direct on 08-31 now cost
$0.00. The whole remaining bill is one seat's model choice.

**The defect:** before each call the desk sets aside money to cover it, priced
at the pinned worst-case rate. The seat's real cost is a **median 0.38x** of
that across 32 calls — roughly **2.6x over-reserved**. So the budget looks
spent long before it is, and the desk stops trading on money it never spent.
On 08-28 a $1.91 hold went against a call that really cost ~$0.25, and the
response at the time was to raise the daily ceiling 1.80 → 2.60.

**That is the part that matters: a bad estimate quietly loosened the real
protection.** The ceiling was raised to accommodate spending that was never
happening, so the guard is now weaker than it was designed to be, for a
reason that was not true.

**Why the obvious fix is wrong.** Reserving at the cheap flex rate looks
right and is not: `allow_fallbacks` is on, so a saturated flex tier lands on
the full $5/$30 rate, and the reservation would then fail to cover the
dearest outcome — trading a false alarm for a real overrun. Saves $0/day and
costs ceiling integrity.

The genuine options are (a) turn off fallbacks for this seat so the cheap
rate is the true worst case, trading uptime for accuracy; (b) lower the daily
ceiling back toward 1.80 now that the phantom charges below are fixed; or
(c) move the seat, which is the standing owner decision above and the largest
saving available. **Do not do (a) or (b) blind — measure a week of real
settled cost against reservations first.**

Related and already fixed on the same branch: a retry that succeeded after a
refused first attempt was charged for BOTH (over-charged $0.0223 total —
pennies, but it is the mechanism that put $1.90 of imaginary spend on the
ledger and darkened the desk twice in one day), and a pricing gap that could
have latched the desk off permanently with no way to self-heal.

**Two found and deliberately NOT fixed, reported rather than actioned:**
a session killed mid-call at day's end leaves a reservation that the NEXT
morning charges and latches on — so the day it darkens is not the day it
broke (never observed; read from code). And cache hits record no cost, so
the alert prints `cost: $?.??` and hides the real figure.

**15. We cannot tell a stale price from a live one — ABANDONED WORK EXISTS.**

Every price the desk uses is currently just a number. Nothing records which
feed it came from, when the MARKET observed it (as opposed to when we
fetched it), or whether it is fresh enough to trade on. Retrieval time
currently makes an old observation look current.

This is upstream of at least three items already on this list: sizing that
rounds to zero (item 5), the quote rejected as 14.6% off reference (item 9),
and a blind data day that looks identical to a quiet one (item 11). It also
bears on the paper-only IEX feed caveat that must be revisited before live
capital.

**~450 lines already exist on `rescue/price-provenance`** — rescued from the
dev account 2026-09-01, never reviewed. It tags each price with its kind,
provider, feed, market-as-of, retrieved-at and a freshness classification.
**Health warning: it is a HALF-APPLIED PATCH.** Two `.rej` files are
committed on the branch, meaning chunks failed to apply and nobody went
back. Treat it as a strong starting point, not as work to merge.

**16. The afternoon spending reserve was built and never connected.**

37 lines on `fix/dollar-based-session-cap` (2026-08-29), whose own commit
message says "NOT wired in". It reserves budget for the afternoon rather
than letting the morning spend the day's allowance.

Belongs with item 14, which the owner has PARKED until the model question is
settled or the desk goes programmatic. Recorded here only so it is not
rediscovered a third time.

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

  a. **A transient infrastructure fault latches like a budget breach.**
     "I cannot read the budget" and "I am over budget" are different events
     and must not share an outcome. The first should retry with backoff and
     only latch if it persists; the second should latch immediately. Today
     both go straight to the durable latch on first occurrence.
  b. **The alert about the latch can fail, and then nothing else happens.**
     A notification failure is currently terminal and unrecorded. It must be
     persisted and retried, and escalate to a second channel — an alert path
     with no fallback is not an alert path.
  **CHECKED 2026-09-02 — the heartbeat tests the PIPE, not the DESK. Half
     of item (c) is already built; do not rebuild it.**
     `quant-agent-alert-heartbeat.timer` fires daily at 10:15 UTC and logs
     `alert channel PROVED (stage=delivered)`. It ran clean on 09-01 and
     09-02. But it proves only that a Telegram message CAN be sent — it says
     nothing about whether the desk did any work. It would report the
     channel healthy while the desk sat frozen all day.
     **So the sending half exists and works. What is missing is the
     "has anything happened" half** — no completed session in N scheduled
     windows, no proposals produced, no run recorded. Build that and reuse
     the proven channel.
     **And note the gap it does not close:** on 2026-09-02 the heartbeat
     proved delivery at 10:15, and that same afternoon the cost-circuit
     alert still failed to send ("cost-circuit unavailable alert was not
     delivered to Telegram"). A provable channel does not mean every alert
     PATH uses it correctly. Test the paths, not just the pipe.

  c. **Nothing watches for SILENCE.** Every alarm we have fires on an event.
     None fires on the absence of events, which is exactly the shape this
     failure takes. `quant-agent-alert-heartbeat.timer` exists on the box —
     **verify whether it actually detects a latched circuit or only a dead
     process. Do not assume it covers this; it did not shout today.**

**Proposed fix, in dependency order:** (c) first — a heartbeat that alerts on
"no completed session in N scheduled windows" catches this failure AND every
future one shaped like it, including ones we have not imagined. Then (a),
which stops the latch firing for reasons that do not deserve it. Then (b).

**Why (c) leads:** an alert that fires on a known failure can itself fail, as
it did here. An alarm on silence cannot be defeated by the thing it watches
going quiet — that IS its trigger. Owner's framing, and it is right: relying
on alerts firing correctly is a recipe for disaster.

Related: `qamc-openrouter-pricing-spof` records the same latch reachable via
a stale price list. That path was fixed 2026-09-02; **this one was not** —
the latch itself is the shared hazard, not any single route into it.

**18. 70% OF WHAT THE MODEL READS IS EARNINGS PROSE — MEASURED 2026-09-02. Owner made the audit priority one; this is its first result.**

**Numbered 18, ranked first in practice.** It was written as "0" to sit
above item 1 without renumbering the other twenty, and that broke the
board: `scripts/status_board.py` reads these numbers as the queue's ranks
and `tests/test_status_board.py` requires the ranked list to start at 1.
18 was the free slot in the existing sequence, so nothing else moved. The
owner's instruction stands regardless of the number — this is the audit he
made priority one, and items 19-21 are explicitly secondary to it.

**The finished prompt was rendered and measured for the first time.** Nobody
had ever read it. It is **199,646 characters — roughly 50,000 tokens** — and
the breakdown is not what anyone assumed:

| section | chars | share |
|---|---:|---:|
| **Earnings Analysis (SEC filings, ~35 companies)** | **140,107** | **70%** |
| Technical Analysis Reports | 17,409 | 8.7% |
| Independent Source Agreement (the Step 5 ceiling) | 11,902 | 6.0% |
| Canonical Evidence Registry | 6,870 | 3.4% |
| Macro Analysis | 5,799 | 2.9% |
| News Intelligence | 3,248 | 1.6% |
| **Deterministic BUY Eligibility — which stocks may be bought** | **853** | **0.4%** |
| Proposal Conversion (what keeps getting refused) | 69 | 0.03% |

**The list of what the desk is actually allowed to buy is 853 characters,
sitting after 140,000 characters of SEC filing summaries.** The record of
what keeps getting blocked is 69 characters — empty, because the book was
wiped the same day it shipped (see item 1).

**This is a plausible mechanism for the familiarity bias, and it costs
nothing to test.** The biggest, most-covered companies have the longest
filings and the most earnings prose. A prompt that is 70% earnings text is
therefore 70% dominated by exactly the famous names the model keeps picking.
Three attempts to fix that behaviour with WORDS all measured as no-change —
because the words were a rounding error next to the volume.

**It also explains the bill.** ~50k tokens per call is why the
portfolio_manager seat is 93% of LLM spend (see item 14).

**ROOT CAUSE OF THE EMPTY EARNINGS REPORTS — FOUND IN THE LOGS 2026-09-02.**

`_extract_key_sections` in `src/data/earnings.py` is failing to find the
sections it looks for. The production journal says so directly, 12 times in
7 days:

```
Structured extraction too sparse (965 chars); falling back to truncated
full text (184067 -> 30000 chars, slice @ 138000)
```

**It recovers 965-1,614 characters of signal from a 184,000-character
filing.** That is not "sparse", it is a failed match — the section headings
it keys on do not match how these filings are actually laid out.

It then falls back to a 30,000-character slice. That fallback was ALREADY
repaired once (an R6 audit found 58 cases where a naive first-N slice fed
the LLM nothing but XBRL labels), and it now seeks the densest numeric
region instead. So the fallback is sane — but it is a blind slice of a
document nobody parsed, and roughly **20 of 67 reports still come back
mostly `[UNSOURCED]`.** Call it a ~30% failure rate.

**Fix the matcher, not the fallback.** The fallback is a safety net that is
now load-bearing, which is why the failure is quiet — nothing errors, the
report is simply empty and gets couriered to the PM anyway.

**Sequencing, and it matters:** repairing this ALONE makes the prompt worse
(see the transcription problem below) — more recovered text means longer
forms. Fix the analyst's OUTPUT SHAPE first so it returns a conclusion, then
fix this so the conclusion is drawn from real data. In that order.

**Cheap check nobody has run:** the log line prints the recovered length
every time. Alert when it is under a threshold instead of logging at INFO
and moving on. A 965-character recovery from a 184k filing should be loud.

**THE ANALYSTS DO NOT CONCLUDE — THEY TRANSCRIBE. Owner's correction, and it is the bigger failure.**

I first reported this as a data-quality bug (filings truncated, fields
empty). That is real but it is the SMALLER problem, and the owner caught
what I missed.

Each of the 67 earnings reports is a FORM, not a call: eight extraction
fields — filing metrics, guidance, strategy, competitive positioning,
strategic risks, operational risks, strategy consistency, data quality —
followed by ONE line of actual judgement (`Analyst synthesis: neutral
(low)`). ~1,400 characters to deliver a one-line conclusion.

**So fixing the truncation makes the prompt WORSE, not better.** Populate
those empty fields and every report gets longer, the prompt grows past 50k
tokens, the cost rises, and the conclusion is buried deeper. Any fix that
starts with "get better filing text" is pushing in the wrong direction.

**The owner's model of the desk is the correct one and we are not built to
it:** analysts research and hand over a CONCLUSION; the PM is the final gate
that weighs conclusions and allocates. Today the PM receives raw extraction
and is expected to do the analysis itself — the analyst's job — which is why
its prompt is 200k characters and why it is 93% of the LLM bill.

**THE "ANALYSTS CAN RUN CHEAPER MODELS" VERDICT IS NOW INVALID. Do not act on it.**

Owner's inference, 2026-09-02, and it follows directly from the above. The
cheap-model benchmark measured the analysts doing the job they CURRENTLY do
— filling in an extraction form from filing text. Extraction is an easy
task and cheap models are adequate at it. **The job they SHOULD do is form
a judgement, which is a different and harder task, and the verdict does not
transfer to it.**

Compounding it: some of that grading ran on filings that arrived truncated,
so part of what was measured was how well a cheap model extracts from
nothing.

Eight seats were moved to Google-direct on 2026-08-31 and now cost $0.00
across 35 calls. **Leave them there for now** — the saving is real and the
current task is genuinely easy. But **re-measure the seat before asking it
to conclude rather than transcribe**, and do not cite the existing result as
evidence it can.

**The full chain, so nobody re-derives it:** filings arrive incomplete →
cheap models fill in a form → 67 forms are couriered to the PM → the PM does
the actual analysis across 200k characters → which is why that one seat is
93% of the LLM bill. Every layer was measured and signed off in isolation
and none is wrong on its own terms.

**The structural fix, ahead of (a)-(d) below:** the earnings seat must
return a call and a short thesis — direction, conviction, two or three lines
of why, and a pointer to the detail — not a completed template. The full
extraction stays retrievable for audit; it must stop being couriered to the
PM by default. Same question applies to every other specialist seat: **check
whether the technical, macro and news seats also hand over transcription
rather than conclusions.**

**Next, in order, none of it needing a paid call:**
  a. Cut or summarise the earnings section and re-render. How small does the
     prompt get, and what does it cost per call then?
  b. Re-run the selection benchmark against the trimmed prompt. If
     `familiarity_bias` moves, the cause is volume, not the model. **This is
     the first intervention with a real mechanism behind it.**
  c. Move BUY eligibility and Proposal Conversion to the TOP. They are the
     binding constraints and they are currently buried.
  d. Then continue the trace: 22 separate inputs feed this one prompt
     (see `_pm_selection_invoke` in `ops/model_policy/scenarios.py`).

**Confirmed for the owner:** there is no hidden channel. The model receives
one assembled text and nothing else. The problem was never that we could not
see what it gets — it is that nobody had looked.

---

**Original audit brief, still open below.**

**This outranks item 1. Start here, before touching anything else.** The
reward:risk work below is real and stays queued, but it is a fix to a gate
whose INPUTS nobody has ever inspected. Do not tune a gate you have not
traced.

Owner's instruction, 2026-09-02: a complete audit of what is fed to the
portfolio manager. **This is the top of the next session.**

**Why it matters more than it sounds.** Every intervention aimed at the
model's BEHAVIOUR has measured as no-change: blinding the tickers (5/5
identical, quality to four decimals), the checkable-catalyst gate (still
picks NVDA and MSFT, `rr_floor_discipline` PASSES every run), prompt
rewrites. Five benchmark runs on the fixed code scored 0.85/0.85/0.85/0.60/
0.85 and failed the SAME check every time — `familiarity_bias`: NVDA and
MSFT taken while GEV, NEE and UNH were passed over.

The model passes every other check. It is following instructions. So the
question is no longer "why does it misbehave" — it is **"what are we
actually handing it, and do our own rules determine an answer at all?"**

**Two parts. Neither costs an LLM call.**

  a. **Trace the pipeline end to end.** Raw data → each analyst → evidence
     scoring → eligibility filter → prompt assembly → what the model
     literally receives. Every stage can drop, reshape or re-rank
     something, and NOBODY HAS WALKED IT. Produce the actual list of stages
     and what each one changes. **Do not summarise from the code comments —
     they have been wrong repeatedly. Render a real prompt from a real
     fixture and read what is in it.**
  b. **Write the selection rules as plain Python and run them on the same
     fixture** (`ops/model_policy/fixtures/run_64290730_pm_input.json`,
     the day already measured). Two possible outcomes, both valuable:
       - The rules produce a clear pick → **the rules are complete, and the
         model is not needed for this step.** Use the code.
       - The rules do NOT determine an answer — names tie, nothing clears
         the floor, "best" is undefined → **we have been blaming the model
         for a choice we never specified.** The gap is where NVDA enters,
         and the fix is to specify it, not to instruct harder.

**The owner's framing, and it is the right one:** if the rules are complete,
the model is not needed here at all.

**Do not repeat the discredited claim.** "The model already knows what it
will choose before it reads anything" was asserted twice in this project and
corrected twice. Blinding disproved it. Anyone picking this up should treat
a model-behaviour explanation as the LAST hypothesis, not the first.

**Also queued from the same conversation:** setting a spend cap on the
OpenRouter key is MINE to do via browser access, not the owner's — check
first whether the provider exposes it via API rather than a dashboard. See
item 14(a).



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

**21. Alerts must be their OWN message, and must not rely on colour — owner's spec, 2026-09-02.**

**Two requirements, both from the owner, both cheap.**

**a. A FAILURE ALERT IS A SEPARATE TELEGRAM MESSAGE. Never appended to, or
bundled inside, a normal run summary.** *"I don't want alerts in the same
message as the normal runs... so it doesn't get lost in the run messages."*
A skipped run, a degraded seat, a halted circuit — each gets its own
message. Routine session output stays routine. The recipient is one person
and the failure must not arrive as a paragraph inside a wall of normal text.

**b. SEVERITY MUST NOT BE CARRIED BY COLOUR.** The current alerts open with
🔴 (critical) and 🟠 (hold) — see `src/notifier.py`. **The owner is
red/green colour blind**; red, orange and green circles are effectively
indistinguishable, and today colour is doing all the work. This is recorded
in `rex-colour-vision` and the alert format ignores it.

Use instead:
  - **Distinct SHAPES**, not coloured discs: 🛑 stop / ⚠️ warning / ℹ️ info.
  - **The first word states severity in plain text** — `HALTED`, `SKIPPED`,
    `DEGRADED` — so the message reads correctly even with no emoji at all.
  - **Repetition marks the top level**: 🛑🛑🛑 reads as urgent at a glance
    without requiring any colour judgement.
  - **Say what to do, not only what broke.** "SKIPPED 10:00 run — earnings
    coverage 31/67, retrying 10:30, no action needed" is a different message
    from one that needs a manual reset, and they must not look alike.

**Check `src/alert_watchdog.py` BEFORE building anything new** — it exists
and has not been read. It may already cover the "nothing arrived" case that
item 17 asks for. Do not build a second watchdog next to a working one.

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
AND BUILT 2026-09-01.** The owner answered the question this item was raised
to ask: the sector cap measures **concentration, per side**, not net
directional exposure. Long sector exposure and short sector exposure are now
tracked independently, each against the same limit, and neither offsets the
other — *"A long and a short in the same sector is not a hedge... We are
trading opportunities."*

Gross summing was considered and REJECTED, because it would block a legitimate
pair trade (long the leader, short the laggard in one hot sector). Ratified as
spec §12.2 and implemented the same day; the build record, the four
implementations it reconciled, and the yfinance sector-coverage exposure it
did NOT fix are all in `docs/QAMC_REMEDIATION_SPEC.md` §12.2.

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

Two owner decisions ratified 2026-08-30, recorded as ratified, not inferred:

- **The inverse exchange-traded funds stay in the tradeable list**, not
  retired. Rex: *"they have their uses they can still be useful for some
  situations."*
- **Paid news sources are refused, permanently.** Rex: *"not worth paying for.
  There has to be other sources available."* Free sources only — closes the
  open question the previous session left.


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

- **Phase 3 runs before the rest of Phase 2.** The spec's stated order is
  Phase 2 → Phase 3. The owner reordered it on the evidence below. Phase 1
  already shipped `expected_horizon_sessions`, which is what Phase 3's pace fix
  needs, so nothing blocks it.
  *Evidence:* on 2026-08-26/27 the book went to fully flat. The evening
  reviewer graded its own exits — EPD (5d) **premature**, "thesis may have only
  been temporarily paused"; MRVL (5d) **premature**, "thesis intact... closed
  position anyway". Two of the last three exits cut intact theses. Sizing
  trades more precisely does not help when they are cut on day 5 against a
  self-referential pace metric.
- **§3.5 resolved: leave the reviewer's model alone, fix the scenario instead.**
  The spec's premise — `position_reviewer` runs "the weakest model in the
  stack" — is contradicted by `ops/model_policy/results/merged.json`:
  `google/gemini-2.5-flash-lite` scores `quality_min 1.0 / quality_mean 1.0`
  at `midday_exit`, tied with `openai/gpt-5.5`,
  `deepseek/deepseek-v4-pro-0813`, `qwen/qwen3.7-flash` and
  `qwen/qwen3-235b-a22b-2507`, and scores 1.0 on every scenario it was ever
  measured on. `gpt-5.5` costs ~84x more per review ($0.0927 vs $0.0011) for
  no measured gain. The real EPD/MRVL failure was a broken pace metric and
  missing memory, not model weakness. The honest gap is that `midday_exit`
  ties five of twelve candidates at 1.0 and therefore does not discriminate;
  the owner chose to build a scenario that does (the EPD shape: metrics
  improved, reason claims stalling) rather than pay 84x on faith. Routing
  unchanged.
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
- **Fractional shares are IN — this decision was REVERSED on 2026-09-01**
  (spec §11.1) and BUILT the same day. The original reasoning rested on the
  stop being an OTO bracket leg; it has not been one since 2026-07-16, so the
  fill→stop window fractional was said to introduce already existed on every
  entry. Owner: *"if the gap is brief upon entry, then it's irrelevant to
  eliminate that option."* Behind `execution.fractional_enabled` (default on),
  gated on a broker-confirmed `fractionable` flag that fails closed, with the
  three required stop-placement guards. Recovers the whole-share rounding tax
  (V wanted 6%, got 3.84%).
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

**Confirmed working as designed, not defects:** running with no `--source-data` at all correctly fails closed at the pricing gate ("the cost circuit cannot confirm current rates offline and will suspend paid analysis") before any model call, rather than proceeding with an unbounded cost; and deleting every row from a sandbox copy's `positions` table (simulating a flattened book) correctly surfaced the trade-ledger-vs-broker stop-out reconciler declining to guess ("recording nothing rather than guessing") for all six affected symbols, with the session still completing end-to-end rather than crashing on the inconsistency.


#### SMALLER, RECORDED
- `db_reads.get_recent_agent_logs` uses `SELECT *` and `GET /agents/{agent_name}` returns 20 rows; PM prompts run 13KB-190KB, so that response could reach several MB. Harmless today because nothing in `frontend/src/` calls it.
- After the constructor rejects a BUY for reward:risk, it logs a second confusing line — "no valid stop below entry (stop=None)" — because the None propagates. Cosmetic.
- OneCLI: OpenRouter spend from a live rehearsal would be real money on the same account, but the rehearsal runs its own cost-circuit database, so production would under-count the true daily bill.
- OneCLI: production's Alpaca secret matches `*.alpaca.markets`, which also covers the paper host, so both credential sets match the same address. The gateway fails closed on the ambiguity. Narrowing the production pattern risks breaking live credential resolution and was deliberately left for the owner.

- `feat/news-dedup` — still unmerged; disposition being decided separately.

#### THE NEW STOP RULE REJECTED FOUR BUYS ON ITS FIRST DAY — RESOLVED 2026-09-01
On 2026-08-28 the reward:risk floor rejected four candidates (CRM 0.39, ONDS 0.78, MP 0.80, NVDA 1.30), which asked whether the targets were too conservative or the stops too wide. **Answered: neither — the targets were not measurements at all**, so those ratios never meant what they appeared to. Do not cite them as evidence about stop width. That day's zero trades had two causes, not one: the cost circuit blocked the morning, and separately these four were refused on payoff.

#### RECURSION FAULT IN THE BAR FETCH
`broker.get_bars failed for DSPC: maximum recursion depth exceeded` — 14 times on 2026-08-28, all for the same symbol. Contained (the call returns an empty list rather than crashing the session) but it is a real fault, not noise. DSPC is a delisted warrant, so the trigger appears to be the fallback path handling a symbol with no data.

#### DELISTED WARRANTS REACHING THE DATA LAYER
Five symbols returned "possibly delisted; no price data found" on 2026-08-28: DSPC, SXTPW, NRSNW, LIMNW, ERNAW. All are warrants. They should not be reaching a bar fetch at all — this is universe/admission hygiene, and it is also what triggers the recursion fault above.

#### EARNINGS CACHE ASSERTS PRICE-DERIVED VALUATION
Repeated on 2026-08-28 for MTZ and KO: the cached earnings analysis asserts price-derived valuation (P/E, market cap) in `valuation_context`, but the agent was given filing text only. Pre-existing; logged as a warning and otherwise ignored.

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

