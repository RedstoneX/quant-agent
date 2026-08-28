# QAMC Current Work

Status: **STALE — this file does not track a live production pointer. Check
reality: `sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1`.**

## Current integration truth — historical (2026-08-26, PR #93), superseded

The block below was accurate for the SHA it names but that SHA is no longer
production. Production has since moved through PR #109/#110 (Phase 3,
`058273f1`), PR #111/#112 (execution fix, `e6ada88`), PR #114 (deploy-drift
alarm, `32c174b`), and PR #113 (Phase 2b sizing + stop-width fix, `46b2029`).
That chain is itself now historical, not current — this section has recorded
five different "current" production SHAs in two days, which was the actual
bug. For what production has and what is still pending, read
"Session start" under "Active finish line" below, and use the command above
rather than trusting any SHA written in this file; this section is kept only
because the PR #92/#93 forensic narrative isn't duplicated elsewhere.

- Production was deployed and verified at
  `a25a723f70a4e0f1548b3389c93c96d9b5ced6d7` (2026-08-26); recorded rollback SHA
  was `7fe6e4babbf3cf0209d8f93536f8150de70fea37`. Both are stale — do not use
  either against current production without re-verifying the gap.
- Production remained Alpaca Paper. Mission Control was private/read-only, all
  seven existing timers were intact, and the only tracked production delta was
  `config/settings.yaml: intraday_scan.enabled: true`.
- The Aug 25 ET-day quota hold rearmed automatically on Aug 26 with exact
  accounting. No manual reset or spend deletion occurred.
- The first Aug 26 morning run admitted RSG, AMR and PAM, then safely stopped on
  a Tech recovery/session-limit mismatch before PM, Risk or broker submission.
- PR #92 fixed that contract with one bounded consolidated recovery, retained
  primary results, a narrower prefilter and compact Smart Money input. The
  controlled rerun completed all 54 selected Technical analyses and produced
  seven directional PM targets.
- That rerun exposed two independent data-shaping defects before Risk: valid
  fenced Smart Money JSON lost its wrapper, and missing queued earnings became
  a false `none` stance. PR #93 fixed both at their source without weakening PM
  grounding. The exact saved response replays with all eight findings.
- Run-scoped SEC Form 4 admission remains active for qualifying symbols outside
  the configured 101-stock universe. The lane is capped at three names and
  remains behind broker eligibility, price, history, liquidity, sector,
  Technical, PM, AI Risk and deterministic gates.
- The audited operator-rerun switch can bypass only a same-day morning marker.
  It requires a reason and still enforces the ET window, weekday, session lock,
  Paper mode, paid-session/cost circuit and the full decision/safety chain.
- The complete hermetic suite passed **2,181 tests** at that point (see
  "Session start" below for the current count).
- Alpaca held EPD 12 and SGOV 89 as of 2026-08-26. EPD was protected for all 12
  shares by the broker stop-limit at stop $38.00 / limit $36.86. No trade was
  forced.

## Stabilization account model — HARD RULE

Use two active accounts until QAMC is stable:

### `ubuntu` — engineering/operator

Use `ubuntu` for Codex sessions, engineering checkout/worktrees outside `/home/qamc`, Git/GitHub, development tooling, tests/builds, private Tailscale Vite/browser work, Docker/sudo engineering tasks, and deployment orchestration.

### `qamc` — runtime only

`qamc` owns `/home/qamc/quant-agent`, runtime `.env`/OneCLI wiring, user services/timers, and QAMC Paper execution. Do not run Codex as `qamc` or turn it into a general engineering account.

### `dev` — parked

Do not use `dev` in the normal workflow or expand its permissions during stabilization.

## Standing Paper-beta delivery workflow — HARD RULE

While QAMC remains Alpaca Paper, engineering inside already-authorized work is autonomous under `ubuntu` through the full lifecycle:

**diagnose → implement → test → preview/inspect → PR → merge → deploy → production verify → rollback if needed.**

There is **no mandatory external code-review, merge or deployment gate during Paper beta**. Independent review may be used when useful, but it is evidence, not permission. Keep a dedicated PR for substantive work so the result remains reviewable and reversible in Git.

Live-capital activation, paid dependencies, secrets/credential redesign, destructive infrastructure replacement and material architecture outside current authority still require explicit operator authorization.

