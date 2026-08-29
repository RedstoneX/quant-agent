# QAMC — Autonomous Overnight Session Brief

Written 2026-08-29 by the outgoing session, at the owner's request, for a
fresh session starting with empty context. The owner is asleep. **You cannot
ask him anything.** Proceed to completion on your own judgement and report at
the end.

---

## 1. What QAMC is for

**It exists to make money.** Not to be a research toy, not to demonstrate an
agent architecture. This is already recorded correctly — `docs/OUTCOME.md`
states it plainly, and `AGENTS.md` records why it once did not: coding agents
wrote their own scope decisions into the governance documents, later agents
read them as owner instructions, and the system was built long-only against a
mandate that never excluded shorting. That was corrected on 2026-08-27 and the
ratification rule now exists to stop it recurring.

You have standing authority to correct wrong documentation on sight, but
**verify before you "correct"** — the mandate documents are already right, and
an earlier session's note claiming otherwise was itself stale.

**It must be fully autonomous.** The owner has health conditions that at times
affect his capacity. He cannot watch markets and must never be a required
step in a control loop. Every guardrail you build must be mechanical and
fail-closed, never "the operator will notice". This is the reason autonomy is
non-negotiable, not a preference.

**Current reality:** Alpaca **paper** account, ~101 symbols, six scheduled
sessions a day. Recorded P&L is negative (−$80.74 after four hidden stop-outs
were backfilled). The system trades, but it cannot yet tell you whether it
trades *well* — that is Phase 7, and it is the gap that matters most.

---

## 2. Your operating model tonight

**You are an orchestrator. You do not implement.**

- Dispatch subagents for everything: investigation, implementation, tests,
  documentation, verification sweeps.
- **Match model to complexity.** Haiku: documentation, inventory, mechanical
  edits from a supplied spec, log parsing. Sonnet: bounded implementation with
  real judgement, targeted tests, non-safety-critical review. Reserve your own
  reasoning for architecture, trading and risk logic, and reviewing anything
  that can lose money.
- **Protect your own context.** Do not read large source files yourself. Read
  diffs, summaries and targeted command output. The more you delegate, the
  longer you last — and you must last all night.
- **Never run two writing agents in the same worktree.** One writer per
  worktree; read-only reporters may overlap freely.

**But do not trust subagents.** Tonight's predecessor session had an agent
confidently report a root cause that was wrong — the documented diagnosis
blamed a data table when the real cause was a header element. It was caught
only because the orchestrator reproduced the claim itself against live data.

**So: verify every consequential claim, cheaply and adversarially.** Not by
re-reading everything — by picking the single load-bearing assertion and
testing it directly. If an agent says "tests pass", check the count. If it
says "the fix works", reproduce the fix's effect. If it says "X is the cause",
check that X actually produces the symptom.

---

## 3. Autonomy and boundaries

Work the plan below to completion. Do not stop for approval. Do not ask
questions — there is no one to answer.

**Hard boundaries. If you reach one, do NOT act on it. Record it as a decision
the owner must make, skip that item, and continue with everything else. Never
let a boundary stall the whole night:**

- Activating **live capital**. `paper: true` must remain true. Never change it.
- Taking on a **paid dependency** (data feeds, wire services, APIs).
- Redesigning **secrets or credentials**.
- **Destructive infrastructure** changes.
- Weakening a risk control, or loosening a threshold, to make something pass.

**Everything else is yours to decide**, including implementation approach,
ordering within a phase, test strategy, which already-benchmarked model a seat
uses, and deploying to the paper account.

**Tonight is the safest possible window for risky trading-code changes:**
it is a weekend, markets are closed, and the scheduled sessions self-gate to
ET market windows, so nothing will trade while you work. Use it.

---

## 4. The work, in the order to do it

The desk board (`docs/phases.yaml`, rendered at `/board`) is the authority on
what remains. Phases 4, 5, 6 and 8 are PARTIAL; 7 and 9 are NOT STARTED.
Read the board's own `plain_summary` for each before starting it.

Land each item as **its own PR**, green in CI, then deploy and verify before
starting the next. Do not batch unrelated work into one PR.

