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

**START HERE — 2026-09-01 handoff. Everything below is a POINTER; the detail
lives in the files named and is not repeated.**

**Read first, in this order:**
1. `docs/QAMC_REMEDIATION_SPEC.md` **Phase 12** — four decisions Rex ratified
   2026-09-01. Nothing is implemented. This is the work.
2. Then **Phase 10** (per-trade risk verdict, macro sizes rather than selects,
   concentration scales, target from levels) and **Phase 11** (fractional
   sizing, 2.0x margin).
3. `docs/OUTCOME.md` — "This is a trading desk, not a retirement portfolio".
   Read before touching any risk rule; it decides which rules are legitimate.
4. `docs/INCIDENT_HISTORY.md` — what already broke and was fixed. Append-only.

**Why it matters:** on 2026-09-01 the desk reviewed 38 qualified signals and
placed zero trades. Root cause is Phase 12.1. It is still unfixed.

**Owner instruction: ship everything in ONE pass, tonight.** Deliberate
acceptance of change risk (Phase 12.4) — the desk cannot trade at all, so a
partial fix leaves it that way. **The rehearsal rig is the mitigation and must
run against the merged result before deploy.**

**Six branches, all pushed, none merged, none deployed:**

| branch | spec | tests |
|---|---|---|
| `fix/risk-verdict-per-trade` | 10.1 | 3853 pass |
| `fix/concentration-scales-size` | 10.3 | 3848 pass |
| `fix/target-from-structure` | 10.4 | 3850 pass |
| `feat/golden-pm-prompt` | PM prompt rewrite + Phases 10/11/12 in the spec | 3848 pass, 1 unrelated |
| `feat/telegram-run-deeplink` | symbol links + company names in alerts | full suite green |
| `rescue/price-provenance` | rescued 11-day-old work — **parked, NOT mergeable**, reference only |

**Merge hazard, read before merging anything:** spec Phases 10/11/12 exist ONLY
on `feat/golden-pm-prompt`. The three fix agents could not see them and each
wrote its own reconstructed Phase 10 into the spec. **Merge
`feat/golden-pm-prompt` FIRST, then reconcile the others' spec sections against
it — the owner-ratified text is the one on that branch, not the
reconstructions.**

**The model-benchmark results are STALE — do not choose a PM model from them.**
Every score in `ops/model_policy/results/*2026-09-01*.json` was measured against
the OLD prompt, which is what produced the restrictive behaviour, so the scores
are entangled with it. **The rig does not need rewriting, only re-running** —
`benchmark_models.py` drives the real agent class, which reads
`config/prompts/portfolio_manager.md` from disk, so re-running after the
rewritten prompt lands tests the new prompt automatically. Keep the DIAGNOSIS
(gpt-5.5 picked SPY in 5 of 5 runs and never proposed more than 2 positions —
the most literal rule-follower, hence the most timid under a prompt full of
"never"); discard the RANKING. Expect absolute scores to rise across the board
if the rewrite works, so compare rankings, not numbers. **Also re-examine the
`actionable_book` check itself** — if the new prompt legitimately produces more
targets, that check may now be too easy to pass and stop discriminating.

**Universe expansion and pruning — DESIGN AGREED WITH THE OWNER 2026-09-01,
never written down until now. Not built. Do not redesign it; implement this.**

Today the 101-symbol universe is a hand-written list. Symbols CAN be added
dynamically but only narrowly: up to 3/run via SEC Form 4 smart-money
admission, and up to 3 per seat / 6 total per run via Phase 9 nominations.
**Nothing ever removes a symbol.**

*Admission screen.* The criteria already exist under `smart_money:` but are
used only to admit outsiders, never to BUILD the list. Reuse them, with these
corrections:
- **A year of price history minimum.** `min_external_history_days: 20` is
  badly wrong — the analysis computes a 200-day moving average and its slope,
  so a symbol admitted at 20 days produces blanks in the fields the analyst
  leans on hardest.
- **Screen on bid-ask spread, not the $10m dollar-volume floor.** $10m/day is
  far stricter than a ~$10k account needs, and it screens the wrong thing:
  what costs money is the spread.
