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

**In flight**

- **Phase 1 + 1b** (PR #102, CI green, awaiting merge) — structural levels from
  five years of history (`src/data/levels.py`), market context
  (`src/data/context.py`), invented stops and targets deleted.

**Next, in order**

1. **Phase 2 — sizing and risk.** Conviction expressed as risk allocation rather
   than percent-of-portfolio notional. Owner-ratified envelope: 5% ceiling per
   trade, 0.5% floor, 25% total, correlation-adjusted. Folds in four audit
   findings: make the drawdown rule real code (§1.1), pass the already-computed
   correlation matrix to the Portfolio Manager (§1.2), compute portfolio heat
   (§1.3), compute R-multiple (§1.4).
2. **Phase 3 — exits.** Break the `pace` feedback loop by measuring against the
   horizon pinned at entry; give the reviewer memory of its own prior review
   (§1.5); close the first-sale-of-the-day gate loophole; route exits through
   AI Risk; upgrade the reviewer's model; ATR noise band; trailing stops.
3. **Insider routine/opportunistic filter.** Cheap Python, best evidence-to-effort
   ratio in the system — over half of Form 4 trades carry zero predictive power.
4. **Lazy Prices 10-K year-over-year diff.** Text similarity only, no model. The
   filings are already downloaded and stored.
5. **Phase 4 — evidence symmetry and feed repair.** Unblindfold the intraday buy
   path; fix Reuters/AP/FRED; surface degraded coverage to the operator.
6. **Phase 5 — short selling.** Alpaca is confirmed ready: `shorting_enabled:
   true`, `no_shorting: false`, `max_margin_multiplier: 4`, equity above the
   $2,000 floor, assets `shortable` with `borrow_status: easy_to_borrow`. This is
   entirely a code change; no account work is outstanding.
7. **Phase 6 — cost circuit and transparency.** Dollar-based cap with an
   afternoon reserve; `position_id` linking a buy to the sell that closed it;
   surface the reasoning already stored but never displayed.
8. **Phase 7 — measurement.** Backtester and conviction calibration. Must enforce
   post-training-cutoff evaluation windows for any LLM signal — contamination is
   the dominant failure mode in this literature.
9. **Analyst upgrades.** News cascade (dedup, then novelty scoring, then a model
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


### QAMC Remediation Spec — Phase 1 complete, Phase 2 next

`docs/QAMC_REMEDIATION_SPEC.md` Phase 1 (Tech Analyst returns real structure) is
implemented on branch `feat/tech-analyst-structural-levels`:

- `TechAnalysisResult` (`src/models.py`) requires `support_levels`,
  `resistance_levels`, `setup_type` (`"range"` / `"breakout"`),
  `expected_horizon_sessions` and `reference_target` for every actionable rating.
- `src/data/levels.py` (new) deterministically finds support/resistance from the
  full OHLCV history; `src/agents/tech_analyst.py` feeds a formatted levels block
  into the prompt, computed over `trading.lookback_days: 1800` (~5 years).
- `src/portfolio_constructor.py`'s synthesized stop (`entry − 2×ATR` / `entry ×
  0.95`) and target (`entry × (1 + 2×stop_gap_pct)`) are deleted, along with their
  config fields. A candidate with no structural stop or target is now rejected
  rather than traded.
- `tests/test_levels.py` (new, 18 tests) covers the levels module.
- `src/data/context.py` (new) adds deterministic market context per symbol — rel.
  strength vs a same-batch benchmark (SPY/QQQ/IWM), 1w/1m/3m/6m/12m returns, 52-week
  range position, ATR percentage-of-price with a 1y percentile and volatility state,
  MA slopes, consolidation detection, dollar volume, up/down volume ratio, unfilled
  gaps — rendered into the Tech Analyst prompt via `format_context_block()`.
  `src/data/market.py` adds `MarketDataProvider.get_next_earnings_date()`, which the
  Tech Analyst's new optional `days_to_earnings` kwarg can consume, but **nothing in
  the pipeline wires it up yet** — it exists and is tested but unused end to end.
  `tests/test_context.py` (new, 27 tests) covers it; the full suite is 2227 tests.

**Next up is Phase 2 (risk-based sizing and correlation-aware budgeting)**: replace
the Portfolio Manager's percent-of-portfolio conviction sizing with a risk
allocation in `[0.5%, 5.0%]` of equity from which share count is derived
(`shares = (equity × risk_pct) ÷ |entry − stop|`), and add correlation/sector
cluster-aware budgeting so correlated names consume one bet's risk budget rather
than several, per `docs/QAMC_REMEDIATION_SPEC.md` §2. This is not started.
Phases 3–8 (exit rework, evidence/feed repair, short selling, cost/transparency,
measurement, documentation) remain pending behind it, in the spec's stated order.

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