### A. Quick defects first (cheap, user-visible, low risk)

1. **Missed Opportunities panel opens a modal unpredictably.** Clicking a
   symbol there opens a Candidate Detail modal only when that symbol happens
   to be in whichever session is selected in the Sessions strip — same click,
   different outcome, nothing on screen explaining it. It is wired to
   `inspectSymbol` in `App.tsx` (conditionally opens a modal) instead of the
   modal-free `chartPositionSymbol` that Positions uses. **The owner has hit
   this and reported it. Fix it first.**
2. **Diagnostics → Search opens a Run Detail modal on any row click.** Needs a
   design decision on what "the symbol" means for the agent-call-hit table,
   which has no symbol column. Decide it yourself and implement.
3. **`docs/phases.yaml`'s `daily_cost_limit_usd: 2.75` rule** pins a value
   expected to be re-tuned — the same flaw that made the Phase 6 session-cap
   rule false-alarm. Convert it to `setting_present`, which already exists.

### B. Phase 6 — spending controls (finish it)

The cost circuit still benches the desk on session *counts* rather than
dollars, and the afternoon reserve is half-built.

- `fix/dollar-based-session-cap` (commit `766a35d`) adds
  `afternoon_reserve_pct` and `afternoon_reserve_release_et_hour` plus a
  `_morning_spend_ceiling()` helper **that is defined and never called.** The
  reserve is inert, untested, and must not ship as-is. Finish it: wire it into
  `begin_call`, test it, and reconcile the `max_paid_sessions_per_mode_per_day`
  default (that branch says 8; shipped settings say 40).
- The count cap should become a genuine runaway backstop, with dollars as the
  real limit. The rationale is in that commit message — read it.

**Why this is second:** while the desk benches itself, everything downstream is
measured on a crippled sample.

### C. Phase 7 — measurement (not started, and the most valuable)

The owner named this explicitly: *"test strategy changes before risking
money."* Three parts, per the board:

1. A way to test a strategy change against history before risking paper money.
   `src/replay.py` exists but is decision replay for debugging, **not** a
   backtester — the spec says so itself. Do not pass one off as the other.
2. Measurement of whether stated conviction actually predicts outcomes.
3. A dashboard of the core edge numbers: win rate, average win vs average
   loss, expectancy.

**Prerequisite, do it inside this phase:** `compute_trade_calibration` in
`src/storage/db.py` is structurally long-only. It FIFO-matches BUY lots to
sell-family exits, so a covered short creates no lot and closes nothing and is
invisible to win rate, average return and hold days — numbers that reach the
Portfolio Manager as facts. Add a separate short-lot FIFO queue with the
return sign inverted. Precedent recorded in that function's own comments:
omitting filled TRAIL_STOP exits once moved win_rate 22.2% → 30.0% and
avg_return −2.79% → −2.18% on the real ledger.

### D. Phase 5 — short selling, stages 2 and 3

Around 50 places in the code assume every position is a long. Stage 1
(counting a short correctly in the risk math) is done. The emergency
force-close path is done and merged. Stages 2 and 3 — safely managing and
actually opening shorts — are not built.

**Ship it ENABLED. Finish the feature.** This is a paper account that resets,
markets are closed until Monday 09:30 ET so nothing trades while you work, and
the owner has all of Sunday to review before a single order could be placed.
There are no users, no customers and no real money.

An earlier draft of this brief said to ship it behind an off switch. The owner
rejected that, correctly: **a disabled feature is unvalidated code**, and the
entire point of reaching the finish line is to surface the next layer of bugs.
You cannot find bugs in something that never runs.

The gate is therefore **completeness, not a flag**. Do not ship a half-built
stage 3 with a setting as a fig leaf. It is done when the code actually opens
and manages a short correctly, the tests cover it, and you have verified the
behaviour against the real broker where the paper account allows.

**Known residual, needs a schema migration:** crash-recovery WAL rows carry no
side column, so an orphaned short with an unreadable broker falls back to
guessing `sell`. Unreachable today; becomes reachable the moment shorts can be
opened. **Close it as part of stage 3, not after.** Its current behaviour is
pinned by tests — read them before changing it.

