# QAMC Current Work

## Active finish line

### Session start — read this first

**Run the rehearsal rig before you touch anything that trades — owner
instruction, 2026-08-29, not an agent decision.** His words: it exists so we
do not wait for Monday's market open to find bugs, shutdowns and errors, he
wants it used properly and routinely, and he wants that written where he
looks — the board, `docs/phases.yaml`'s `rehearsal_rig` entry, and here.

- What it is: `ops/rehearsal/` runs a full trading session offline against a
  snapshot of production, replaying recorded model responses. Free,
  deterministic, about 50 seconds. Blocks outbound network at the process
  level, then proves the production database is byte-identical afterward.
  Suppresses operator alerts via `QAMC_REHEARSAL=1` so a rehearsal never
  pages anyone.
- When to run it: this is the default way to find a bug, not a formality —
  run it before deploying anything touching the session pipeline, and after
  any change to the agents, the risk engine, the cost circuit or execution.
- Why this is not optional: a full trading day, 2026-08-28, was already lost
  to a defect a rehearsal would have caught before the market opened.
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

### Ordered backlog — RESUME POINT

**Landed (2026-08-31) — six PRs, all merged and deployed. Nine open defects closed, four more deleted, six new ones found — read this first**

Tonight's audit worked through the thirteen open defects recorded below on 2026-08-30. Full detail and re-check commands for every item are in `docs/phases.yaml`'s `open_defects` entry — this is the plain-language summary.

- The macro event calendar is now half-real. The desk fetches a genuine forward schedule of seven US macro releases (CPI, payrolls, PPI, PCE, GDP, retail sales, jobless claims) from a free government source and shows it to the Risk Manager and the Macro Analyst, and the earnings-date lookup that existed but was never called is now wired in. **Still missing: Fed meeting dates specifically.** No free source publishes those, so both prompts now say so outright instead of guessing. Whether to add the Fed's own free calendar page just for that has been put to the owner — **not yet decided.**
- The price-list refresh gap is closed. A scheduled job now refreshes it twice a day, every day including weekends, and pages over Telegram the moment it starts going stale rather than waiting for the hard cutoff. (Correction to an earlier note: the claim that the box had no scheduled jobs at all was a checking mistake, not a real finding — the box has always had them. The refresh gap itself was real and is now fixed.)
- The evening report's lessons and reminders now actually carry forward: they're saved and read back by tomorrow's trading decisions instead of being generated and thrown away. One of the four fields — the report grading its own prior forecast — is kept for the record but deliberately not fed back in, to avoid the same self-review loop that was cut once before.
- A smaller inconsistency is fixed: the parked cash-equivalent holding can no longer take up one of the limited slots meant for real positions' news coverage.
- Every trade's conviction, requested risk, allocated risk and which model decided it are now visible on the dashboard and through the API, not just recorded internally.
- The rehearsal tool used to test changes offline no longer depends on how stale the live price list happens to be at the moment someone runs it — that dependency mismatched what it was supposed to be testing and could produce a false failure.
- A safety-net test that was supposed to guarantee every outcome prints a plain explanation, but only ever checked itself against itself, has been rebuilt to check against the real code instead. It already found one genuine, small gap in the process (see below).
- Three previously-identical "nothing happened" outcomes inside the mid-day quick check (feature off, already running, nothing found) now report distinctly, so they can be told apart after the fact. All three are and remain harmless.
- Four pieces of dead code confirmed to have zero callers anywhere, including tests, were deleted: an unused local copy of the holdings list, a write-only bookkeeping field, a superseded internal data shape, and four small orphaned helper functions.

**New, found by the same audit, not fixed — ranked by consequence:**

1. **Highest consequence:** the alarm that is supposed to tell the owner a code change never actually reached the live server cannot send its alert — a missing piece of wiring means the notification credentials never reach it. It has never actually needed to fire yet, so this has gone unnoticed. Awaiting an owner decision on how to wire it; not scheduled work.
2. One mid-day failure outcome still has no plain-English explanation and would show a raw internal code instead. Low frequency, tracked, self-flagging if it's missed again.
3. A cosmetic bug in the rehearsal tool: a note about how much slack the price-list safeguard has always reads as "none," regardless of the real setting, because it's checked before that setting is available.
4. A leftover scheduled job still points at a folder from the project's original owner instead of this one.
5. A setup guide referenced by one internal file doesn't exist.
6. Unchanged: full wire-service news coverage still needs a paid subscription the owner has already declined. Not a defect — a closed question.

**Landed (2026-08-30, later) — two fixes plus a close call, all deployed**