- **Require shortable / easy-to-borrow.** Half the point is bearish trades.
- **Exclude warrants, units and rights.** Delisted warrants already reached
  the data layer once and caused a recursion fault.
- **Exclude names under a pending takeover** — an acquisition target trades
  flat at the deal price, so every technical signal becomes noise while
  looking like a calm uptrend.
- **Volatility ceiling.** A name so wild that its structural stop is enormous
  fails reward:risk by construction — screen it at the door rather than
  rejecting it daily.
- **Minimum company size**, so a micro-cap cannot qualify on one freak volume
  day.
- **NO earnings-date requirement.** Considered and REJECTED by the owner: ETFs
  have no earnings and ~20 of the universe are ETFs. Reducing their size for a
  missing date is equally wrong. Dropped entirely.
- **No maximum price** — Phase 11.1 turned fractional sizing on (built
  2026-09-01), so a $500 share is no longer unsizeable.

*Pruning.* Nothing does this today.
- **Re-screen weekly.** Fail once -> flagged. Fail twice consecutively ->
  removed. One bad week must not evict a good name.
- **NEVER remove a symbol currently held.** That cuts a live position off from
  analysis while its stop still sits at the broker — unwatched but real.
- **Immediate removal, no second chance,** when the broker reports the asset
  does not exist, or it is delisted or permanently halted.
- **Log every addition and removal with its reason, and summarise them in the
  morning alert.** The owner must never discover the universe changed by
  accident.

*Cost.* Scanning is free — the specialist seats run on the free Gemini tier
(measured 2026-09-01: `tech_analyst` processed 307,754 input tokens at $0.00).
The cost is the PM reading a longer candidate list. **Cap how many screened
candidates reach the PM**, the same way `NominationConfig` already caps
nominations, so the bill is a number that is set rather than one that emerges.
Untested risk: the free tier is rate-limited and a much wider scan may hit it.

**Phase 11 status, 2026-09-01, after verification and merge — this replaces
both earlier claims, which had each gone stale in a different direction.**

**MERGED and verified on `integration/ship-2026-09-01`:**
- **Margin interest tracker.** Measures only, never gates. Review found and
  fixed a real defect: it fast-exited whenever `allow_margin` was false and so
  never read cash at all — unsafe, because covering a losing short is exempt
  from the cash-only block and can carry a genuine debit balance.
- **The sector cap no longer switches itself off.** Pre-existing rot,
  reproduced against pristine `af266de`: a holding at 85% of equity plus a 10%
  order, both in an unresolved sector, produced ZERO violations — 95% pooled
  straight past the 90% ceiling. 80 of 101 universe symbols depend on a live
  network lookup with no offline fallback, so the cap was inert for most of
  what the desk trades. Verified NOT over-broad: cash-park never reaches the
  code, index and sector ETFs resolve from static tables offline.
- **Phase 11.2's ceiling and ladder.** 2.0x gross cap at the sizing and
  execution gates; the ladder stepping 2.0/1.5/1.0/0.5 on peak-to-trough
  drawdown. Both owner gates verified BY MUTATION, not by reading the tests:
  breaking the ladder so it never steps fails 24 of 55 tests, making it trim
  to make room fails 6, making the rung compound fails 2. The ladder runs in
  the session preamble BEFORE any agent, from account state alone, so a blank
  PM response cannot skip it — which is not hypothetical, a benchmark run that
  night had one candidate model return an empty book on 1 run in 5.

- **Phase 11.1, fractional sizing and its three stop guards — MERGED.** — fractional
sizing and its three stop guards. No path leaves a fractional position
silently unprotected; the owner alert fires unconditionally after every
protection attempt. Review found and fixed a real gap: the 30-minute sweep's
repair belt used a bare single-shot stop submit with no retry and no
whole-share fallback — the same weakness the entry guard exists to close.

**`allow_margin` REMAINS `false`.** The ceiling was built before borrowing is
enabled; that was the sequencing requirement and it held. The flag and the PM
prompt's exposure table move together, later, after the rehearsal rig has run
against the merged result.

**Still to build:** the wider universe with pruning (design recorded below, no
code), and Phase 10.2 deterministic analyst weighting.

