# QAMC Current Work

Status: **AUG 26 ROOT FIXES DEPLOYED | PAPER SAFE | NATURAL FULL-CHAIN ACCEPTANCE OPEN**

## Current integration truth

- Production is deployed and verified at
  `a25a723f70a4e0f1548b3389c93c96d9b5ced6d7`; rollback SHA is
  `7fe6e4babbf3cf0209d8f93536f8150de70fea37`.
- Production remains Alpaca Paper. Mission Control is private/read-only, all
  seven existing timers remain intact, and the only tracked production delta is
  `config/settings.yaml: intraday_scan.enabled: true`.
- The Aug 25 ET-day quota hold rearmed automatically on Aug 26 with exact
  accounting. No manual reset or spend deletion occurred.
- The first Aug 26 morning run admitted RSG, AMR and PAM, then safely stopped on
  a Tech recovery/session-limit mismatch before PM, Risk or broker submission.
- PR #92 fixes that contract with one bounded consolidated recovery, retained
  primary results, a narrower prefilter and compact Smart Money input. The
  controlled rerun completed all 54 selected Technical analyses and produced
  seven directional PM targets.
- That rerun exposed two independent data-shaping defects before Risk: valid
  fenced Smart Money JSON lost its wrapper, and missing queued earnings became
  a false `none` stance. PR #93 fixes both at their source without weakening PM
  grounding. The exact saved response replays with all eight findings.
- Run-scoped SEC Form 4 admission remains active for qualifying symbols outside
  the configured 101-stock universe. The lane is capped at three names and
  remains behind broker eligibility, price, history, liquidity, sector,
  Technical, PM, AI Risk and deterministic gates.
- The audited operator-rerun switch can bypass only a same-day morning marker.
  It requires a reason and still enforces the ET window, weekday, session lock,
  Paper mode, paid-session/cost circuit and the full decision/safety chain.
- The complete hermetic suite passes **2,181 tests**. The exact saved Smart
  Money output and production-shaped missing-earnings record have dedicated
  regressions.
- Current `/health` has DB/broker reachable, `paper=true`, no global suspension
  and no active session lock. Its degraded label is caused only by the earlier
  run's historical session-scoped retry hold. Exact Aug 26 spend is
  **$0.5279159 / $1.50**; both permitted paid morning sessions are consumed.
- Alpaca still holds EPD 12 and SGOV 89. EPD remains protected for all 12 shares
  by the broker stop-limit at stop $38.00 / limit $36.86. No trade was forced.

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

**Live production state (2026-08-27, 11:00 ET):** deployed at `18dd4bc` on the
paper account, rollback `9f77b03e`. Phase 3 (exit rework) and the execution
limit fix are LIVE. Seven positions open, all with broker-resident stops.
`paper: true`. Daily LLM budget raised to $2.75.

**Not deployed:** everything merged after `18dd4bc`, and Phase 2b, which is
committed-but-unfinished on branch `feat/risk-based-sizing` (worktree
`/home/ubuntu/projects/quant-agent-worktrees/phase2b`). That branch already
carries `TargetPosition.risk_allocation_pct` and a new `src/data/company.py`
(company profile blurbs — the owner asked for these; wire them into the PM
prompt and the Telegram trade alerts).

**Engineering setup:** work as `ubuntu`, never as `qamc`. There is no venv in
the engineering checkouts — use `/home/ubuntu/projects/quant-agent/.venv/bin/python`
with `PYTHONPATH` set to the checkout root and the five dummy API-key env vars
CI uses. Read the live box with `sudo -n -u qamc`. Log timestamps are **UTC**;
the owner is **ET** — convert before quoting times to him.

