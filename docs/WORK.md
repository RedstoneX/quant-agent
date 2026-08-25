# QAMC Current Work

Status: **FINISH LINE DEPLOYED | ALPACA PAPER NATURAL ACCEPTANCE + RESEARCH INTELLIGENCE TRANCHE**

## Current integration truth

- Production is deployed and verified at
  `16c52715b3ee05ec9e38c12958a14ee77a6d38d7`; rollback SHA is
  `a6758f935910c5cf380cc6a7acedc5f3b78f6366`.
- PR #69 fixed the intraday chart data path by explicitly requesting Alpaca IEX for 5m/15m/1h bars. Production verification reported non-empty SPY/AAPL bars and working `5m Today`, `15m`, `1h`, and `1D` chart controls.
- The chart live-price/current-price truth issue was already fixed by commit `796558f184f8dd800c7e1cbb57f11173ad3d6f6b`. It is not an outstanding task.
- The previously flagged `get_latest_price` missing-feed concern is not an established defect: Alpaca latest trade/quote requests default to the best feed available to the subscription; current probes show IEX succeeds and explicitly requested SIP is rejected as unsubscribed, as expected. The method's `None` result on an actual API exception is intentional and tested fail-closed behavior.
- Production remains Alpaca Paper. The seven existing timers remained intact, Mission Control remained private/read-only, and `config/settings.yaml: intraday_scan.enabled: true` was preserved.
- GitHub `main` may move ahead with documentation or later accepted work. **Production does not automatically follow `main`.**
- PRs #74, #75, #76 and #77 are merged and deployed. Backend recovery and the
  final Mission Control finish line are complete.
- Deployment verification passed `/health`, DB, broker, Alpaca Paper,
  OpenRouter per-seat routing including PM `openai/gpt-5.5`, private OneCLI,
  Telegram configuration, `/cockpit`, accepted chart timeframes, read-only
  method rejection, and all seven existing timers.
- The only tracked production delta remains
  `config/settings.yaml: intraday_scan.enabled: true`.

## Stabilization account model — HARD RULE

Use two active accounts until QAMC is stable:

### `ubuntu` — engineering/operator

Use `ubuntu` for Codex sessions, engineering checkout/worktrees outside `/home/qamc`, Git/GitHub, development tooling, targeted tests/builds, private Tailscale Vite/browser work, Docker/sudo engineering tasks, and approved deployment orchestration.

### `qamc` — runtime only

`qamc` owns `/home/qamc/quant-agent`, runtime `.env`/OneCLI wiring, user services/timers, and QAMC Paper execution. Do not run Codex as `qamc` or turn it into a general engineering account.

### `dev` — parked

Do not use `dev` in the normal workflow or expand its permissions during stabilization.

## Standing delivery workflow — HARD RULE

Engineering inside an already-authorized task is autonomous under `ubuntu`: diagnose, implement, test, preview, browser-verify, commit and push without repeated operator prompts.

Implementation promotion remains reviewable. Codex does not independently merge its own implementation work or mutate the `qamc` runtime unless that promotion is explicitly authorized. Once production deployment is authorized, `ubuntu` performs the shortest safe privileged deploy/verify operation directly; do not bounce the operator among accounts.

## Friction-reduction rules

1. No normal use of `dev` and no manual account ping-pong.
2. Private DEV preview/browser verification is standing-authorized for relevant engineering work.
3. For bounded fixes, read only current authority plus relevant code and run the shortest decisive verification.
4. Targeted tests first; no default full-suite, commissioning rerun, multi-agent fan-out or broad repository archaeology.
5. Stop when the requested result is proven; repeated re-validation without new evidence is not diligence.
6. Keep handoffs concise: changed / verified / unresolved blocker / exact promotion state.
7. Preserve the `ubuntu` engineering vs `qamc` runtime boundary; do not add new lockdown/security infrastructure during stabilization without a real need.
8. Do not infer current defects from historical notes. Reopen a resolved area only from current operator or production evidence.
9. After an authorized production change, bundle preflight/deploy/restart/acceptance into one safe intervention.

## Active finish line

### Mission Control / existing cockpit utility

Complete. PRs #76 and #77 are merged and deployed with the backend recovery
from PRs #74 and #75. Preserve the accepted live cockpit unless current evidence
or the research-intelligence outcome below requires a coherent extension.

### Research Intelligence Desk + Smart Money Analyst — AUTHORIZED END-TO-END TRANCHE

Build one coherent intelligence experience, not a sequence of small dashboard tweaks.

The operator should be able to open Mission Control and quickly understand what every research agent learned, where agents agree/disagree, what the Portfolio Manager decided, what AI Risk changed, what deterministic Python allowed/blocked, what actually happened, and what the system learned afterward — **without reading logs or raw JSON**.

The experience is for one private operator, not customers. It should feel like a sharp internal trading desk: useful, candid, visually interesting, occasionally dry or irreverent, and never corporate. **Say everything useful. Nothing merely decorative.**

Cover the existing research/review seats where their output is relevant — Technical, News, Macro, Earnings, Portfolio Manager, AI Risk Manager, Position Reviewer, Evening Review and Meta-Reflection — and add one new **Smart Money Analyst** specialist seat.

#### Smart Money Analyst outcome

Use currently free, source-backed alternative data. Current research establishes Bargo as an acceptable initial source. Its free congressional API derives records from official House/Senate STOCK Act disclosures and preserves transaction date versus disclosure date. Its broader MCP is currently free in beta and advertises additional source-backed streams including insider transactions, 13F holdings, options flow, analyst sentiment and related market intelligence. Quiver's API is paid and is not authorized as a dependency.

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
8. Smart Money Analyst is source-backed, provenance/timestamp/lag-aware, attributable, separates timely from stale evidence, suppresses noise, and reaches PM only through the accepted specialist path.
9. Empty, stale, partial and provider-error states are truthful and visually composed.
10. Targeted tests/build pass and rendered desktop+iPad visual acceptance passes with zero console/page errors and no horizontal overflow.

#### Engineering and promotion authorization

This is outcome-driven work. Codex has autonomy to inspect the current repository, choose the simplest implementation consistent with accepted architecture, make routine engineering/design decisions, implement, test, visually inspect, commit and push **one dedicated branch/PR**. Do not split the work into micro-PRs, repeatedly ask the operator routine design questions, or over-specify implementation from this handoff.

**For this tranche specifically, the operator explicitly authorizes the complete engineering → merge → production-deploy → production-verify sequence once Codex has satisfied the acceptance criteria above.** This is the explicit promotion authorization required by `CLAUDE.md`; it is not a standing permission for unrelated future work.

Codex may merge its own dedicated PR for this tranche after verifying the final PR head and acceptance evidence, then use `ubuntu` to perform the shortest governed privileged convergence against the existing `qamc` runtime and verify production. Preserve the `ubuntu` engineering/operator versus `qamc` runtime-only boundary. Do not alter secrets architecture, add unrelated infrastructure, force trades, weaken safety, or broaden broker authorization. If deployment verification fails, stop further mutation, preserve/restore the last known-good production state using the existing governed rollback path, and report the concrete blocker.

Stop for the operator only on a genuine unresolved product/safety/architecture conflict, a paid dependency, or an external credential/authorization requirement that cannot be satisfied from existing project resources.

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

**Active gates:** natural Alpaca Paper observation continues; Research Intelligence Desk + Smart Money Analyst is authorized end-to-end through its dedicated PR, merge, governed production deployment and production verification once its acceptance criteria pass. No trade may be forced or manufactured to create acceptance evidence.