**Open, documented, not blocking:** the live rung is not on Mission Control.
`src/api/` may never import `src.risk` (ratified guardrail,
`tests/test_api_safety.py`), so the dashboard shows the standing cap only; the
session alert is the sole operator surface until the measurement functions
move out of the risk package. Also worth the owner's eye: the ladder
introduces a SECOND drawdown measure (peak-to-trough against a 252-day
high-water mark) alongside the existing rolling-window one. One table, one
resolver, so the mechanism is not duplicated — but it adds a measure rather
than reusing the existing one.

**Phase 11 was MISSING from this line until 2026-09-01 and that caused a real
scope error.** A session read this list, built Phase 12 and the branch merges,
and correctly believed Phase 11 was out of scope. Verified by search across
every branch: no fractional sizing anywhere, and **no gross-exposure cap of any
kind exists** — `max_portfolio_risk_pct` bounds AT-RISK capital, not gross. So
11.2 ADDS a ceiling where none exists; it is a tightening, not a loosening, and
it must land before `allow_margin` is turned on.

**Phase 12.1/12.2/12.3 are now IMPLEMENTED** (2026-09-01) along with the five
open branches. Phase 10.2 — computing analyst weighting in Python so no seat
dominates by prompt position — is also still unbuilt.

**Baseline:** `pytest tests/ -q` gives 2 pre-existing failures in
`tests/test_rehearsal_reproduces_cost_ceiling.py` — they read live production
state and pass in CI. Anything else failing is yours.


**READY TO DEPLOY, WAITING ONLY ON THE MARKET CLOSING — do this first.**
**MERGED 2026-09-01 into `integration/ship-2026-09-01`** — verified with
`git merge-base --is-ancestor`. This entry previously said "not merged",
which was wrong and was repeated onward before being checked. **What is
still owed is the DEPLOY, not the merge.**

Branch `feat/telegram-run-deeplink`. Makes
every ticker in a Telegram alert tappable through to that company's quote
page. Full suite green: 3837 passed, 1 skipped, and only the two known
`test_rehearsal_reproduces_cost_ceiling.py` failures that read live
production state.

It was NOT deployed on 2026-09-01 for two specific reasons, neither of
which is "later, vaguely":
1. The market was open (12:44 ET) with the midday session 16 minutes out.
   Deploying restarts the trading service; mid-session is the wrong moment.
2. It touches `main.py` and `src/scheduler.py`, not only message text — so
   the rehearsal-rig rule above applies and the rig has not been run
   against it yet.

**Superseded 2026-09-01 22:00 UTC:** the merge is done, so the remaining
order is: run the rehearsal rig against the merged integration branch, wait
for CI, deploy, restart, confirm the served bundle matches disk.

**Timing constraint, measured from the live timers rather than assumed:** the
production evening session fires at 23:30 UTC and several timers fire with
it; the morning run is 13:00 UTC, half an hour before the open. Deploying
restarts the service, so the window is AFTER the evening session completes
and BEFORE 13:00 UTC.

Rex asked directly: "who's gonna remember to deploy it? I'm not gonna
remember." This entry is the answer. It stays here until it is deployed,
and whoever picks this file up next is the one who owes him the deploy.

**Known and deliberately NOT fixed on that branch** (do not let it block
the merge): the alerts still link to the Mission Control home page rather
than to the specific run. The cockpit has no URL routing whatsoever — no
router package, no query or hash parsing anywhere in `frontend/src`, and
`selectedRunId` is in-memory `useState` only. Deep-linking needs ~15 lines
in `App.tsx` to read `?run=<id>` on mount for same-day runs, and more than
that for older ones, because there is no UI to view a non-current run at
all. Separately: `_append_company_identities` in `src/notifier.py` is dead
code for real trading alerts — `src/pipeline.py` emits `executed`/
`no_trades`, which route to `trader_feed.py`'s own formatters, and those
never call `CompanyProfileStore`. That is why company names have never
appeared in an alert.


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