**Working agreement:** the owner has given a standing autonomy grant — do not
stop at phase gates for approval. Interrupt only for live-capital activation,
new paid dependencies, secrets redesign, destructive infrastructure, or
evidence that a ratified decision was wrong. See `Owner decisions` below.

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
  **Still open:** §3.4 (route exits through AI Risk), §3.5 (upgrade the
  reviewer's model off `gemini-2.5-flash-lite`), §3.6 (ATR noise band), §3.7
  (broker-resident trailing stops). **Note on §3.5:** its "weakest model in
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
  session would not hit a cold migration. **Phase 2b is NOT deployed.**

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
  correlation-adjusted.

**Measured LLM spend (10 days to 2026-08-27) — read before proposing any budget change**

$6.73 total across 48 sessions. **$5.84 of it is the Portfolio Manager: 87%.**

| Mode | Runs | Avg/run | Dominated by |
|---|---:|---:|---|
| `morning` | 20 | $0.221 | PM $3.65 (83% of the mode) |
| `intra_check` | 10 | **$0.222** | **PM $2.19 (99% of the mode)** |
| `evening` | 7 | $0.004 | evening + news |
| `close` | 7 | $0.003 | news + position_reviewer |
| `earnings_preprocess` | 4 | $0.004 | earnings |
| `midday` | 7 | $0.003 | news + position_reviewer |

Two facts worth acting on:

1. **`intra_check` costs the same as a full morning run** ($0.222 vs $0.221)
   while doing almost none of the work — $0.003/run on research, $0.219 on the
   PM call. It is also the session the PM is *deliberately blindfolded* in
   (`portfolio_manager.py` returns a technical-only evidence registry when
   `session_type == "intra_check"`, though macro and news are already in
   memory). 33% of all spend, on blindfolded scanning.
2. **The whole research desk costs 5.5%.** Technical — the *only* source of
   trade discovery today — is 4.6% of spend. The system pays 87% to arbitrate
   a shortlist produced by its cheapest component. Fix the allocation before
   raising the ceiling.

**Next, in order**

1. **Phase 2b — sizing and risk (remaining).** Conviction expressed as risk
   allocation rather than percent-of-portfolio notional (§2.1,
   `shares = (equity × risk_pct) ÷ |entry − stop|`, envelope 5% ceiling / 0.5%
   floor), correlation-aware cluster budgeting so correlated names consume one
   bet's risk rather than several (§2.2), and retiring the fixed position-count
   concept (§2.4). Phase 2a landed the measurement; this turns
   `max_portfolio_risk_pct` from a reported figure into a live gate. Note: a
   grep of `config/prompts/` and `src/` found **no fixed position-count target**
   anywhere — §2.4 may already be satisfied; verify before building to it.
2. **GPT-5.5 Flex for the Portfolio Manager — do this first, it is free.**
   OpenRouter lists OpenAI Flex as a GPT-5.5 provider at exactly half price
   ($2.50/$15 vs $5/$30). **Same model**, so there is no quality question to
   answer and no benchmark to run — production PM cost goes ~$0.22 -> ~$0.11
   per run, against a seat that is 87% of all spend. The only risk is added
   latency; the session wrapper already has a 1200s kill. Verify the routing
   policy test still passes (the committed benchmark is for the model, not the
   provider tier).
3. **Un-blindfold `intra_check` (audit §6 / spec Phase 4).** The PM is handed a
   technical-only evidence registry in this mode while macro and news sit
   loaded in memory. Nearly free to fix and it converts the system's
   worst-value session — 33% of spend — into something that earns it. Frees
   ~$0.44/day to fund the two items above and below.
4. **Surface what the PM actually read.** `agent_logs.input_message` already
   stores the PM's complete prompt — all seven memory layers, verbatim — and
   `AgentLogItem` in `src/api/schemas.py` already declares the field.
   **Nothing populates or serves it.** Wiring that one field through gives the
   operator a "what the PM actually read" view. Today the Journal panel shows
   the evening reflection's `lessons` and `suggested_actions` (that part
   works), but not the assembled briefing: the 7-evening narrative, 14-day
   recurring missed themes, repeat loss patterns, last 5 RM verdicts, the PM's
   own last 3 decisions, or realized-win-rate calibration. Source material is
   visible; the briefing is not.
5. **Phase 9 — the research desk deliberates.** Every seat may nominate a
   candidate; Technical becomes a responder rather than the gatekeeper on
   candidacy; material disagreements must be adjudicated, not just logged;
   conviction follows multi-source agreement. Full design in
   `docs/QAMC_REMEDIATION_SPEC.md` Phase 9. Depends on 2b — "agreement earns
   size" is meaningless until size is expressed as risk.
6. **Execution: bounded re-peg.** PR #111 fixed the limit-as-ceiling bug and
   the unfillable-order submission. Still open: replace a working order toward
   the moving NBBO up to the slippage ceiling. Note the footgun — an Alpaca
   replacement mints a NEW order id, so the state machine must track it and
   handle partial fills rather than blind-looping PATCHes.
7. **Earnings filing extraction is broken.** `EarningsProvider._extract_text`
   (`src/data/earnings.py`) takes the first 30,000 characters of a filing. For
   a 10-K that is the cover page, auditor's report and table of contents — the
   financial statements are hundreds of pages further in. MSFT's own analysis
   says so: *"Filing text is heavily truncated, consisting mainly of auditor's
   report and table of contents."* The earnings seat has never seen MSFT's
   numbers. Cheap fix (locate MD&A / financial statements rather than slicing
   from the top) and it restores an entire evidence source.
8. **Insider routine/opportunistic filter.** Cheap Python, best evidence-to-effort
   ratio in the system — over half of Form 4 trades carry zero predictive power.
9. **Lazy Prices 10-K year-over-year diff.** Text similarity only, no model. The
   filings are already downloaded and stored.
10. **Phase 4 — evidence symmetry and feed repair.** Unblindfold the intraday buy
   path; fix Reuters/AP/FRED; surface degraded coverage to the operator.
11. **Phase 5 — short selling.** Discovery ALREADY WORKS — `TechAnalysisResult.rating`
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
12. **Phase 6 — cost circuit and transparency.** Dollar-based cap with an
   afternoon reserve; `position_id` linking a buy to the sell that closed it;
   surface the reasoning already stored but never displayed.
13. **Phase 7 — measurement.** Backtester and conviction calibration. Must enforce
   post-training-cutoff evaluation windows for any LLM signal — contamination is
   the dominant failure mode in this literature.
14. **Analyst upgrades.** News cascade (dedup, then novelty scoring, then a model
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

**Active work:** natural Alpaca Paper observation continues. The Aug 26 runs
proved live SEC admission, compact Smart Money input, complete bounded Technical
coverage and real directional PM candidate generation. PR #93's corrected PM
evidence handoff is deployed but cannot consume a third paid morning session
today. The remaining acceptance item is a future eligible session traversing PM,
AI Risk, deterministic gate and broker execution when warranted, followed by
management/exit and measured outcome. No trade may be forced or manufactured;
repeated no-trade results must have a specific decision or risk reason, not a
mechanical pipeline failure.
