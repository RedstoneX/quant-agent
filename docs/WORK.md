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

### Research Intelligence Desk + Smart Money Analyst — AUTHORIZED ENGINEERING TRANCHE

Build one coherent intelligence experience, not a sequence of small dashboard tweaks.

The operator should be able to open Mission Control and quickly understand what every research agent learned, where agents agree/disagree, what the Portfolio Manager decided, what AI Risk changed, what deterministic Python allowed/blocked, what actually happened, and what the system learned afterward — **without reading logs or raw JSON**.

The primary experience should feel like an edited private trading publication: concise, specific, visually balanced, easy to scan, and useful even on quiet/no-trade days. Raw structured evidence remains secondary drill-down, never the main reading experience.

Cover the existing research/review seats where their output is relevant — Technical, News, Macro, Earnings, Portfolio Manager, AI Risk Manager, Position Reviewer, Evening Review and Meta-Reflection — and add one new **Smart Money Analyst** specialist seat.

#### Smart Money Analyst outcome

Use currently free, source-backed alternative data. Current research establishes Bargo's free congressional-trades API as an acceptable initial source: it derives records from official House/Senate STOCK Act disclosures, preserves transaction date versus disclosure date, and requires visible attribution where its data is displayed. Quiver's API is paid and is not authorized as a dependency.

The Smart Money Analyst should turn alternative disclosures into useful research evidence rather than a ticker feed. Congressional activity is delayed disclosure data (potentially weeks after the trade), so a single stale politician transaction must never be represented as a real-time entry signal. Prefer meaningful patterns such as clusters, repeated activity, alignment/conflict with current QAMC themes, and clearly identified data limitations. If other genuinely free, reliable, source-backed smart-money streams (for example insider or institutional disclosures) are available under acceptable terms, Codex may incorporate them into this same seat rather than proliferating agents.

The new seat may inform the Portfolio Manager through the existing specialist-evidence path. It must not bypass PM, AI Risk, deterministic Python or broker protections and must not create a new execution path.

#### Research/reading experience outcome

Desktop should provide a strong default editorial reading order while letting the operator organize the material to suit how they read it. Reuse the already accepted Dockview interaction model so research panels can be moved, resized, tabbed, maximized and persist their layout. iPad should be deliberately composed for reading/navigation rather than a squeezed desktop workspace.

Favor useful editorial synthesis such as daily market thesis, what changed, agent findings, important evidence, disagreement, PM ruling, Risk response, proposed-versus-executed delta, position review, after-the-bell lessons and tomorrow watch. These are outcomes, not a mandatory screen taxonomy; Codex should choose the simplest coherent composition after inspecting the current product.

No fabricated confidence, quotes, history or facts. Sparse, stale, partial, no-news, no-trade and provider-error states must look intentional and remain truthful.

#### Acceptance

This tranche is complete when real stored QAMC data demonstrates that:

1. An operator can read a coherent daily story without opening logs or JSON.
2. Every relevant agent has a useful, visually balanced representation of its belief/findings, strongest evidence, meaningful changes and disagreement where authoritative data supports them.
3. PM/Risk/execution are understandable as deltas: what PM wanted, what Risk changed, what deterministic code allowed/blocked, and what actually executed.
4. Desktop research panels are genuinely movable/resizable/tabbable/maximizable with persisted layout and a sensible default workspace.
5. iPad has a deliberately designed reading/navigation experience with no horizontal overflow or micro-text.
6. Smart Money Analyst is source-backed, provenance/timestamp/lag-aware, attributable, and reaches PM only through the accepted specialist path.
7. Empty, stale, partial and provider-error states are truthful and visually composed.
8. Targeted tests/build pass and rendered desktop+iPad visual acceptance passes with zero console/page errors and no horizontal overflow.

#### Engineering posture

This is outcome-driven work. Codex has autonomy to inspect the current repository, choose the simplest implementation consistent with accepted architecture, make routine engineering/design decisions, implement, test, visually inspect, commit and push **one dedicated branch/PR**. Do not split the work into micro-PRs, repeatedly ask the operator routine design questions, or over-specify implementation from this handoff.

Stop only for a genuine product/safety/architecture conflict that cannot be resolved from current authority. **No merge or production deployment is authorized by this tranche.**

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

**Active gates:** natural Alpaca Paper observation continues; Research Intelligence Desk + Smart Money Analyst engineering may proceed under `ubuntu` through branch/PR only. No trade may be forced or manufactured to create acceptance evidence, and no research-intelligence implementation may be merged or deployed without the normal later gates.