- The macro data feed's retry policy has been rebuilt. The old one gave a failing economic-data series one quick second try and then gave up; that is exactly what let one bad three-minute stretch (2026-08-26) lose all nine numbers the macro seat reads, silently. It now tries harder, with a real time limit on the whole job — a minute and a half, not per series — so a slow patch can be ridden out without ever risking a session running long. Six new free indicators were added on top of what was already tracked — a real (inflation-adjusted) 10-year yield, the market's inflation expectation, a 3-month Treasury rate, the dollar's strength against other currencies, investment-grade borrowing costs, and weekly unemployment claims — each checked against the real data source before being wired in. And if any of these numbers fail to come back, the desk is now told so directly, the same way it is already told when the news feed is degraded, instead of quietly reasoning from nothing (PR #162).
- A second, smaller repair: if the mid-day opportunity scan crashed partway through, it used to look exactly like a normal quiet check that found nothing — no error, no signal, nothing for anyone to see. It now says plainly that it crashed, and the rehearsal report counts that as a failure instead of a pass (PR #163).
- A close call, caught in time, not yet fixed: the price list the spending safeguard uses to know what each AI call costs is supposed to refresh itself, but only when a real trading session actually starts one — nothing refreshes it on a clock. Over the weekend it sat unrefreshed long enough to cross the point where the safeguard would have refused to run any paid analysis at all come Monday morning, meaning the desk would have opened and done nothing. It was noticed and refreshed by hand before that happened. Nothing has been put in place yet to stop this from happening again — that is still open work.
- Still missing on the macro side: there is no calendar of upcoming Fed decisions or inflation reports. Asked whether one is coming up, the desk still answers from what the model remembers, not from a real schedule.

**Found (2026-08-30, documentation audit) — ten more open defects recorded, none fixed yet**

An audit raised eleven candidate defects beyond the two already tracked above; ten verified real, one turned out false. All ten are now recorded in `docs/phases.yaml`'s `open_defects` entry as items (c) through (m), ranked by how directly each touches a trading or risk decision — read that entry for the full detail and the exact re-check command for each. In order: (c) the earnings-date lookup meant to ground the risk manager's mandatory event-risk check is wired to nothing, so that check still runs on the model's memory instead of real data; (d) the evening report's discipline-notes / selection-rules / thesis-update / outlook-grading fields are generated by the LLM every night and dropped before they reach storage or any later decision, so the loop the evening report explicitly promises never closes; (e) the evening session excludes the parked cash-sweep vehicle from per-symbol news selection but the two same-day checks earlier in the day don't, so it can occupy one of the capped news slots that would otherwise go to a real position; (f) every trade's conviction / requested-risk / allocated-risk / deciding-model fields are persisted but not exposed anywhere a human can see them, dashboard or API; (g) the rehearsal harness's own test that proves it can reproduce last week's spending-limit failure is quietly coupled to the same OpenRouter pricing-cache staleness already recorded as defect (b) above, so it can fail for a reason unrelated to what it exists to test; (h) a test meant to guarantee every outcome of the mid-day quick check prints a plain explanation is not actually exhaustive — it only checks that its own checklist agrees with itself — though it does not currently fail; (i) several healthy outcomes of the intraday opportunity scan (feature off, lock contention, nothing found) are indistinguishable from each other after the fact (benign, a residual loose end from this month's crash-visibility fix, PR #163); (j)-(m) are lower-consequence dead code found in the same pass: an unused local positions table nothing in production reads, a run-context field written once and never read back, a superseded news-analysis model family kept alive only by tests, and four small helper functions with zero callers anywhere.

**Checked and found NOT to be a defect:** a claim that `docs/STATE.md` pins a specific production commit that is now several merges behind current `main`. Verified live 2026-08-30: production HEAD and `origin/main` are both `6a8694a` — zero merges of gap (`scripts/status_board.py`'s own live `undeployed_merges` reading is 0). Not recorded as a defect.

**Separately noticed while checking the above, not yet acted on (out of scope for this pass):** `docs/STATE.md`'s "Intraday opportunity discovery" section — the file is dated 2026-08-27 at the top — still says the broker layer has no short-selling capability at all today, and that nothing in the codebase tells the Portfolio Manager the inverse ETFs are bearish instruments. Both are now false: shorting went live 2026-08-29, and the inverse-ETF/Portfolio-Manager wiring landed 2026-08-30 (PR #158). Left as-is; flagged here for whoever next edits that file.

**Landed (2026-08-30 through ~15:00 UTC 2026-08-31) — all five items now deployed to production**

- All five ordered items from the 2026-08-29 backlog have shipped. (1) Inverse-ETF longs now count against the bearish exposure ceiling, with a second commit fixing a sign error: shorting an inverse ETF is bullish, not bearish (PR #158). (2) Free per-symbol news feeds are scoped to held positions and candidates instead of universally requested (PR #157). (3) Every trade carries its allocation, conviction, and deciding model pinned at entry; exits label whether they link to an originating decision (PR #159). (4) The rehearsal harness can now read the intraday scan's own outcome report instead of only the top-level status — at the time, one limitation was left in place on purpose: a crashed scan produced no marker, so the session status stayed 'ok' even on crash (production honesty gap, documented but not fixed by design) (PR #156). That gap is now closed too — see "Landed (2026-08-30, later)" above (PR #163): a crash now attaches its own status and reports as a failure. The other limitation from PR #156 still holds: the nested-outcome path is unit-tested but no production replay has actually contained an intraday scan yet (none in live history so far). (5) The desk can now formally argue out disagreements and size trade risk by the number of independent seats that agree: a target carrying an unadjudicated conflict is dropped before grounding (punishment fits offence, single-target drop not session-wide), and risk_allocation_pct is ceilinged by agreement count in the deterministic risk code (PR #160, merged during this audit window).
- To check the live state: `sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1` should show PR #160 merged.

**Landed (2026-08-29) — read this first, supersedes most of what follows**

- Short selling is complete and live: the desk can now open a short and cover
  it, not merely hold one safely, on the same careful caps and gates a long
  trade gets.
- A tool to test a strategy change against real history now exists, though it
  can only check the mechanical trading rules, not what the AI agents
  themselves would have decided — that was never recorded, so it cannot be
  replayed.
- Any research seat, not only the chart analyst, can now bring a candidate to
  the desk's attention. Still missing: the desk formally arguing out a
  disagreement between seats, and sizing a trade bigger when more seats
  independently agree.
- The cost-circuit outage from two days ago is fully fixed, not just
  patched, and its safety backstop now recovers on its own instead of
  shutting a trading mode down for the rest of the day.
- Both defects logged below that were specific to short trades — blind
  performance stats and confused crash recovery — are verified fixed.
- The news desk's free source list was widened, and a feed that had quietly
  stopped publishing while still reporting success was found and removed.
  Real wire-service coverage still needs a paid subscription and an owner
  decision.
- All of the above is merged and confirmed deployed to production as of
  today. Most of "STILL OPEN — 2026-08-29" and "EXECUTION ORDER FOR THE NEXT
  SESSION" below is now done; see their own superseded-notices rather than
  reading them as current.

**Next, in order (set 2026-08-30) — start here**

Two owner decisions ratified 2026-08-30, recorded as ratified, not inferred:

- **The inverse exchange-traded funds stay in the tradeable list**, not
  retired. Rex: *"they have their uses they can still be useful for some
  situations."*
- **Paid news sources are refused, permanently.** Rex: *"not worth paying for.
  There has to be other sources available."* Free sources only — closes the
  open question the previous session left.

Five items, ordered by dependency then value:

1. **DONE (PR #158)** — Make the inverse funds coherent with real short selling. Since they stay, a
   long position in one is bearish exposure the short-side ceiling cannot
   currently see. That exposure now counts against the same ceiling, and the
   Portfolio Manager knows these are bearish instruments. Correction 2026-08-30:
   a second commit in the same PR fixed a sign error: shorting an inverse ETF is
   bullish (betting the underlying index rises), not bearish, and must not consume
   the bearish budget. The setting was renamed from `risk.max_short_gross_pct` to
   `risk.max_gross_bearish_pct` to reflect the widened scope.
2. **DONE (PR #157)** — Widen the free news sources further. Per-company
   coverage now exists but is scoped to the names the desk holds or is watching
   that day instead of universally, capped so it does not explode to ~100 requests.
   Yahoo per-symbol only; Seeking Alpha was verified working and deliberately not
   enabled due to cost constraints.
3. **DONE (PR #160, merged during audit)** — The unbuilt half of the research-desk work: seats formally arguing out a
   disagreement, and a name more independent seats agree on earning a larger
   share of the risk budget. A target carrying an unadjudicated conflict is now dropped
   before grounding (punishment fits offence — single-target drop, not session-wide).
   Sizing by agreement is now in the deterministic risk code (not a model instruction):
   risk_allocation_pct is ceilinged by how many independent seats are directionally aligned,
   indexed by agreement count. Default schedule is [3.0, 4.0, 5.0, 5.0, 5.0]%, keeping
   1-source trades at 60% of the 5% envelope, 2-source at 80%, 3+ at full 100%.
4. **DONE (PR #159)** — Log every trade's allocated risk against how it actually turned out, so
   conviction can be judged from data. Each trade now carries its allocated risk
   percentage, stated conviction, and deciding model pinned at entry. Exit rows
   label whether they link to an originating decision or have none. The grouping
   of outcome-by-conviction exists but is gated: below 20 per bucket it reaches
   the human operator only and is kept out of every agent prompt.
5. **DONE (PR #156, with one gap since closed)** — The intra-session scan result that never
   reaches the session report. The rehearsal harness can now read the intraday
   scan's nested outcome from its own report instead of only the top-level status.
   At the time, two limitations were recorded and not fixed by design: the path is
   unit-tested but no current production replay actually contains an intraday_scan
   key (still true — none exist in live history), and a crashed scan produced no
   marker — the session read healthy and the operator could not see the crash.
   **Correction 2026-08-30 (PR #163): the crash gap is now closed** — a crashed
   scan attaches its own status and the session reports it as a failure.

Single ordered list of outstanding work. A session resuming cold should start
here. Items are ordered by dependency first, then by value per unit of effort.
Rationale for the trading items is in `docs/QAMC_REMEDIATION_SPEC.md`; evidence
for the analyst items is in `docs/AGENT_ROLE_AUDIT.md` and
`docs/RESEARCH_FINDINGS.md`.

**Landed (2026-08-27)**

- Phase 0 — CI is a real gate. `pytest` required on `main`, strict, and
  `enforce_admins: true`. Proven by a deliberately failing test being refused.
  Root causes were fork-level workflow disablement plus `fastapi` living only in
  the `[api]` extra while CI installed `.[dev]`.
- Governance — owner-ratification rule in `AGENTS.md`; `OUTCOME.md` corrected to
  a profit mandate; `STATE.md` corrected on five false claims.
- Repository hygiene — branches reduced from 110 to 27.
- **Phase 1 + 1b** (PR #102, merged) — structural levels from five years of
  history (`src/data/levels.py`), market context (`src/data/context.py`),
  invented stops and targets deleted. PRs #103, #104 and #105 followed
  (audit + research docs, then the document-authority tiers).
- **Phase 2a** — committed as `c89e957` on branch
  `feat/risk-metrics-and-pm-correlation`, merged and deployed. Folds in the four
  audit findings that were blocking real Phase 2 sizing work: the drawdown-halve
  is now deterministic code (§1.1, `src/risk/rules.py::apply_drawdown_scale` +
  `drawdown_buy_cap`), the Portfolio Manager sees the correlation matrix before
  it decides (§1.2), portfolio heat / budget risk / open risk exist and render
  to PM + RM (§1.3, `src/risk/metrics.py`), and R-multiple reaches the Position
  Reviewer (§1.4). New `risk.max_portfolio_risk_pct` (25%) is **reporting-only**
  — it does not gate anything yet.
- **Phase 3.1 + 3.2** — committed as `aea82ee` on branch
  `feat/exit-rework-pace-and-memory`, merged and deployed. §3.1: the `pace` feedback
  loop is broken — `expected_horizon_sessions` and `setup_type` are pinned on
  the `trades` row at BUY time and never recomputed, the rolling-calibration
  `avg_hold_days` query is deleted from the review path, and `pace_status`
  (`measured` / `too_early` / `n/a_breakout` / `unavailable_no_pinned_horizon`)
  replaces a fabricated figure with a labeled absence. §3.2 (audit §1.5): the
  reviewer now has memory — each review snapshots its per-position metrics
  (`db.save_position_review_metrics`), the next review receives the deltas,
  and new `src/risk/exit_guard.py` vetoes a SELL/REDUCE whose stated reason is
  a deterioration claim when every metric that moved actually improved. Exits
  on new information are never vetoed. 2323 tests pass (2283 on main, +40).
- **Phase 3.3** — committed as `2f177e33` on branch
  `feat/exit-gate-and-risk-routing`, merged and deployed. The hard-trigger phrase
  gate previously applied only to a symbol already trimmed that day, so a
  position's *first* sale executed on soft reasoning unchecked — almost
  every sale. Two of 2026-08-26's evening-graded `premature` exits (EPD,
  MRVL) were first sales that went straight through the gap. Every SELL and
  REDUCE now requires the reason to name a recognized trigger; a
  non-matching reason is dropped and logged as
  `exit_blocked_no_named_trigger`. The trigger vocabulary
  (`_HARD_TRIGGER_KEYWORDS`, `src/pipeline.py`) was widened first — macro
  regime shift, sector shock, adverse/material news, earnings miss, guidance
  cut, all sanctioned by spec §3.8 and previously unrepresented — so gating
  every exit against the old list would have blocked legitimate ones.
  Concentration and drift were deliberately NOT added: their reason shape is
  the verbatim 2026-05-04 AMZN double-trim, and drift trims belong to the
  Portfolio Manager's rule-priority rows 4-5, not this seat.
  `config/prompts/position_reviewer.md` states the new scope and full
  trigger list. 2324 tests pass (2323 before).
  **§3.4–§3.7 landed afterward** (route exits through AI Risk, ATR noise
  band, broker-resident trailing stops — see "Phase 3 — COMPLETE" below);
  §3.5 (upgrade the reviewer's model off `gemini-2.5-flash-lite`) was
  resolved as an owner decision rather than implemented. **Note on §3.5:** its
  "weakest model in
  the stack" premise is contradicted by the committed benchmark data —
  `ops/model_policy/results/merged.json` scores `gemini-2.5-flash-lite` at
  quality 1.0/1.0 on its own `midday_exit` scenario, tied with four other
  candidates including the PM's own `gpt-5.5`. See the correction note under
  `QAMC_REMEDIATION_SPEC.md` §3.5 for the full evidence. This needs an owner
  decision on what §3.5 should actually be scoped to; it is not decided
  here.

- **Phase 3 — COMPLETE (2026-08-27).** §3.1 pace feedback loop cut (horizon
  pinned at entry on the trade row; `avg_hold_days` removed from the review
  path entirely) and §3.2 reviewer memory + the metric-contradiction veto
  (`src/risk/exit_guard.py`) landed in PR #107. §3.3 every exit must name a
  trigger and §3.4 exits route through AI Risk — fail-OPEN, deliberately
  asymmetric with the entry path — landed in PR #108. §3.6 ATR noise band on
  price-derived exits and §3.7 deterministic broker-resident trailing
  (`src/risk/trailing.py`, range vs breakout keyed on the pinned `setup_type`)
  landed in PR #109. §3.5 resolved as an owner decision, see below. §3.8
  unchanged — the reviewer keeps full authority to exit on new information.
- **DEPLOYED to the live paper account 2026-08-27 ~09:20 ET** at `058273f1`;
  rollback SHA `9f77b03e`. Verified on the box: `paper=true`,
  `intraday_scan.enabled: true` (the only tracked config delta) preserved
  through the checkout, `drawdown_buy_cap` armed, the OKLO noise-band case
  blocks, and the `trades` migration (`expected_horizon_sessions`,
  `setup_type`) plus the reviewer-memory reader were pre-run so the 09:30
  session would not hit a cold migration. **Phase 2b was NOT deployed at this
  point — see below; it has since been merged and deployed too (same evening,
  as part of PR #113).**

- **GPT-5.5 Flex for the Portfolio Manager — done.** `16f6535` on branch
  `feat/pm-flex-routing`, merged and deployed as part of PR #113. OpenRouter serves
  the PM's exact model (`openai/gpt-5.5-20260423`) from `openai/flex` at half
  price ($2.50/$15 vs $5/$30 per M tokens) — an endpoint choice, not a model
  choice, so no benchmark was needed. New `llm.<agent>_provider_order`
  (`["openai/flex"]` for the PM), rejected at config load on any non-OpenRouter
  seat. Fallbacks stay enabled rather than pinned `only`: the fallback serves
  the same weights, so the exposure is money, and failing a session closed to
  save $0.11 is a bad trade. Because one model id now has two prices,
  OpenRouter calls request `usage: {include: true}` and the daily cost
  circuit spends against the provider-reported cost, not the pinned-table
  estimate, degrading to the estimate when no figure comes back.
  `EXPECTED_PROVIDER_ORDER` added to `ops/commissioning/verify_commissioning.py`
  so a runtime host silently losing this preference doubles the PM's cost
  while looking normal. Expected effect: PM cost per run roughly halves.
- **Phase 2b — risk-based sizing and correlation budget — done.** `75c0233`,
  same branch. §2.1: `TargetPosition.risk_allocation_pct` (0.5–5.0% of equity)
  replaces `target_weight_pct` as the live sizing field;
  `shares = (equity × risk_pct/100) / |entry − stop|`, so a wider stop yields
  a smaller position rather than a rejected trade. `target_weight_pct` stays
  Optional only so stored decisions still replay. §2.2: new
  `src/risk/budget.py::allocate_risk_budget` enforces the 25% total at-risk
  ceiling and a 40%-of-total per-correlated-cluster cap as a live gate,
  largest-request-first with an alphabetical tie-break, denying a request
  below the 0.5% floor rather than shrinking it. New config:
  `max_position_risk_pct` 5, `min_position_risk_pct` 0.5,
  `max_cluster_risk_share_pct` 40. §2.4 verified already satisfied — no fixed
  position-count target exists anywhere. The gate is enforced only when the
  caller supplies book risk + clusters (`pipeline_stages._book_risk_inputs`);
  otherwise portfolio ceilings are deliberately unenforced and only the 5%
  single-name cap applies. **See the caution above** — a separate,
  uncommitted attempt at this same phase still sits in the `phase2b`
  worktree and needs reconciling.

  **Two follow-on fixes landed on top, same branch, both PR #113, both merged
  and deployed 2026-08-27 evening.** `b712f4c`: `max_position_pct` (20%) is a HARD BLOCK rule in the
  risk engine, not a trim, and nothing connected it to §2.1's sizing — the
  constructor was free to build orders the engine existed to refuse. Measured
  against the book: the 17 most recent BUYs carried stops a median 4.3% below
  entry, which admits at most 0.86% risk under the 20% ceiling, so every
  target from moderate conviction upward was silently hard-blocked (empty
  book, not an error). The constructor now clamps to that same ceiling itself
  and states in the order's reasoning that the position therefore risks LESS
  than the PM allocated. `3dff940` then fixed the reason the stops were that
  tight in the first place: they sat a median 1.7 ATRs from entry — barely
  past one ordinary day's range, the same noise band Phase 3's trailing-stop
  work established at 1.25 ATR. A stop that tight was both firing on ordinary
  volatility (Phase 3's failure mode) and forcing the 20% clamp to bind at
  every conviction level (this failure mode) — one root cause, two symptoms.
  Entry stops placed inside `risk.min_stop_atr_multiple` ATRs (base 3.0,
  scaled 0.85x/1.15x by breakout/range setup and 0.95x/1.10x/1.20x by
  risk-on/transitional/risk-off regime) are now pushed out to that floor;
  structure still places the stop, this only widens one placed inside the
  noise, never tightens a wide one. A widened stop that drops reward:risk
  below `risk.min_reward_risk_after_widening` (1.5) rejects the trade rather
  than taking it at a payoff it never earned. Measured effect: MSFT's stop
  went 2.4% → 7.0%, VLO 4.5% → 9.2%, OKLO 7.7% → 24.7%, and 0.5/1.0/1.5% risk
  now produces 7.1/14.2/20.0% positions instead of clamping all three to 20%
  — conviction changes size again. `config/prompts/portfolio_manager.md`'s
  conviction bands and `tech_analyst.md`'s stop guidance were recalibrated to
  match (2026-08-27 doc-sync pass). 2487 tests pass. **This bullet does not
  track live deploy status** — see "Session start" above for how to check
  what production is actually running.
- **`intra_check` un-blindfolded — done.** `fb88e08`, same branch. It cost
  $0.222/run vs morning's $0.221 while the PM received a technical-only
  evidence registry. The morning's macro/news are now carried forward
  (date-scoped: refuses anything not from today) and labelled
  `carried_from_morning` in `data_status` instead of `not_run_intraday`, so
  the degraded-sources advisory still fires honestly. Nothing is re-fetched.
  `session_type` removed from `build_evidence_registry`/`validate_grounding`
  entirely rather than left inert.
- **Cockpit surfaces `agent_logs.input_message` — done, premise corrected.**
  `6b7af86`, same branch. The backlog previously claimed the field
  "already stores the PM's complete prompt" but "nothing populates or serves
  it" — the first half was right, the second was **false**: all 32 PM rows in
  the live DB carry it (13KB–190KB each), and `/runs/{run_id}` has always
  served it via `SELECT *`. The only real gap was `frontend/src/api/client.ts`'s
  `AgentLogItem` interface omitting the field — a frontend-only fix, now
  rendered by a new `AgentPromptViewer` in the Run Detail modal.
- **Macro sector-stance vocabulary unified, and a live crash fixed.**
  `cdb387b`, same branch. Macro sector stances reached the PM in two
  vocabularies — the live agent's overweight/underweight vs `MacroStore`'s
  persisted bullish/bearish — and both now arrive in normal operation because
  the intraday tick (above) carries stored macro forward.
  `SECTOR_STANCE_TO_DIRECTION`/`SECTOR_DIRECTIONS`/`normalize_sector_stance`
  now live once in `src/models.py`, imported by `macro_store`. Also fixed a
  live crash the carry-forward surfaced: `build_user_message` indexed each
  `sector_guidance` entry as a mapping, so the persisted dict shape raised
  `TypeError: string indices must be integers` and killed every intraday PM
  call carrying macro forward.
- **`ops/` and `scripts/` are now import-guarded — done.** `6f897a1`, same
  branch, new `tests/test_ops_scripts_importable.py`. `pytest` collects
  `tests/` only, so the operational tooling (the model benchmark, the
  commissioning verifier, the pricing check, replay and preview) had zero
  coverage and a `src/models.py` schema change could break a tool while the
  suite stayed green. Three consumers drifted that way on 2026-08-27 alone:
  `ops/model_policy/scenarios.py` stopped importing when `138edd2`'s Phase 1
  tranche made `setup_type` required (fixed same-day in `300ea14` — the
  harness was broken for HOURS, not months; an earlier verbal description of
  it as "rotted months ago" was wrong and has been corrected in
  `tests/test_model_policy_harness_imports.py`, `e19d42b`), and
  `ops/preview/branch_preview.py` plus the Mission Control evidence route both
  kept reading `TargetPosition.target_weight_pct` after Phase 2b made it
  optional (`002095c`). The new guard discovers every module under `ops/` and
  `scripts/` dynamically rather than hardcoding a list, so a new file is
  covered the moment it lands. Import is deliberately shallow — it proves a
  tool still agrees with the schemas it's built on, not that the tool works.
  2470 tests pass.
- **Insider routine/opportunistic filter — PR #133 opened against `main`, not yet
  merged or deployed.** `f3aeba4` + `866e423` (original implementation) plus
  a 2026-08-28 finishing pass on branch `feat/insider-signal-filter`
  (worktree `insider-filter`). `src/data/insider_signal.py::classify_transaction`
  implements the Cohen/Malloy/Pomorski (JF 2012) routine-versus-opportunistic
  test in pure Python, first-match-wins: non-open-market codes, incomplete
  amounts and zero-price rows are handled first (mostly contract guards —
  `SECForm4Provider` already drops everything but non-derivative P/S), then
  the calendar-month test (same insider, same issuer, same calendar month,
  same direction, in each of the 3 preceding years), then a recurring-cadence
  fallback (>=3 prior same-direction trades, mean gap 20-120 days, coefficient
  of variation <=0.25) for when 3 years of history is not yet available, then
  proportional-size rules on sells (routine under 5% of the pre-transaction
  holding) and all buys opportunistic. `SmartMoneyObservation` gains
  `signal_class`/`signal_class_reason`/`signal_class_detail`/`signal_weight`;
  a routine purchase can no longer make a symbol `admission_eligible` (narrows
  the existing gate only — broker/price/liquidity/history/sector gates in
  `pipeline.py` are untouched); `smart_money_analyst.md` and the analyst's
  compact payload now carry the verdict and weight dollar totals by class. New
  `data/smart_money/insider_history.json`, pruned at `insider_history_retention_days`
  (default 5 years), because `observations.json` is pruned to the lookback
  window and the calendar test needs years of retained history.
  **Deliberate departure from the folk version of this filter:** a 10b5-1
  checkbox alone never marks a sale routine — `RESEARCH_FINDINGS.md` §1
  states plainly that planned and discretionary high-value sales show
  similar opportunism, so the flag only ever supports a routine label for a
  sale that is already proportionally small; a large planned sale stays
  `material_stake_sale` and its reason text records that the flag was seen
  and not acted on. Two tests pin this.

  **2026-08-28 finishing pass** (this PR) closed two gaps found on review of
  the original commits:
  1. **Every classification threshold was a module-level constant** —
     `MIN_MATERIAL_SELL_FRACTION`, `CALENDAR_ROUTINE_YEARS`,
     `MIN_CADENCE_TRADES`, the cadence gap/dispersion bounds, and
     `HISTORY_RETENTION_DAYS` — which violates the standing rule that a
     number able to change classification output is an operator setting,
     not a hardcoded one. Moved to seven new fields on `SmartMoneyConfig`
     (`src/config.py`, all prefixed `insider_`, defaults unchanged from the
     old constants so unconfigured behavior is identical), threaded through
     a new `InsiderSignalThresholds` dataclass into `classify_transaction`/
     `classify_observations`, and wired end-to-end through
     `SECForm4Provider.__init__` → `src/pipeline.py`'s construction of it →
     `config/settings.yaml`. A config-load validator rejects a cadence
     window with `min >= max` and a history retention shorter than the
     calendar-routine lookback requires.
  2. **Test gaps required by the finishing task:** every reachable
     transaction-code path pinned with hard literals (P, S, and the `""`
     contract-guard path directly; `SmartMoneyObservation.transaction_code`
     is `Literal["", "P", "S"]`, so A/M/F/G/D/X cannot reach the classifier
     at all — those six are instead pinned at the parser boundary in
     `tests/test_smart_money.py::test_every_non_open_market_code_is_dropped_before_the_classifier`,
     proving they never arrive); an indeterminate (unclassifiable) filing
     confirmed KEPT through the full `fetch()` pipeline, not just at
     `classify_transaction`; three tests proving the thresholds are
     genuinely configurable (same input, different threshold, different
     verdict — something a hardcoded constant could not do); and a revert
     cross-check (`src/` reverted to `origin/main` with these tests left in
     place) that failed 4 tests directly plus a whole-module collection
     error on `tests/test_insider_signal.py` (36 tests that never got to
     run) — i.e. the tests actually depend on this implementation existing.
     `tests/test_smart_money.py` (pre-existing, untouched by the original
     commits) continues to pass unchanged, pinning that non-routine Smart
     Money behavior — admission, materiality, clustering — is unaffected.

  **Measured against the real production cache**
  (`/home/qamc/quant-agent/data/smart_money/observations.json`, read-only,
  2026-08-28): **2,742 stream=insider rows within the 7-day lookback, 1,224
  filings, 2026-08-21 to 2026-08-28 — 57.3% routine (1,572), 42.6%
  opportunistic (1,167), 0.1% indeterminate (3).** This re-confirms the
  original 56.2%-of-2,188 figure (measured a day earlier, 2026-08-21 to
  2026-08-27) rather than replacing it — same order of magnitude, same
  caveat: **zero rows matched the calendar-month or cadence rules** in
  either measurement, because `insider_history.json` still does not exist
  in production (this PR is not deployed). The 57.3% is still driven
  entirely by the proportional-sell rules (`planned_small_disposition` +
  `immaterial_stake_sale` = 1,568 of 1,572 routine rows; the remaining 4 are
  `zero_price_transaction`) plus, this time, one previously-unmeasured
  finding: **only 1 of the 413 buy-side (code `P`) rows was routine at all**
  (a single `$0`-price transaction) — the routine label is concentrated
  almost entirely on the sell side, which the admission gate never reads.

  **Before/after on `SECForm4Provider.fetch()`, same real cache, same
  `config/settings.yaml`, uncapped `max_observations` (measuring the gate,
  not the 40-row display limit), classifier vs. a stub that force-labels
  every row `opportunistic` (byte-for-byte what `fetch()` did before this
  branch — the only change is the added `and verdict.label != "routine"`
  clause):**
  - **Admission-eligible symbols: unchanged, 43 both before and after** —
    the sole buy-side row the classifier demotes (the `$0` transaction
    above) was already excluded by the pre-existing materiality floor
    regardless of its label, so on today's snapshot the narrower admission
    gate has not yet flipped any symbol's eligibility. This will not stay
    true forever — it holds only because no *material* buy in the current
    7-day window happens to be routine yet, and because the calendar test
    has no history to draw on.
  - **Ranking impact is real and large even though admission isn't**: of
    the 1,842 rows that clear `fetch()`'s materiality/cluster gates, 1,113
    (60.4%) are routine and now sort last / contribute `$0` to the
    dollar-weighted ranking sum `smart_money_analyst.py::_symbol_rank`
    uses. **94 symbols in the fetch() output have their entire visible
    dollar volume down-weighted to `$0`** — including one (`IHT`) whose raw
    total was **$2.04 billion**, entirely two routine sales, that would
    previously have dominated the ranked list ahead of every genuine
    opportunistic signal in the universe.

  **Pre-existing gap found, not fixed (out of this task's scope, flagged
  per the stop-on-pre-existing-rot rule):** a row with
  `transaction_value_usd is None` (e.g. `incomplete_amounts`) can never
  survive `fetch()`'s survivors loop at all — both the individual-
  materiality and cluster-window branches require
  `transaction_value_usd is not None` before a row is even added to the
  candidate set, dropping it regardless of `signal_class`. This predates
  the classifier (`b1944cd`, "add SEC smart-money transient admissions")
  and is a materiality-gate limitation, not a routine/opportunistic
  classification defect — the classifier itself still returns
  `indeterminate`, never `routine`, for this case
  (`tests/test_insider_signal.py::test_indeterminate_filing_from_missing_amounts_is_not_downgraded_to_routine`
  pins that). Worth a separate look if unpriced Form 4 rows turn out to be
  common enough to matter.

  Full suite: 2,713 tests pass (2,696 after merging current `main`, +17 net
  new in this finishing pass). Nothing here is deployed; PR only.

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
- **Fractional shares are OUT.** Alpaca supports fractional on simple orders
  but not combined with bracket/OCO. QAMC attaches the protective stop as an
  OTO bracket at entry (`AGENTS.md` invariant 3), so fractional would mean a
  window where a position exists unprotected. Not worth recovering whole-share
  rounding loss (V wanted 6%, got 3.84%).
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

**Next, in order**

1. **Phase 9 — the research desk deliberates.** Every seat may nominate a
   candidate; Technical becomes a responder rather than the gatekeeper on
   candidacy; material disagreements must be adjudicated, not just logged;
   conviction follows multi-source agreement. Full design in
   `docs/QAMC_REMEDIATION_SPEC.md` Phase 9. Depended on Phase 2b — now
   committed (`75c0233`, `feat/pm-flex-routing`) — because "agreement earns
   size" is meaningless until size is expressed as risk.
2. **Execution: bounded re-peg — BUILT, SHIPPED DARK, AND MOSTLY INERT, merged
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
3. **Earnings filing extraction fix — PR #115 (`009ab78`, branch
   `feat/shorts-visible`, misnamed — it carries the earnings fix, not
   shorting), merged into `main`.** Deploy status is not tracked here — check
   `sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1` against
   `git log origin/main` to see whether it has shipped yet. Corrects the diagnosis
   previously recorded here, which was wrong: the class is
   `EarningsDataProvider` (`src/data/earnings.py`), not `EarningsProvider`,
   and it was never doing a naive first-30,000-characters slice — structured
   section extraction and a density-seeking fallback both already existed.
   The real defect: `_extract_key_sections` matches the phrase "financial
   statements", which also appears verbatim inside the auditor's opinion
   letter ("...the related notes (collectively referred to as the financial
   statements)"). The acceptance test measured only LENGTH (≥3,000 chars), so
   that prose comfortably cleared the bar and suppressed the density-seeking
   fallback that would have found the real tables. Measured over the 68
   filings cached on the production box: 17 reached the earnings analyst
   starved (<40 financial figures), 12 of those with ZERO — MSFT, AAPL,
   GOOGL, BAC, CVX, NFLX among them. Fix: require ≥40 financial figures
   (dollar amounts, comma-grouped thousands, parenthesized negatives — the
   same pattern `_find_financial_dense_region` already scored by, now a
   shared module constant) in addition to length; failing the content check
   falls through to the fallback instead of returning. Re-measured across all
   68: 17 improved, 51 unchanged, 0 regressed, 0 starved.
4. **Insider routine/opportunistic filter — PR #133 opened against `main`, not yet
   merged/deployed.** See the "Landed" entry above (`feat/insider-signal-filter`)
   for what it does and the measured routine split with its caveat.
5. **Lazy Prices 10-K year-over-year diff.** Text similarity only, no model. The
   filings are already downloaded and stored.
6. **Phase 4.2 — repair the data feeds.** News-feed half **DONE** (branch
   `fix/news-feeds-and-coverage`, 2026-08-28): Reuters/AP investigated live —
   neither is fixable for free (Reuters retired public RSS in 2020; AP's own
   feed requires a paid OAuth2 API, and the free third-party proxy is
   Cloudflare-walled) — both removed from `RSS_FEEDS`, Yahoo Finance News
   added as a partial free substitute, and `NewsCoverage` (`src/data/news.py`)
   now makes a dead feed impossible to miss: it's in the analyst's own prompt
   and in `data_status["news"]` (`ok`/`partial`/`failed`), which is what
   `trader_feed.py`/`notifier.py` already render as the operator-facing
   `⚠️ Data degraded` banner. **FRED half also DONE (2026-08-30, PR #162)** —
   see the "Landed (2026-08-30, later)" entry above; this line was left
   "still open" for two days after that stopped being true.
   (4.1, un-blindfolding the intraday buy path, is done — `fb88e08`,
   `feat/pm-flex-routing`, see the landed section above.)
7. **Phase 5 — short selling, now a three-stage plan.** The prior estimate
   recorded here — "bounded and additive, roughly a day, NOT a rewrite" — was
   **wrong**. A survey for Stage 1 found roughly 50 long-only assumptions
   across the money path, several failing silently: the constructor would
   re-open a held short every session (a short's weight was absent from
   `_current_weights`, so `.get(sym, 0.0)` read an already-held short as
   unheld); short orders bypass the risk engine entirely via an early
   `return []` on SELL; and shorts counted as zero portfolio risk in
   `portfolio_heat` (`qty <= 0` was excluded). Discovery still ALREADY WORKS —
   `TechAnalysisResult.rating` emits `sell` / `strong_sell` today. **Inverse
   ETFs are explicitly NOT the answer** — the owner rejected that workaround;
   he wants real short selling. Split into:
   1. **Make shorts countable — PR #116 (`feat/shorts-countable`, two commits
      `71325b1` + `a81bfde`), merged into `main`.** Deploy status is not
      tracked here — check `sudo -n -u qamc git -C /home/qamc/quant-agent log
      --oneline -1` against `git log origin/main`. Signed weights
      in `_current_weights`, side-aware `r_multiple`/`position_risk`/
      `portfolio_heat` in `src/risk/metrics.py`, and `qty != 0` (not `qty >
      0`) in every reporting filter (`src/storage/db.py`,
      `src/notifier.py`, `src/trader_feed.py`, `src/pipeline.py`). No order
      path is touched — the constructor emits nothing for a held short
      (`current_pct < 0`) rather than routing a cover or an add-to-short
      through paths that don't yet handle direction. 40 tests added
      (`tests/test_shorts_countable.py`), 21 of which failed pre-merge on
      `main` without this fix; the rest are a no-op wall proving long
      arithmetic is unperturbed.
   2. **Make shorts safe — landed 2026-08-28 (commit `10e0f10`, "Stage 2 of
      short selling: make shorts safe"; `tests/test_shorts_safe.py`, 28
      tests), merged to `main`.** Correction 2026-08-29: this line
      previously read "(not yet started)" — that was wrong as of today's
      check against origin/main; stage 2 is done, stage 3 below is what
      remains. Risk-engine routing so a SELL on an unheld symbol doesn't
      skip the deterministic gate via the early `return []`; stop direction
      (above entry) and trailing direction inverted for shorts; unbounded-loss
      margin accounting.
   3. **Turn it on (not yet started).** Order placement in the broker layer,
      then retire the inverse ETFs (`SH`, `SDS`, `PSQ`, `SQQQ`) as the
      bearish-expression mechanism. **Correction 2026-08-29: the first half
      is done — stage 3 (PR #150) landed and is merged and deployed, so
      shorts can be opened and covered.** The inverse-ETF retirement is
      still outstanding: `SH`/`SDS`/`PSQ`/`SQQQ` remain in the trading
      universe, now redundant rather than necessary.
   Alpaca is ready: `shorting_enabled: true`, `no_shorting: false`,
   `max_margin_multiplier: 4`, assets `shortable` with `borrow_status:
   easy_to_borrow`. The Alpaca paper account was verified on 2026-08-28 as
   already margin-enabled (`shorting_enabled: True`, `multiplier: 4`, equity
   $9,871.87) — no owner action is outstanding. (`docs/QAMC_REMEDIATION_SPEC.md`
   Phase 5 previously recorded an owner action to switch the account to
   margin; that is stale and has been corrected there.)

   **Owner ratified 2026-08-29: ship stages 2 and 3 complete and enabled, not
   behind a flag** — a disabled feature is unvalidated code; see "Owner
   decisions, 2026-08-29" under "Session start" above.

   **Known residual, needs a schema migration.** `pending_protection_restores`
   WAL rows predate shorts and carry no side column, so a row for a short's
   cancelled BUY stops looks byte-identical to one for a long's cancelled
   SELL stops. `_derive_close_side_for_drain` (`src/pipeline.py`) reads the
   broker's live signed position to tell them apart, but when that read
   itself fails, `_drain_pending_protection_restores` deliberately degrades
   to the pre-existing `sell` default rather than stalling the row —
   verified in the code and comments as of 2026-08-29. Unreachable today
   because shorts cannot yet be opened; becomes reachable, and wrong, the
   day they can. Close it as part of stage 3, not after. Current behaviour
   is pinned by `tests/test_shorts_emergency_close.py`'s crash-recovery
   drain-path coverage (added `e9851ea`, flagged by PR #135's own coverage
   audit as the one corner with zero tests) — read it before changing it.
   **Correction 2026-08-29: closed as part of stage 3, as planned.**
   `pending_protection_restores` now persists a `side` column, written at
   row-creation time by whoever is closing the position; the drain path
   prefers that persisted value and only falls back to the live-broker
   derivation (never a blind `sell` default) for a legacy row written before
   the migration. See `tests/test_wal_protection_side.py`.
8. **Phase 6 — cost circuit and transparency.** Dollar-based cap with an
   afternoon reserve; `position_id` linking a buy to the sell that closed it;
   surface the reasoning already stored but never displayed. **Correction
   2026-08-29: done** — see "Landed (2026-08-29)" at the top of this backlog.
9. **Phase 7 — measurement.** Backtester and conviction calibration. Must enforce
   post-training-cutoff evaluation windows for any LLM signal — contamination is
   the dominant failure mode in this literature. **Correction 2026-08-29: the
   backtester landed; conviction calibration (whether the AI's stated
   conviction predicts outcome) did not** — see "Landed (2026-08-29)" above.
10. **Analyst upgrades.** News cascade — **stage 1 (dedup) is DONE**
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

#### THE REHEARSAL HARNESS — built, acceptance test PASSING (corrected 2026-08-29)
Merged to `main` as PR #122 (`feat/session-rehearsal`). Runs a full session offline against a snapshot of production, replaying recorded model responses. Free, deterministic, about 50 seconds. Blocks outbound network at the process level and proves the production database is byte-identical afterwards. Operator alerts are suppressed via `QAMC_REHEARSAL=1`.

**This section previously said the acceptance test did not pass. That was stale by the time it was read on 2026-08-29** — the fix landed the same day it was written, inside the same PR (`ee6f671`, "un-merge chunked agent rows so replay stops running dry"), and nothing after that commit ever came back to correct this text. Verified again on 2026-08-29 by re-running both acceptance tests from a fresh worktree against the live production snapshot: `pytest tests/test_rehearsal_replay.py tests/test_rehearsal_reproduces_cost_ceiling.py` — **11 passed**.

What was wrong and the fix: `tech_analyst.analyze_batch` auto-chunks a large symbol batch into several real provider calls (3 chunks + 1 missing-symbol recovery for the 2026-08-28 incident run, `agent_logs.provider_requests = 4`), then merges them into ONE `agent_logs` row before logging. Replay patches the provider transport, invoked once per real call, so it needed 4 recorded answers for that row and found 1 — the first chunk consumed it, every later chunk raised `MissingRecordedResponse`, and the resulting failure cascade masked the actual incident behind an unrelated `failed_call_unknown_cost` trip on `tech_analyst`. Fix (`ops/rehearsal/replay.py::_unmerge_chunked_call`): `analyze_batch` already joins each real call's text behind `"--- chunk i/N ---"` / `"--- missing-symbol recovery ---"` markers, in call order, in both `input_message` and `full_response` — a complete ordered record of the real calls a merged row represents. Replay now splits one row back into one `RecordedCall` per real call before matching, prorating merged-only token/cost figures by each part's share of the row text (last part takes the remainder, so parts always sum to exactly the recorded total).

With the chunk defect fixed, the harness reproduces the 2026-08-28 incident timeline exactly (`llm_circuit_events` on the live box: 09:32 defect 1 — projected session cost, `portfolio_manager`, session $0.0461 / day $0.0476; 11:15 operator reset; 11:30 defect 4 — paid-session count cap, `tech_analyst`, day $0.1765). `test_the_pre_fix_estimator_still_reproduces_the_2026_08_28_block` forces the old byte-as-a-token estimator back on through config (demanding more measured history than the ledger holds) and confirms the reserved-exposure ceiling still blocks the Portfolio Manager exactly as it did that morning, zero trades proposed or executed. `test_rehearsal_reproduces_2026_08_28_pm_cost_ceiling_failure` runs the same incident under the four cost-circuit fixes (PR #126, merged same day) and confirms the ceiling no longer fires and the Portfolio Manager is reached — the correct post-fix outcome. Both tests require `sudo -n -u qamc` read access to the production database and skip cleanly where that access is unavailable.

**Two more rig-only defects found 2026-08-29 by actually running the harness (not just its acceptance test) across `morning`/`midday`/`close`/`evening`/`intra_check` against the live snapshot, both fixed in the same pass:**

1. **`ResponseLibrary.match()` could crash on an ordinary, unpinned rehearsal.** Reproduced live: a plain `morning` rehearsal (no incident pinning, matching against full history) hit `TypeError: '<' not supported between instances of 'RecordedCall' and 'RecordedCall'` inside `scored.sort(reverse=True)`. Cause: `_unmerge_chunked_call` gives every part of one merged `agent_logs` row the same `row_id`, so two un-merged parts of one chunked row tie exactly on `(score, -row_id)` whenever they also tie on Jaccard score (trivially true when neither shares a word with the live prompt), forcing Python to compare the un-orderable `RecordedCall` objects to break the tie. In production this cascaded: tech_analyst's retry logic caught it as a call failure, exhausted retries, failed over to a second provider, hit the identical crash on the identical tied candidates, burned through the cost circuit's `provider_attempt_limit`, and suspended paid analysis for the rest of the session — a rig-only bug that looked exactly like a production incident. Fixed in `ops/rehearsal/replay.py` by ranking candidates by index instead of by object; `tests/test_rehearsal_replay.py::test_match_does_not_crash_when_two_unmerged_parts_of_one_row_tie` pins it.
2. **The verdict didn't know its own pipeline's status vocabulary.** `midday`, `close` and `intra_check` rehearsals that ran perfectly normally — no crash, no missing recording, no blocked agent — all came back `VERDICT: FAIL`, because `_verdict`'s healthy-status set only recognized `executed`/`no_orders`/`no_trades`/`market_holiday`. `run_position_review` (shared by midday/close) returns `"reviewed"` on a normal completion, `run_intra_check` returns `"ok"` when there is no loss violation (the common case on a 30-minute cadence), and `run_evening` returns `"analyzed"`. Production's own `src/trader_feed.py` and `src/notifier.py` already group these with the statuses the rig did recognize as healthy — the rig disagreeing with production about what counts as "this worked" is exactly the dishonest-output failure mode this harness exists to catch in the trading system, reproduced in the harness itself. Fixed in `ops/rehearsal/report.py`; `tests/test_rehearsal_report_verdict.py` pins it (new file, 6 tests).

**Found and reported, NOT fixed here (out of this task's scope, real defects/gaps for someone to pick up):**

- **`NewsIntelligenceReport.market_sentiment` (`src/models.py`) rejects a value the real news analyst LLM uses regularly, and silently discards the entire news report when it does.** It's `Literal["bullish","bearish","neutral"]` with only case normalization, no vocabulary mapping. Measured against the full retained `agent_logs` history: **9 of 54 real news_analyst responses (16.7%) used a non-enum value** — `mixed` (7x), `mixed-to-bearish` (1x), `risk-off` (1x). Each one makes `NewsAnalystAgent.analyze()`'s `NewsIntelligenceReport(**parsed)` raise, caught generically, discarding macro_narrative, pm_briefing, state_changes and stock_news for that entire session — even though the same method has explicit, deliberate per-entry isolation for `state_changes` and `stock_news` specifically so one bad entry can't take down the whole report (see its own docstring). That protection was never extended to the top-level `market_sentiment`/`confidence` fields. Roughly one in six real news-analyst sessions in the retained history lost its entire news product to this.
- **The rig cannot rehearse `earnings_preprocess` at all.** `ops/rehearsal/runner.py`'s `SESSIONS` mapping and `run.py`'s `--session` choices list only `morning`/`midday`/`close`/`evening`/`intra_check`. `TradingPipeline.run_earnings_preprocess()` is a real, scheduled (08:00 ET), LLM-calling session — the *only* place 10-Q/10-K filings get analyzed — with the same structural shape (RunContext, trading-day gate, cost-session activation, protection-restore drain) as the five modes the rig supports. This is undocumented anywhere in the rig's code, tests or docs; it appears to be an oversight, not a deliberate scope cut. Consequence: a defect specific to earnings preprocessing — like the earnings-extraction bug fixed in PR #115 the same week — would be invisible to this harness.
- **A failed broker read cannot be rehearsed.** `RehearsalTradingClient`/`RehearsalDataClient` never raise on `get_account()`/`get_positions()`; only `get_asset()` and `close_position()` are wired to fail. Production's `except Exception: return {"status": "broker_error", ...}` path in `run_morning`/`run_intra_check` is therefore completely untested by this harness. Consistent with the module's own documented scope, but worth naming since "a broker read that fails" is exactly the kind of resilience case this rig should be able to exercise.

**Confirmed working as designed, not defects:** running with no `--source-data` at all correctly fails closed at the pricing gate ("the cost circuit cannot confirm current rates offline and will suspend paid analysis") before any model call, rather than proceeding with an unbounded cost; and deleting every row from a sandbox copy's `positions` table (simulating a flattened book) correctly surfaced the trade-ledger-vs-broker stop-out reconciler declining to guess ("recording nothing rather than guessing") for all six affected symbols, with the session still completing end-to-end rather than crashing on the inconsistency.

Full suite: 2892 tests passed before this pass; +7 net new (1 in `test_rehearsal_replay.py`, 6 in new `tests/test_rehearsal_report_verdict.py`). Now lives at `ops/rehearsal/` on `origin/main`, not on a standalone branch/worktree; see "Session start" above for the owner's 2026-08-29 instruction to run it routinely.

**Hardening pass 2026-08-29 (second): verified the five just-added healthy statuses against `src/pipeline.py` directly rather than trusting the comments above, and audited every `run_*` session function for other gaps.** Found and fixed three more:

1. Three genuine *failure* statuses — `position_review_parse_error` (`run_position_review`/midday+close), `evening_analysis_error` and `evening_parse_error` (`run_evening`) — were already asserted as FAIL by this pass's own tests but had no `STATUS_PLAIN` entry at all, so each would have printed the generic "ended with status 'X'" fallback instead of a real explanation. Added.
2. `early_close` (`run_position_review`/midday+close, `src/pipeline.py:7806`) — a deliberate skip on half-day-holiday sessions, the same shape as `market_holiday` — was missing from both `STATUS_PLAIN` and the healthy set. Added to both.
3. `run_morning`'s PM-failure family — `pm_parse_error`, `pm_schema_error`, `pm_grounding_error`, `pm_repair_changed_decision` (`src/agents/portfolio_manager.py`, surfaced via `ctx.analysis_failure_status`) — were real, reachable statuses with no `STATUS_PLAIN` entry. Production's own `src/notifier.py`/`src/trader_feed.py` already match on `status.startswith("pm_")` as a PM-decision failure; the rig's vocabulary had not caught up. Added as failures (not healthy).

Also added `run_earnings_preprocess`'s statuses (`fetch_error`, `nothing_new`, `analysis_error`, `preprocessed`) pre-emptively — that session is real and scheduled but the rig still cannot invoke it (unchanged, separate gap, see below) — so the vocabulary is already correct on the day that gap closes.

One nuance worth recording: `intraday_no_trades`/`intraday_executed` (from the first hardening pass above) are correct in meaning but were found to be currently **unreachable** as `report.status` — they only ever appear nested at `result["intraday_scan"]["status"]` (`src/pipeline.py:8497-8498`), which `ops/rehearsal/report.py`'s `collect()` never reads; production's own `src/trader_feed.py` reads that nesting explicitly (`nested = result.get("intraday_scan")`, line 54) rather than trusting `result["status"]` for intra_check. Left in `STATUS_PLAIN`/the healthy set (harmless, correct-if-ever-reached) but the rig having no visibility into the intraday scan's own outcome is a real, separate gap — reported, not fixed here.

New guard test `tests/test_rehearsal_report_verdict.py::test_every_known_pipeline_terminal_status_is_classified` pins the full status vocabulary against a hardcoded, file:line-cited list (dynamic AST discovery was tried and rejected — the PM-failure family lives on `AgentResult.semantic_status`, set in a different file, not a string literal at the `"status"` key's return site, so a literal-string walk would silently miss exactly the drift this test exists to catch) — a future undocumented pipeline status now fails CI instead of printing raw.

Full suite: 2900 passed (2899 after the first hardening pass + this test).

#### SMALLER, RECORDED
- `db_reads.get_recent_agent_logs` uses `SELECT *` and `GET /agents/{agent_name}` returns 20 rows; PM prompts run 13KB-190KB, so that response could reach several MB. Harmless today because nothing in `frontend/src/` calls it.
- After the constructor rejects a BUY for reward:risk, it logs a second confusing line — "no valid stop below entry (stop=None)" — because the None propagates. Cosmetic.
- OneCLI: OpenRouter spend from a live rehearsal would be real money on the same account, but the rehearsal runs its own cost-circuit database, so production would under-count the true daily bill.
- OneCLI: production's Alpaca secret matches `*.alpaca.markets`, which also covers the paper host, so both credential sets match the same address. The gateway fails closed on the ambiguity. Narrowing the production pattern risks breaking live credential resolution and was deliberately left for the owner.

#### BRANCHES READY, NO PR YET

- `feat/insider-signal-filter` — merged as PR #133, no longer pending
- `fix/news-feeds-and-coverage` — merged as PR #132
- `fix/dollar-based-session-cap` — its first commit, `766a35d`, added
  `afternoon_reserve_pct` (40) and `afternoon_reserve_release_et_hour` (12)
  plus a `_morning_spend_ceiling()` helper that was defined and never
  called. **Correction, 2026-08-29: superseded, not still open.**
  `_morning_spend_ceiling()` is called from `begin_call` in current `main`
  (`src/cost_circuit.py`), with dedicated passing tests
  (`tests/test_cost_circuit.py::test_morning_spend_ceiling_pure_computation`,
  `test_afternoon_reserve_blocks_morning_spend_above_the_ceiling`,
  `test_afternoon_reserve_recovers_the_same_day_without_a_rollover`) —
  landed via PR #126 (`fix/cost-circuit-four`) and PR #131
  (`fix/pricing-staleness`), both already merged. See "STILL OPEN —
  2026-08-29" item 5 below.
- `feat/news-dedup` — still unmerged; disposition being decided separately.
- `feat/bounded-repeg` — PR #144 opened 2026-08-29. Agent decision: merge it
  rather than leave it to rot, shipping the re-peg disabled by default. Check
  `gh pr list` for current status before treating this as landed.

#### THE NEW STOP RULE REJECTED FOUR BUYS ON ITS FIRST DAY — measure before changing
On 2026-08-28, the reward:risk floor added the previous day rejected four candidates outright. Recorded reward:risk after the stop was widened past the noise band: CRM 0.39, ONDS 0.78, MP 0.80, NVDA 1.30, against a 1.50 minimum.

Three of the four offered **less reward than risk** — those are correctly refused. NVDA at 1.30 is the borderline case.

This is the rule working as designed, but it is also a signal worth measuring rather than reacting to: with honest stop distances, the technical analyst's targets are frequently too close to clear a 1.5 payoff. Either the targets are too conservative or the widened stops are too wide. Do not adjust the 1.50 floor on impression — gather a week of these rejections first, then decide which of the two numbers is wrong.

Note this means 2026-08-28's zero trades had **two independent causes**, not one: the cost circuit blocked the morning before the Portfolio Manager ran, and separately these four were refused on payoff.

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
- `MarketDataProvider.get_next_earnings_date()` is implemented but **unwired**;
  the Tech Analyst accepts a `days_to_earnings` kwarg that nothing supplies.
- Nothing tells the Portfolio Manager that `SH`, `SDS`, `PSQ` and `SQQQ` are
  bearish instruments, so even the sanctioned bearish expression is unwired.
- 26 unmerged branches await triage, including two abandoned VPS security
  branches (`claude/vps-security-hardening-t8m3qz`,
  `claude/vps-deployment-hardening-q3f7k2`) worth rescuing before deletion.
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

## Evidence-only follow-ups

- news-narrative factual drift;
- `actual_provider` attribution oddity.

Do not interrupt natural validation for these unless current evidence shows they materially distort decision quality, truthfulness, or operator understanding.

`get_latest_price` is **not** on this list solely because its request omits `feed`; that concern has been reconciled. Reopen only on concrete production evidence.