## Friction-reduction rules

1. No normal use of `dev` and no manual account ping-pong.
2. Private DEV preview/browser verification is standing-authorized for relevant engineering work.
3. For bounded fixes, read only current authority plus relevant code and run the shortest decisive verification.
4. Use parallelism/subagents when independent work can safely run together and doing so reduces wall-clock time. Do not fan out duplicate work merely to use more agents.
5. Targeted tests first. Broaden only when failures, risk or acceptance evidence justify it.
6. Stop when the requested result is proven; repeated re-validation without new evidence is not diligence.
7. Keep handoffs concise: changed / verified / preview if relevant / unresolved blocker / production state.
8. Preserve the `ubuntu` engineering vs `qamc` runtime boundary; do not add new lockdown/security infrastructure during stabilization without a real need.
9. Do not infer current defects from historical notes. Reopen a resolved area only from current operator or production evidence.
10. Bundle production preflight/deploy/restart/acceptance into the shortest safe intervention.

## Parallelism and engineering-agent policy

Parallel work is encouraged when tasks are genuinely independent. The lead agent owns integration.

- Good parallel targets: independent code surfaces, investigation questions, targeted tests, log/evidence collection, browser/visual verification and documentation checks.
- Bad parallelism: multiple workers rediscovering the same facts, editing the same files without coordination, or duplicating validation already proven.
- Multiple worktrees are allowed when they materially simplify independent work.

Match intelligence to the task:

- **Strongest available reasoning model:** architecture, trading logic, safety-sensitive changes, complex debugging, hard code review, ambiguous UX/product judgment and cross-system integration.
- **Cheaper/faster workers:** bounded tests, searches, inventory, log parsing, evidence collection and other mechanical work.
- Escalate a cheap worker when the task becomes reasoning-heavy instead of burning turns.

Parallelism is an efficiency tool, not an agent-count target.

## Active finish line

### Session start — read this first

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

### Ordered backlog — RESUME POINT

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
- **Insider routine/opportunistic filter — PR opened against `main`, not yet
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
2. **Execution: bounded re-peg.** PR #111 fixed the limit-as-ceiling bug and
   the unfillable-order submission. Still open: replace a working order toward
   the moving NBBO up to the slippage ceiling. Note the footgun — an Alpaca
   replacement mints a NEW order id, so the state machine must track it and
   handle partial fills rather than blind-looping PATCHes.
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
4. **Insider routine/opportunistic filter — PR opened against `main`, not yet
   merged/deployed.** See the "Landed" entry above (`feat/insider-signal-filter`)
   for what it does and the measured routine split with its caveat.
5. **Lazy Prices 10-K year-over-year diff.** Text similarity only, no model. The
   filings are already downloaded and stored.
6. **Phase 4.2 — repair the data feeds.** Fix Reuters/AP/FRED; surface degraded
   coverage to the operator. (4.1, un-blindfolding the intraday buy path, is
   done — `fb88e08`, `feat/pm-flex-routing`, see the landed section above.)
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
   2. **Make shorts safe (not yet started).** Risk-engine routing so a SELL
      on an unheld symbol doesn't skip the deterministic gate via the early
      `return []`; stop direction (above entry) and trailing direction
      inverted for shorts; unbounded-loss margin accounting.
   3. **Turn it on (not yet started).** Order placement in the broker layer,
      then retire the inverse ETFs (`SH`, `SDS`, `PSQ`, `SQQQ`) as the
      bearish-expression mechanism.
   Alpaca is ready: `shorting_enabled: true`, `no_shorting: false`,
   `max_margin_multiplier: 4`, assets `shortable` with `borrow_status:
   easy_to_borrow`. The Alpaca paper account was verified on 2026-08-28 as
   already margin-enabled (`shorting_enabled: True`, `multiplier: 4`, equity
   $9,871.87) — no owner action is outstanding. (`docs/QAMC_REMEDIATION_SPEC.md`
   Phase 5 previously recorded an owner action to switch the account to
   margin; that is stale and has been corrected there.)
8. **Phase 6 — cost circuit and transparency.** Dollar-based cap with an
   afternoon reserve; `position_id` linking a buy to the sell that closed it;
   surface the reasoning already stored but never displayed.
