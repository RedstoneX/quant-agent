# QAMC Current Work

Status: **COST CIRCUIT LATCHED | NON-LLM SAFETY ACTIVE | PAID ANALYSIS SUSPENDED**

## Current integration truth

- Production is deployed and verified at
  `9c24f78ec99dbcafa62413858aff7e735ae10dbd`; rollback SHA is
  `d645ef2d61d8ba4c06dd18c40b0ae44334462cec`.
- PR #81 deployed the persistent mandatory cost circuit and PM/pipeline
  remediation. PR #83 fixed the intraday lock context manager that the first
  natural suspended tick exposed. The full suite passed 2,110 tests and the
  deployed intraday suite passed 24 tests.
- Production seeded **$4.211481** of existing ET-day spend and latched the
  **$1.50** daily limit with `alert_state=1`. The required Telegram shutdown
  alert was delivered. Do not reset while the unchanged ledger is over limit.
- A post-deployment midday run reconciled both broker-protected long positions,
  then returned `paid_analysis_suspended` before any model request. Agent-log
  ID 189 and ET-day spend remained unchanged.
- The 18:30 UTC intraday tick also reconciled both protected longs and blocked
  before Tech with zero spend. Its initial structured suspension was masked by
  `generator didn't stop after throw()`; PR #83 now propagates the suspension
  through the advisory lock correctly. The latch was never reset, max agent-log
  ID remains 189 and incremental circuit spend remains $0.00.
- PR #69 fixed the intraday chart data path by explicitly requesting Alpaca IEX for 5m/15m/1h bars. Production verification reported non-empty SPY/AAPL bars and working `5m Today`, `15m`, `1h`, and `1D` chart controls.
- The chart live-price/current-price truth issue was already fixed by commit `796558f184f8dd800c7e1cbb57f11173ad3d6f6b`. It is not an outstanding task.
- The previously flagged `get_latest_price` missing-feed concern is not an established defect: Alpaca latest trade/quote requests default to the best feed available to the subscription; current probes show IEX succeeds and explicitly requested SIP is rejected as unsubscribed, as expected. The method's `None` result on an actual API exception is intentional and tested fail-closed behavior.
- Production remains Alpaca Paper. The seven existing timers remained intact, Mission Control remained private/read-only, and `config/settings.yaml: intraday_scan.enabled: true` was preserved.
- GitHub `main` may move ahead with documentation or later accepted work. **Production does not automatically follow `main`.**
- PRs #74, #75, #76 and #77 are merged and deployed. Backend recovery and the final Mission Control finish line are complete.
- PR #79 is merged and deployed. The Research Desk passed production desktop
  and iPad verification against real stored QAMC data; the API remained
  read-only and all seven timers remained intact.
- PR #87 is merged and deployed. It finishes the Research Desk editorial and
  visual acceptance: evidence-backed change/tension/why-now, restrained desk
  annotation and semantic seat language, technical setup context, run-local
  PM/Risk/gate/execution deltas, canonical SEC findings and standalone
  run-scoped admissions, complete after-bell parsing, persistent Dockview
  maximize behavior, and deliberately composed iPad/empty/stale/partial states.
  Production desktop and iPad inspection passed with the canonical stored data,
  no console/request errors, no horizontal overflow, no micro-text and no raw
  failure/telemetry copy. The API remains read-only and all seven timers remain
  intact.
- PR #85 replaced the blocked Bargo adapter with first-party SEC Form 4 and
  enabled the Smart Money seat. Its source-only production preflight processed
  25 official filings, cached 13 exact P/S observations and retained five
  material rows for SPIR/TISI. No symbol cleared the higher external-admission
  threshold, no LLM call occurred, max agent-log ID remained 189 and the cost
  circuit remained latched at the unchanged $4.211481.
- Run-scoped automatic admission is active for qualifying symbols outside the
  configured 77-stock universe. The permanent universe is unchanged; the lane
  is capped at three names and remains behind broker/common-stock, price,
  history, liquidity, sector, Technical, PM, AI Risk and deterministic gates.
- Engineering passed 2,127 tests; the deployed focused suite passed 272 tests.
- Deployment verification passed `/health`, DB, broker, Alpaca Paper, OpenRouter per-seat routing including PM `openai/gpt-5.5`, private OneCLI, Telegram configuration, `/cockpit`, accepted chart timeframes, read-only method rejection, and all seven existing timers.
- The only tracked production delta remains `config/settings.yaml: intraday_scan.enabled: true`.

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

**Active work:** natural Alpaca Paper observation continues. The Research Desk
tranche and deterministic SEC source/admission presentation are complete. Paid
Smart Money synthesis and natural transient-candidate evidence remain pending
because paid research is deliberately suspended; this is not a blocker for the
completed UI/editorial work. Do not reset the circuit until it is reviewed and
reset with an auditable reason. Deterministic safety observation continues. No
trade may be forced or manufactured to create acceptance evidence.