### E. Phase 9 — the research desk actually deliberates

The owner's actual ask: every research seat (news, earnings, macro, insider
signals) gets to propose trade ideas and argue about them, instead of the
technical-chart analyst being the only seat allowed to originate a candidate.
Fully designed on paper, no code. The design is in
`docs/QAMC_REMEDIATION_SPEC.md` and `docs/AGENT_ROLE_AUDIT.md` — follow it
rather than inventing a new one.

This is the largest architectural item. If the night runs short, land a
coherent first slice with the seats able to originate, rather than a
half-wired everything.

### F. Phase 4 remainder, and the rest

- **News sources audit** the owner asked for: review what the news seat reads,
  widen it — **free sources only**. Reuters and AP are dead for good (Reuters
  killed public RSS in 2020; AP needs a paid key). Both already removed. A
  paid wire is an owner decision, so do not take one.
- **`feat/company-profiles`** — 221 lines, written, never wired in. The owner
  asked for it. Wire it or record why not.
- **Phase 8** — documentation correction, ongoing.

---

## 5. Operational facts that will cost you hours if you miss them

- **A `git checkout` on the box is not a deploy.** The API holds the cockpit
  bundle. Restart `quant-agent-api.service` and then confirm the hashed bundle
  filename the server returns matches the one on disk under
  `src/api/static_cockpit/assets/`.
- **Production serves a committed frontend bundle and never builds.** A
  frontend source change does not reach the screen until you run the build and
  commit its output.
- **The real Mission Control is `https://ovh-vps.wallaby-bowfin.ts.net/cockpit/`**
  — no port number. Anything on port 8810 is an ephemeral branch preview. One
  was left running for seven days and showed the owner a week-old dashboard;
  he reasonably concluded deploys were not working. If you start a preview,
  kill it when you are done.
- **Never `git add -A` or `git commit -a`.** Other sessions edit sibling
  worktrees; a blanket stage commits their work under your message.
- **Never bare `git stash`.** The stash is a repo-global ref shared across
  every worktree. Two agents collided on it tonight. Use
  `git stash push -m "<name>"` and pop by explicit index, or better, measure
  baselines in a throwaway worktree.
- **Branch protection requires branches to be up to date**, so a merge queue
  must be serialised: merge main in, wait for CI, merge, repeat. **Never use
  `--admin`.**
- **`gh pr edit` fails on this repo** (a deprecated Projects-classic GraphQL
  field). Use `gh api repos/RedstoneX/quant-agent/pulls/N -X PATCH`.
- **Subagents stall on polling loops** and will burn six figures of tokens
  waiting on CI. Give every agent an explicit polling budget, or poll yourself.
- **Engineering runs as `ubuntu`; the runtime is `qamc`.** No venv in the
  engineering checkouts — use `/home/ubuntu/projects/quant-agent/.venv/bin/python`
  with `PYTHONPATH` set to the checkout root and the CI dummy API-key env vars.
  Read the live box with `sudo -n -u qamc`.
- **Log timestamps are UTC; the owner is ET.** Convert before quoting times.
- **Never state a date or duration from impression** — get it from `git log`.
  Everything before 2026-08-09 belongs to the upstream author, not to QAMC.

Starting state: production deployed at `dcbb4d6`, zero open PRs at the time of
writing, status board green (13 phases, 0 contradicted).

---

## 6. What to leave him in the morning

A **concise** rundown. He is not a programmer and has asked repeatedly for
plain language — no file paths, function names, SHAs or test names in the
summary.

For each thing you did:
- one line on what changed,
- one short line on **why you decided it that way**.

Then, separately and briefly:
- what you deliberately did NOT do, and why,
- anything that hit a hard boundary and needs his decision,
- what will actually change in the desk's behaviour when markets open Monday,
  so he can review it on Sunday rather than discover it in a fill.

He wants to be able to say "no, change that" about any decision — so make the
decisions legible, not buried. Lead with the conclusion. Keep it scannable.
Do not pad it with everything that went right.