9. **Phase 7 — measurement.** Backtester and conviction calibration. Must enforce
   post-training-cutoff evaluation windows for any LLM signal — contamination is
   the dominant failure mode in this literature.
10. **Analyst upgrades.** News cascade (dedup, then novelty scoring, then a model
   on the residual); deterministic macro regime with the model confined to FOMC
   text; earnings multi-quarter trends. Several need new data sources and an
   owner decision first.

**Identified 2026-08-28, not yet fixed**

#### THE FOUR COST-CIRCUIT DEFECTS
A full trading day (2026-08-28) was lost to these. Fix them together and prove them with the rehearsal harness before deploying.

**Corrected 2026-08-28 against `llm_circuit_events` on the live box.** The
earlier version of this section blamed defect 2 for the outage. The ledger
says defect 1 did, an hour and a half earlier. What actually happened:

| ET time | what tripped | agent | spend at that moment |
|---|---|---|---|
| 09:32 | defect 1 — projected session cost | portfolio_manager | session $0.0461 / day $0.0476 |
| 11:15 | operator reset by hand | — | — |
| 11:30 | defect 4 — paid-session count cap | tech_analyst | day $0.1765 |

1. **The estimator predicts nothing — and it is what stopped the desk.** It
   bounds a prompt by treating every UTF-8 byte as a token and reserves the
   full `max_output_tokens` at list price. At 09:32 ET it reserved **$1.8657**
   for one Portfolio Manager call and refused to proceed, on a day that had
   spent 4.6 cents. Decomposed exactly: **$0.504 of that is the output half**
   (`1.05 x 16,000 tokens x $30/1M`, the same regardless of prompt) and
   **$1.3617 is the input half**. Measured over 37 recorded PM calls: average
   actual cost **$0.1718**, worst ever **$0.5783**, output never above 11,034
   of the 16,000 tokens reserved, and roughly **3.6 UTF-8 bytes per input
   token** on the large prompts. So the reservation runs ~3.2x the worst call
   ever recorded and ~11x the average.
   **The fix previously proposed here targeted only the output half — 27% of
   the error.** Input is the other 73% and must be fixed too. Reserve from
   measured per-(agent, model) history in `agent_logs`: divide prompt bytes by
   a measured bytes-per-token ratio at a conservative low percentile, and
   reserve observed maximum output times a safety margin, capped at
   `max_output_tokens`. Fall back to today's worst-case bound whenever history
   is thin. Percentile, margin and minimum sample count are settings — no
   hardcoded numbers.
2. **A failed request is charged as if it ran.** A provider 429 returns
   nothing and bills nothing, but is recorded as unknown cost, which makes the
   day unreconcilable and hard-stops the desk. Real, and it contributed to the
   11:15 ET manual reset — but it is the second cause, not the first. Fix:
   classify the failure. Known-zero-cost rejections (429, 400, 401, 403, 404,
   pre-send transport failures) release the reservation and charge nothing;
   genuinely ambiguous failures (timeout after send, 5xx, truncated stream)
   keep today's conservative behaviour. Fail closed — anything unclassified is
   ambiguous.
3. **The operator reset tool is blocked by the emergency it exists to reset.**
   `scripts/cost_circuit.py` calls `activate_session()` unconditionally before
   dispatching the command; that runs `_seed_today` ->
   `_validate_accounting_invariants`, which raises on exactly the fault being
   cleared, so `reset` is never reached. (The earlier note said `status()` was
   the blocker — same effect, wrong function.) Had to be worked around by hand.
4. **A separate cap of 2 paid sessions per mode per day** — unrelated to the
   estimator. It stopped the desk again at **11:30 ET** on 17 cents of actual
   spend, under trigger `session_retry_limit`. Raised to 8 on the live box as a
   stopgap and now committed. Proper fix is dollar-based with an afternoon
   reserve so a runaway morning cannot consume the day, keeping a deliberately
   high count only as an infinite-loop backstop. Partial prior work on branch
   `fix/dollar-based-session-cap`.

#### LIVE-BOX CONFIG DRIFT — RECONCILED (PR #119, live `224722e`)
Closed 2026-08-28. It was **five** settings, not three, and two of the recorded
baselines were wrong. `config/settings.yaml` in git is now byte-identical to the
production box and the box's working tree is clean.

