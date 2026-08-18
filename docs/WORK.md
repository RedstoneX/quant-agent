# QAMC Current Work

Status: **ALPACA PAPER SOAK ACTIVE — AUTONOMOUS PRODUCT IMPROVEMENT TRANCHE AUTHORIZED**

## Goal

Use the running paper soak to materially improve both **QAMC trading intelligence** and **Mission Control as an operator product**. The current dashboard is functionally useful but materially short of the intended Mission Control cockpit, and the recent market decline is a live test of whether QAMC's supported bearish path is genuinely usable.

This tranche is not an endless audit. Investigate enough to establish causes, then **plan, implement, test, browser-verify, iterate and document** the fixes that are justified inside the accepted architecture.

## Engineering autonomy

Claude is the engineering lead for this tranche and has broad authority inside the existing architecture to:

- inspect repository and runtime evidence;
- decide routine implementation details;
- use subagents and safe parallel worktrees where useful;
- refactor read-side/API/UI code when that improves clarity or maintainability;
- add bounded read-only derived API fields/endpoints when required by the product outcome;
- improve prompts/candidate plumbing when evidence proves a behavioural blind spot;
- implement dashboard information architecture and visual changes;
- run focused and full tests;
- use browser/runtime verification and screenshots;
- update existing authoritative documentation;
- push a dedicated implementation branch for external review.

Do **not** repeatedly stop after discovery or planning. Stop for the operator only when required by credentials/privilege, a genuine unresolved product/value choice, or a material architecture/safety fork prohibited by `CLAUDE.md`.

## Product decisions already made

### Directionality

QAMC is **not intended to be structurally long-only**. Within the currently supported instruments and existing safety architecture, it should be able to express bullish, bearish or neutral/cash views.

Current approved bearish expression is through inverse ETFs already in the universe (`SH`, `SDS`, `PSQ`, `SQQQ`). This tranche does **not** authorize direct stock shorting, options/theta strategies, margin, or deterministic risk/execution redesign.

SGOV is **cash-equivalent sweep parking**, not a PM investment thesis. Mission Control must never make it look like the AI deliberately put the portfolio into bonds as its main risk allocation.

### Mission Control visual/product direction

The existing single-column/vertically stacked dashboard is a baseline, not the target product.

The prior QAMC Mission Control vision is reaffirmed as the **layout and information-hierarchy target**: a dense, coherent desktop trading cockpit that remains usable on iPad, with the most important trading state visible without long scrolling or raw-log archaeology.

Converge toward these characteristics using the data and architecture QAMC actually has:

- compact top account/status strip;
- equity, daily P&L, unrealized P&L, deployable liquidity, sweep parking and directional/risk exposure clearly separated;
- market/regime and system status visible near the top when authoritative evidence exists;
- candidates/watchlist as a first-class working area rather than chips buried deep in a journal;
- chart-led market/security context where existing data can support it without inventing a new trading/data platform;
- visually prominent **Specialists → Portfolio Manager → AI Risk Manager → deterministic gate → execution** chain;
- explicit first-class states for **no trade**, proposed trade, rejected/modified trade and executed trade;
- positions/orders/trades available but not allowed to dominate the entire page;
- journal presented as a decision narrative: thesis, candidates, decisions, missed opportunities, lessons and next posture;
- investigations/forensics that make useful questions easy to answer (e.g. PM wanted risk but RM reduced it; deterministic gate rejected a later winner; expensive deliberation produced no trade; bearish opportunity was missed);
- model/provider/cost/latency and raw technical metadata available through progressive disclosure rather than competing with the trading workflow;
- responsive desktop/tablet layout with materially less unnecessary vertical stacking.

Use the vision as a product reference, not permission to fabricate features. In particular, the old mockup's PAUSE/KILL/trade controls remain **out of scope** because Mission Control is currently read-only.

## Workstream A — one-pass directionality forensic + correction

Reconstruct the relevant Aug 17–18 scheduled paper sessions once from authoritative runtime/database evidence. Follow the actual chain through market/regime evidence, inverse-ETF technical evidence, candidates, PM, RM, deterministic gate, execution and evening review.

Classify the actual cause of no meaningful risk deployment. Possible causes include:

- no qualified bearish setup;
- bearish evidence never reached candidate/PM consideration;
- PM recognized the decline but deliberately stayed neutral;
- inherited long-participation priors or prompt framing distorted the decision;
- RM vetoed/scaled it;
- deterministic risk blocked it;
- runtime/data failure suppressed an otherwise valid path.