**Historical — the 2026-08-27 evening deploy.** As of that evening,
production was deployed at `46b2029` (merge of PR #113,
`feat/pm-flex-routing`), superseding `32c174b` (PR #114, the deploy-drift
alarm, merged on top of `e6ada88` — PR #113 carries `32c174b` in its own
merge history). Phase 3 (exit rework), the execution limit fix, the
deploy-drift alarm, Phase 2b risk-based sizing, the stop-width fix, the
OpenRouter flex-routing change and the intraday un-blindfolding were all live
as of that deploy: seven positions open, all with broker-resident stops,
`paper: true`, daily LLM budget raised to $2.75. (Earlier same-day notes had
claimed `18dd4bc`, then `e6ada88`, as the deploy SHA; `18dd4bc` was never
actually on the box, and `e6ada88` was superseded within the same session —
recorded here only for the forensic trail, not because it matters now.)
**None of this paragraph describes current state** — use the command above.

**Historical — 2026-08-27 night, nothing further deployed, deliberately.**
The sizing and stop-width change (`3dff940`, part of the deploy above) had
its first live session 2026-08-28 09:30 ET, and the operator chose not to
confound that read with another deploy that night. PR #115 (earnings fix)
and PR #116 (shorts Stage 1) were both open, reviewed, and intentionally left
undeployed that night. **Whether they are deployed now is a different
question — check reality, above, and see the ordered backlog below.**

**Historical — deploy-drift alarm (PR #114, `9eef617` + `38a985c`), landed in
the 2026-08-27 deploy.** `scripts/check_deploy_drift.py` plus
`quant-agent-drift-check.timer` (Mon-Fri 08:45 ET) alerts over Telegram when
the box's deployed HEAD falls behind `origin/main`. Built because PR #111 sat
merged-but-undeployed for eight hours with nothing catching it. Verified
firing.

**Historical — also in the 2026-08-27 deploy, PR #113 (`feat/pm-flex-routing`,
merged as `46b2029`):**

- `75c0233` Phase 2b risk-based sizing + the correlation-aware risk budget
  gate — **the highest-consequence change in this deploy.** It decides how
  much money each trade may lose. `b712f4c` and `3dff940` land on top of it,
  same branch: the constructor now clamps to the risk engine's 20%
  single-name ceiling instead of proposing orders it hard-blocks, and entry
  stops sitting inside ordinary volatility get pushed out to a
  regime-and-setup-scaled ATR floor (`risk.min_stop_atr_multiple`) — a
  widened stop that drops reward:risk below 1.5 rejects the trade outright.
  Measured against the real book: MSFT's stop went 2.4% → 7.0%, VLO 4.5% →
  9.2%, OKLO 7.7% → 24.7%, and 0.5/1.0/1.5% conviction now produces
  7.1/14.2/20.0% positions instead of clamping all three to 20%. First live
  session under this change is 2026-08-28 09:30 ET.
- `fb88e08` the intraday PM un-blindfolding.
- `16f6535` the PM's OpenRouter `openai/flex` endpoint routing.
- `6b7af86` the Mission Control `input_message` surface, `cdb387b` the
  sector-stance vocabulary + `TypeError` crash fix, `002095c` risk-sized
  targets reappearing in the cockpit funnel, `300ea14` + `6f897a1` + `55f0e05`
  the benchmark-harness repair and its guards, and the `docs:` commits.

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

Two halves, in this order:
  a. **Fix the geometry** so every R/R computation uses identical stop
     geometry and a level-backed stop is honoured however tight. The 1-ATR
     guard upstream already prevents a stop sitting in pure noise, so the
     floor is redundant in the level-backed case and destructive in it.
     Dispatched 2026-09-02 on `wt/rr-geometry`.
  b. **Then** replace the 1.5 hard gate with the already-ratified weighted
     composite score (see `qamc-weighted-scoring-architecture`). Do NOT do
     this before (a) — scoring a fabricated ratio more gently is not a fix.

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

**5. Allocation rounds to zero shares — 3 of 68 (4%). DEFECT (probably).**

An account-scale artifact: a ~$9.9k account against $200+ share prices.
Fractional sizing is now enabled, so **the open question is why this still
rounds to zero** — that needs checking before it is written off as scale.

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
of spend. A clean baseline needs to be re-measured once the current tranche
(flex routing, the `intra_check` fix) is deployed and the circuit breaker
has had a run without tripping.

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