| setting | was in git | now (and live) |
| --- | --- | --- |
| `intraday_scan.enabled` | `false` | `true` |
| `llm_cost_circuit.daily_cost_limit_usd` | `1.50` | `2.75` |
| `llm_cost_circuit.session_reserved_exposure_limit_usd` | `1.80` | `2.60` |
| `llm_cost_circuit.daily_reserved_exposure_limit_usd` | `1.90` | `5.50` |
| `llm_cost_circuit.max_paid_sessions_per_mode_per_day` | `2` | `8` |

Corrections to the original note: the git baseline for
`daily_reserved_exposure_limit_usd` was `1.90`, not `3.20` (`3.20` was itself an
earlier uncommitted box value), and `daily_cost_limit_usd` was also a git delta
— the box had been running `2.75` against a committed `1.50` — and was not on
the list at all. Hard spend caps unchanged at $0.90/session and $2.75/day.

The 8-session cap and the 5.50 ceiling are STOPGAPS the four fixes above should
supersede. They were committed anyway so git describes the running system.

Also on 2026-08-28: the circuit was reset, two quota holds released, and the
day's `costs_exact` flag settled without refunding any charge. DB backed up
first.

#### THE REHEARSAL HARNESS — built, acceptance test not passing
Branch `feat/session-rehearsal`, worktree `/home/ubuntu/projects/quant-agent-worktrees/rehearsal`. Runs a full session offline against a snapshot of production, replaying recorded model responses. Free, deterministic, about 50 seconds. Blocks outbound network at the process level and proves the production database is byte-identical afterwards. Operator alerts are suppressed via `QAMC_REHEARSAL=1`.
**Outstanding:** the replay runs out of recorded responses on the Technical Analyst's chunked calls, so it cannot yet reproduce the 2026-08-28 Portfolio Manager ceiling failure on demand. That is its acceptance test and it does not pass yet.

#### COCKPIT — DELIVERED (PR #120)
All five owner requests are implemented, built and tested on branch
`feat/cockpit-trader-view`: chart vertical space, a holdings + P&L strip visible
on arrival, the average-entry line drawn on the chart with live P&L, PREV CLOSE
dropped on the 1D view only, and Positions/Liquidity split into two dockable
panels. The Dockview layout key is bumped to `qamc.dockview.cockpit.v2` so a
stale saved layout cannot break on load. 84 frontend tests pass, up from 71.

**Discovered while shipping it:** this repo commits its built frontend bundle to
`src/api/static_cockpit/` and the API serves that directory from disk —
production never runs `npm run build`. A frontend source change therefore does
not reach the screen until the rebuilt bundle is committed. The refreshed bundle
is included in PR #120. Anyone changing frontend source must do the same.

Mission Control is read-only, so this ships without affecting trading.

#### NEWS FEEDS
Reuters returns 404 and AP returns 403, confirmed live on 2026-08-28. Untested hypothesis: a 403 is usually a blocked User-Agent rather than a dead feed. No paid dependency without the owner's approval. The part that must land regardless: a dead feed currently logs a warning and vanishes, so the system reports complete news coverage while missing two wire services.

#### SMALLER, RECORDED
- `db_reads.get_recent_agent_logs` uses `SELECT *` and `GET /agents/{agent_name}` returns 20 rows; PM prompts run 13KB-190KB, so that response could reach several MB. Harmless today because nothing in `frontend/src/` calls it.
- After the constructor rejects a BUY for reward:risk, it logs a second confusing line — "no valid stop below entry (stop=None)" — because the None propagates. Cosmetic.
- OneCLI: OpenRouter spend from a live rehearsal would be real money on the same account, but the rehearsal runs its own cost-circuit database, so production would under-count the true daily bill.
- OneCLI: production's Alpaca secret matches `*.alpaca.markets`, which also covers the paper host, so both credential sets match the same address. The gateway fails closed on the ambiguity. Narrowing the production pattern risks breaking live credential resolution and was deliberately left for the owner.

