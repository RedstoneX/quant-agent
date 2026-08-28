# QAMC Current Work

Status: **STALE — see "Session start" below for current production state.**

## Current integration truth — historical (2026-08-26, PR #93), superseded

The block below was accurate for the SHA it names but that SHA is no longer
production. Production has since moved through PR #109/#110 (Phase 3,
`058273f1`), PR #111/#112 (execution fix, `e6ada88`), and PR #114 (deploy-drift
alarm, `32c174b` — current). For what is actually deployed now, read
"Session start" under "Active finish line" below; this section is kept only
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


**Live production state (2026-08-27 evening ET, deployed this session):**
deployed at `32c174b` on the paper account — PR #114, the deploy-drift alarm,
merged on top of `e6ada88`. (Earlier same-day notes claimed `18dd4bc`, then
`e6ada88`; `18dd4bc` was never actually on the box, and `e6ada88` was
superseded by this deploy within the session — corrected here.) Phase 3 (exit
rework), the execution limit fix, and the deploy-drift alarm are LIVE. Seven
positions open, all with broker-resident stops. `paper: true`. Daily LLM
budget raised to $2.75.

**New this deploy — deploy-drift alarm (PR #114, `9eef617` + `38a985c`):**
`scripts/check_deploy_drift.py` plus `quant-agent-drift-check.timer`
(Mon-Fri 08:45 ET) alerts over Telegram when the box's deployed HEAD falls
behind `origin/main`. Built because PR #111 sat merged-but-undeployed for
eight hours with nothing catching it (see the correction above). Verified
firing.

**Not deployed:** everything merged after `32c174b`, plus the whole of
**PR #113** (`feat/pm-flex-routing`), which is open, CI-green and unmerged.
None of it is on production. It carries, in review order:

- `75c0233` Phase 2b risk-based sizing + the correlation-aware risk budget
  gate — **the highest-consequence change here; review first.** It decides how
  much money each trade may lose. `b712f4c` and `3dff940` land on top of it,
  same branch: the constructor now clamps to the risk engine's 20%
  single-name ceiling instead of proposing orders it hard-blocks, and entry
  stops sitting inside ordinary volatility get pushed out to a
  regime-and-setup-scaled ATR floor (`risk.min_stop_atr_multiple`) — a
  widened stop that drops reward:risk below 1.5 rejects the trade outright.
  Measured against the real book: MSFT's stop went 2.4% → 7.0%, VLO 4.5% →
  9.2%, OKLO 7.7% → 24.7%, and 0.5/1.0/1.5% conviction now produces
  7.1/14.2/20.0% positions instead of clamping all three to 20%. None of
  this is deployed — PR #113 is still open.
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
  `feat/risk-metrics-and-pm-correlation` (not yet merged). Folds in the four
  audit findings that were blocking real Phase 2 sizing work: the drawdown-halve
  is now deterministic code (§1.1, `src/risk/rules.py::apply_drawdown_scale` +
  `drawdown_buy_cap`), the Portfolio Manager sees the correlation matrix before
  it decides (§1.2), portfolio heat / budget risk / open risk exist and render
  to PM + RM (§1.3, `src/risk/metrics.py`), and R-multiple reaches the Position
  Reviewer (§1.4). New `risk.max_portfolio_risk_pct` (25%) is **reporting-only**
  — it does not gate anything yet.
- **Phase 3.1 + 3.2** — committed as `aea82ee` on branch
  `feat/exit-rework-pace-and-memory` (not yet merged). §3.1: the `pace` feedback
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
  `feat/exit-gate-and-risk-routing` (not yet merged). The hard-trigger phrase
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
  point — see below, it has since been committed.**

- **GPT-5.5 Flex for the Portfolio Manager — done.** `16f6535` on branch
  `feat/pm-flex-routing` (committed, not merged/deployed). OpenRouter serves
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

  **Two follow-on fixes landed on top, same branch, both PR #113, neither
  deployed.** `b712f4c`: `max_position_pct` (20%) is a HARD BLOCK rule in the
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
  match (2026-08-27 doc-sync pass). 2487 tests pass. **None of this is
  deployed** — PR #113 is open, CI-green, unmerged.
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
  2b (`75c0233`, `feat/pm-flex-routing`, not yet merged/deployed) — see the
  landed section above.

**Measured LLM spend (10 days to 2026-08-27) — read before proposing any budget change**

**These figures predate the flex-routing and intra_check fixes below** (both
committed on `feat/pm-flex-routing`, not yet merged/deployed) — they are the
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
3. **Earnings filing extraction is broken.** `EarningsProvider._extract_text`
   (`src/data/earnings.py`) takes the first 30,000 characters of a filing. For
   a 10-K that is the cover page, auditor's report and table of contents — the
   financial statements are hundreds of pages further in. MSFT's own analysis
   says so: *"Filing text is heavily truncated, consisting mainly of auditor's
   report and table of contents."* The earnings seat has never seen MSFT's
   numbers. Cheap fix (locate MD&A / financial statements rather than slicing
   from the top) and it restores an entire evidence source.
4. **Insider routine/opportunistic filter.** Cheap Python, best evidence-to-effort
   ratio in the system — over half of Form 4 trades carry zero predictive power.
5. **Lazy Prices 10-K year-over-year diff.** Text similarity only, no model. The
   filings are already downloaded and stored.
6. **Phase 4.2 — repair the data feeds.** Fix Reuters/AP/FRED; surface degraded
   coverage to the operator. (4.1, un-blindfolding the intraday buy path, is
   done — `fb88e08`, `feat/pm-flex-routing`, see the landed section above.)
7. **Phase 5 — short selling.** Discovery ALREADY WORKS — `TechAnalysisResult.rating`
   emits `sell` / `strong_sell`, so bearish candidates are identified today.
   What is missing is everything downstream: `PortfolioConstructor._build_sell`
   returns `None` when the symbol is not already held, so a bearish view on a
   name you do not own dies silently in one line; `TradeDecision.action` has no
   open-short value; and sizing, stops (inverted — above entry) and margin
   accounting all assume long. Bounded and additive, roughly a day, NOT a
   rewrite. **Inverse ETFs are explicitly NOT the answer** — the owner has
   rejected that workaround; he wants real short selling. Alpaca is ready:
   `shorting_enabled:
   true`, `no_shorting: false`, `max_margin_multiplier: 4`, equity above the
   $2,000 floor, assets `shortable` with `borrow_status: easy_to_borrow`. This is
   entirely a code change; no account work is outstanding.
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

**Set aside — small, easily forgotten**

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
- No margin, options or direct stock shorting; bearish expression remains through approved inverse ETFs.
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
paragraph is 2026-08-26 history — PR #93 and "today"'s session limit are stale;
current production state is `32c174b`, see "Session start" above.)* The
remaining acceptance item is a future eligible session traversing PM, AI Risk,
deterministic gate and broker execution when warranted, followed by
management/exit and measured outcome. No trade may be forced or manufactured;
repeated no-trade results must have a specific decision or risk reason, not a
mechanical pipeline failure.