Do not stop at the classification. If the evidence shows a genuine blind spot, correct the smallest responsible layer and add a regression fixture. Valid corrections can include explicit inverse-ETF consideration under materially bearish evidence, candidate plumbing fixes, horizon wording that distinguishes swing trading from long-only thinking, or rebalancing an inherited prior that is demonstrably distorting current-account decisions.

Do not hindsight-fit the system merely because a recent decline is obvious after the fact.

## Workstream B — Mission Control redesign + explainability

Redesign the existing read-only Mission Control so the main screen answers, at a glance:

1. **What do I own and how much real risk is deployed?**
2. **What does QAMC think the market is doing?**
3. **What opportunities/candidates is it looking at?**
4. **What did the specialists, PM, Risk and deterministic gate decide?**
5. **Why did it trade — or why did it not trade?**
6. **What did it miss and what is it learning from that?**
7. **Is the system healthy and what did the AI cost?**

Required concrete outcomes include:

- honest raw cash / SGOV sweep / deployable liquidity / risk exposure presentation;
- a latest substantive run decision funnel with a concise dominant **WHY NO TRADE?** explanation when appropriate;
- latest available directional/regime posture with honest unknown states;
- inverse-ETF consideration visible when relevant;
- missed UP and DOWN opportunities visible from journal/evening evidence;
- existing PM/RM/specialist/model/cost details preserved in drill-down;
- substantially improved information density, hierarchy, spacing and responsive layout compared with the current vertically stacked page.

Claude may reorganize the UI significantly and may add bounded read-only API aggregation required to support this presentation. Do not introduce a new framework, persistent service, database, observability platform or trading-critical dependency without architectural approval.

## Workstream C — operator usefulness and experiment measurement

Review the current product for other high-value improvements that are already supported by existing data and architecture. Implement them when they clearly advance `OUTCOME.md` without creating major new infrastructure.

Examples include:

- benchmark/account performance context where existing history supports it;
- clearer AI-vs-deterministic attribution if existing records permit a truthful comparison;
- more useful journal summaries and investigations;
- clearer model/provider/cost/latency reporting;
- better navigation/progressive disclosure;
- fixes for misleading empty states, labels or terminology exposed by real soak use.

Do not add decorative complexity or unsupported vanity metrics.

## Verification / iteration

Treat browser appearance as an acceptance dimension, not an afterthought.

Before stopping:

- run focused tests continuously and the full automated suite at the checkpoint;
- verify representative real/sanitized soak states in the browser;
- capture desktop and tablet/iPad-sized screenshots under the existing verification convention;
- compare screenshots against the product direction in `OUTCOME.md` and iterate if the result still reads like a raw database/status page;
- verify a real no-trade run is understandable from the main screen and traceable into detailed evidence;
- verify SGOV is visually unmistakable as cash-equivalent sweep parking;
- verify no deterministic risk/execution semantics changed;
- verify Alpaca remains Paper-only;
- verify no secrets enter Git, screenshots, UI or logs.

A technically green page that is still materially far from the intended cockpit is **not** complete.

## Hard boundaries

- Alpaca **Paper only**; no live trading.
- Preserve Specialist Agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution.
- Deterministic Python and broker protections remain final safety/execution authority.
- No deterministic risk/execution semantic redesign.
- No direct stock shorting, negative target weights, options trading or margin in this tranche.
- Mission Control remains read-only and non-critical to trading; no broker-write controls.
- No public services; Mission Control remains tailnet/private.
- Preserve `dev` / `qamc` / `ubuntu` isolation.
- Keep upstream OneCLI as the credential layer.
- No new persistent service, durable infrastructure layer, security system, database, proxy, framework migration or custom replacement for an approved component without explicit architecture approval.
- No secrets in Git/chat/logs/screenshots/client evidence.
- No silent model fallback or unrecorded model choice.
- Claude does not push implementation directly to `main` and does not merge its own PR.

## Checkpoint

Continue autonomously until a **meaningful implementation checkpoint** is complete, not merely until an audit or plan is written.

At checkpoint:

1. commit and push the dedicated branch;
2. ensure tests and browser/runtime verification evidence are recorded;
3. update existing `STATE.md` / `WORK.md` only as appropriate — do not create new handoff/status docs;
4. report concisely using **finding → decision → change → evidence → remaining uncertainty**;
5. STOP for independent ChatGPT/operator review and merge decision.