#### OPEN PRs, none deployed
- #115 earnings extraction — the analyst was reading the auditor's letter, not the numbers. 17 of 68 cached filings starved, 12 with zero figures.
- #116 shorts countable — proven a no-op on a long-only book.
- #117 doc sync.
- Insider routine/opportunistic filter (`feat/insider-signal-filter`) — see "Landed" above; PR number to be added once opened.

#### BRANCHES READY, NO PR YET
`feat/news-dedup` (real duplication only about 5%), `feat/bounded-repeg` (inert by design, ships off), `fix/dollar-based-session-cap` (unfinished), `feat/session-rehearsal`.

`feat/insider-signal-filter` moved to "OPEN PRs" above (2026-08-28): finished
(thresholds moved to config, per-code and fail-closed tests added) and PR
opened against `main` — 57.3% of 2,742 real Form 4 rows measured routine,
consistent with the earlier 56.2%-of-2,188 figure.

#### DECISIONS RATIFIED 2026-08-28
- Stops were too tight and that was the root cause of two separate failures. The ATR multiple must scale by setup type and macro regime — never a hardcoded constant.
- Real short selling, not inverse ETFs. Three stages: countable, safe, live.
- No dev/prod mirror. Production is paper and resets, so the case for enterprise staging collapses. Build the rehearsal harness instead.
- The system already sends marketable limit orders, which is a market order with a bounded worst case. No change needed.
- Documentation is the source of truth. Wrong documentation is corrected on sight without asking.
- Rehearsal alerts are suppressed rather than routed to a second Telegram bot.

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


### QAMC Remediation Spec — what Phase 1 delivered

Kept as a record of the Phase 1 tranche's contents. **Status lives in the
ordered backlog above, not here** — this section had drifted into a second,
contradictory account of the same work (it still described Phase 1 as sitting
on an unmerged branch after it had merged) and is now scoped to substance only.

- `TechAnalysisResult` (`src/models.py`) requires `support_levels`,
  `resistance_levels`, `setup_type` (`"range"` / `"breakout"`),
  `expected_horizon_sessions` and `reference_target` for every actionable rating.
- `src/data/levels.py` deterministically finds support/resistance from the full
  OHLCV history; `src/agents/tech_analyst.py` feeds a formatted levels block into
  the prompt, computed over `trading.lookback_days: 1800` (~5 years).
- `src/portfolio_constructor.py`'s synthesized stop (`entry − 2×ATR` / `entry ×
  0.95`) and target (`entry × (1 + 2×stop_gap_pct)`) are deleted, along with their
  config fields. A candidate with no structural stop or target is now rejected
  rather than traded.
- `src/data/context.py` adds deterministic market context per symbol — relative
  strength vs a same-batch benchmark (SPY/QQQ/IWM), 1w/1m/3m/6m/12m returns,
  52-week range position, ATR percentage-of-price with a 1y percentile and
  volatility state, MA slopes, consolidation detection, dollar volume, up/down
  volume ratio, unfilled gaps — rendered via `format_context_block()`.
- `src/data/market.py` adds `MarketDataProvider.get_next_earnings_date()` and the
  Tech Analyst accepts a `days_to_earnings` kwarg, but **nothing in the pipeline
  wires them together** — tested, unused end to end. Still true; still listed in
  the backlog's "set aside" items.

### Mission Control / existing cockpit utility

Complete. PRs #76 and #77 are merged and deployed with the backend recovery from PRs #74 and #75. Preserve the accepted live cockpit unless current evidence or the research-intelligence outcome below requires a coherent extension.

### Research Intelligence Desk + Smart Money Analyst — COMPLETE / DEPLOYED / SEC SOURCE COMMISSIONED

The accepted Research Intelligence Desk tranche is complete in production at
PR #87. The acceptance criteria below remain the product contract; they are no
longer an unfinished implementation queue. Natural market validation continues
separately and does not reopen the completed read-side tranche without new
production evidence.

Build one coherent intelligence experience, not a sequence of small dashboard tweaks.

The operator should be able to open Mission Control and quickly understand what every research agent learned, where agents agree/disagree, what the Portfolio Manager decided, what AI Risk changed, what deterministic Python allowed/blocked, what actually happened, and what the system learned afterward — **without reading logs or raw JSON**.

The experience is for one private operator, not customers. It should feel like a sharp internal trading desk: useful, candid, visually interesting, occasionally dry or irreverent, and never corporate. **Say everything useful. Nothing merely decorative.**

Cover the existing research/review seats where their output is relevant — Technical, News, Macro, Earnings, Portfolio Manager, AI Risk Manager, Position Reviewer, Evening Review and Meta-Reflection — and add one new **Smart Money Analyst** specialist seat.

#### Smart Money Analyst outcome

Use first-party, credentialless SEC data for v1. Phase A is broad Form 4
discovery of exact non-derivative open-market purchase/sale codes `P` and `S`,
with accession-level provenance, transaction time, SEC acceptance time, lag,
owner identity/role, amendment and 10b5-1 context where present. Python owns
parsing, direction, recency, materiality, independent-owner clustering,
deduplication and admission eligibility. Quiet or unchanged evidence must use
zero model tokens; the LLM sees only compact surviving evidence and may
synthesize meaning but cannot author source facts or admission.

The permanent configured universe remains unchanged. A fresh external `P`
purchase that clears the higher external materiality threshold may be admitted
for the current run only after deterministic Alpaca common-US-equity/tradable
eligibility, supported-exchange, minimum-price, minimum-history, minimum
20-session dollar-liquidity and known-sector checks. At most three external
symbols may be admitted per run. Admission only adds the symbol to that run's
research/PM allowlist; it must still receive current Technical analysis and
pass Portfolio Manager grounding, AI Risk, every deterministic risk/funding
rule and broker protection. It is never written into the permanent universe.

Schedule 13D/13G and curated-manager 13F deltas remain possible later phases,
not v1 admission inputs. Alpha Vantage may be considered only as an optional
cross-check/fallback; Bargo may be reconsidered if access arrives. Neither is
a current dependency. Paid alternative-data dependencies remain unauthorized.

The Smart Money Analyst should identify **viable present-tense trading evidence**, not merely summarize disclosure feeds. It must distinguish evidence by freshness and economic meaning. Congressional trades can be disclosed up to roughly 45 days after the transaction and 13F holdings can be filed up to 45 days after quarter-end, so those streams are primarily thematic/confirmatory context. SEC Form 4 insider transactions are generally filed within two business days and are materially more timely. Any genuinely real-time/near-real-time stream made available under the accepted free source may be treated according to its actual timestamp and provenance.

The seat should intelligently suppress noise and surface only material patterns: clustered or repeated activity; unusual size/direction relative to the available disclosure; multiple independent smart-money streams aligning; activity that confirms or contradicts current News/Macro/Earnings/Technical evidence; and fresh evidence that changes the current thesis. A lone stale politician transaction is not a trade signal. Every surfaced finding must state what happened, when it happened, when it became knowable, why it matters now, and whether it is actionable, confirmatory, contradictory or merely historical.

If other genuinely free, reliable, source-backed smart-money streams are available under acceptable terms, Codex may incorporate them into this same seat rather than proliferating agents. Provider/API details should remain replaceable rather than becoming trading architecture.

The new seat may inform the Portfolio Manager through the existing specialist-evidence path. It must not bypass PM, AI Risk, deterministic Python or broker protections and must not create a new execution path.

#### Research/reading experience outcome

Desktop should have a strong designed default composition, then let the operator rearrange it. Reuse Dockview so panels can move, resize, tab, maximize and persist their layout. iPad should be composed for reading, not squeezed from desktop.

The writing should be **compact but substantive**. Short sentences. Strong editing. No filler, repeated conclusions, forced jokes, fake quotes or generic AI throat-clearing. Wit should come from judgment, not punchlines. Quiet days should stay quiet.

Use visual structure where it genuinely helps:
- **signal stack** — quick agreement/conflict across relevant agents;
- **what changed** — the new information since the prior useful read;
- **tension** — the most important disagreement or contradiction;
- **why now** — why the item deserves attention today;
- **evidence strip** — compact factual chips instead of prose where possible;
- **mini chart/sparkline** — only when it adds immediate market context;
- **Read / PM / Risk** — clearly separated judgment, portfolio implication and risk consequence;
- **dry annotation** — occasional, restrained, evidence-based commentary when the situation earns it.

Do not force every device onto every card. The point is rhythm and hierarchy, not decoration. One important story may be visually dominant while supporting research is smaller. Balance matters more than symmetry.

Favor useful editorial synthesis such as daily market thesis, agent findings, disagreement, Smart Money evidence, PM ruling, Risk response, proposed-versus-executed delta, position review, after-the-bell lessons and tomorrow watch. Raw structured evidence remains secondary drill-down.

No fabricated confidence, quotes, history or facts. Sparse, stale, partial, no-news, no-trade and provider-error states must look intentional and remain truthful.

#### Acceptance

This tranche is complete when real stored QAMC data demonstrates that:

1. An operator can read a coherent daily story without opening logs or JSON.
2. Every relevant agent has a useful, visually balanced representation of its findings, strongest evidence, meaningful changes and disagreement where supported.
3. The writing is substantive without being verbose, visually scannable, and has a restrained private-desk personality rather than corporate/LLM prose.
4. Signal stacks, change markers, tension, why-now context, evidence strips, mini-chart context, Read/PM/Risk separation and occasional dry annotations are used where they improve comprehension rather than mechanically everywhere.
5. PM/Risk/execution are understandable as deltas: what PM wanted, what Risk changed, what deterministic code allowed/blocked, and what actually executed.
6. Desktop research panels are genuinely movable/resizable/tabbable/maximizable with persisted layout and a sensible default workspace.
7. iPad has a deliberately designed reading/navigation experience with no horizontal overflow or micro-text.
8. Smart Money Analyst is SEC-source-backed, accession/timestamp/lag-aware,
   attributable, direction-validated, noise-suppressing, and reaches PM only
   through the accepted specialist path. Any external symbol is run-scoped,
   visibly admitted by deterministic evidence, and still traverses the full
   Technical → PM → AI Risk → deterministic gate → broker chain.
9. Empty, stale, partial and provider-error states are truthful and visually composed.
10. Targeted tests/build pass and rendered desktop+iPad visual acceptance passes with zero console/page errors and no horizontal overflow.

#### Engineering posture

This is outcome-driven work. Codex has autonomy to inspect the current repository, choose the simplest implementation consistent with accepted architecture, make routine engineering/design decisions, implement, test, visually inspect, commit, push, merge, deploy and verify production under the standing Paper-beta workflow.

Use parallel subagents where they genuinely accelerate independent work. Assign strong reasoning models to difficult architecture/trading/product problems and cheaper workers to bounded mechanical work. Do not split the product tranche into micro-PRs, repeatedly ask the operator routine questions, or over-specify implementation from this handoff.

Preserve the `ubuntu` engineering/operator versus `qamc` runtime-only boundary. Do not alter secrets architecture, add unrelated infrastructure, force trades, weaken safety or broaden broker authorization. If deployment verification fails, stop further mutation and preserve/restore the last known-good production state.

Stop for the operator only on a genuine unresolved product/safety/architecture conflict, a paid dependency, a live-capital boundary, or an external credential/authorization requirement that cannot be satisfied from existing project resources.

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

## Hard boundaries

- **Current execution authorization is Alpaca Paper only.**
- Options/theta strategies remain outside accepted architecture. Direct stock
  shorting and margin were authorized 2026-08-27 (`docs/STATE.md`) and are
  pending implementation, not yet in production — see the Phase 5 backlog
  entry above for status.
- Deterministic Python/broker protections remain final safety authority.
- Do not force/manufacture trades or weaken safeguards to increase activity.
- Do not create paper-only trading semantics.
- No new daemon/service/database/proxy/security/credential/orchestration architecture without separate explicit approval.
- No paid alternative-data dependency without separate explicit approval.
- Keep `qamc` runtime-only and preserve OneCLI secret handling.
- Do not expand `dev` privileges during stabilization.
- Mission Control remains private/read-only; Telegram remains output-only.
- No public exposure of QAMC or OneCLI.

**Active work:** natural Alpaca Paper observation continues. *(The rest of this
paragraph is 2026-08-26 history — PR #93 and "today"'s session limit are
stale; see "Session start" above for how to check current production
state.)* The
remaining acceptance item is a future eligible session traversing PM, AI Risk,
deterministic gate and broker execution when warranted, followed by
management/exit and measured outcome. No trade may be forced or manufactured;
repeated no-trade results must have a specific decision or risk reason, not a
mechanical pipeline failure.
