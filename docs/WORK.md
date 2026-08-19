# QAMC Current Work

Status: **ALPACA PAPER SOAK ACTIVE — TRANCHE: THREE-DEFECT FORENSIC FIX (SGOV liquidity, intraday blind spot, Tech batch loss)**

**2026-08-19 update (new tranche):** The Mission Control cockpit tranche
below (Workstreams A/B/C) is complete and closed — PR #46 merged,
production cutover done, checkpoint satisfied. A separate operator-
directed read-only production forensic performed the same day from the
Ubuntu admin session established three new defects, treated as verified
evidence (do not repeat the forensic unless implementation reveals a
contradiction):

1. **SGOV funding semantics** — PM/RM were shown ~$10K effectively-
   available cash because SGOV was treated as cash-equivalent; actual
   usable cash was ~$145. SGOV was sold to fund approved BUYs, but the
   proceeds weren't there when execution rechecked, so all BUYs were
   safely skipped by the deterministic gate.

   **Resolved (external review round 2).** Verified from Alpaca's official
   docs: `cash` is credited as soon as a SELL *fills*; T+1 gates only
   withdrawal/transfer and the non-marginable (crypto) figure. So filled
   SGOV proceeds DO fund a same-session equity BUY, and the first pass's
   switch to `non_marginable_buying_power` was the wrong field (it lags).
   Margin fields (`buying_power`, `regt_buying_power` — ~2x equity, since
   every Alpaca account is a margin account) are never used. Deployable =
   `cash` + convertible sweep value, both owned assets, so no leverage.
   The real defect was assuming the sale filled: `fund_buys` now reports
   the CONFIRMED rise in raw broker cash and fails closed when it cannot
   confirm. `reserve_pct` reverted 5.0 → 1.0.
2. **Intraday opportunity-discovery blind spot** — the full opportunity-
   generation chain runs once each morning; `intra_check` is loss-
   protection only; midday/close review existing holdings only. A
   material intraday move cannot generate a new trade. Fixed on the
   existing `intra_check` cadence: current-session data, bullish and (via
   existing inverse ETFs) bearish, explicit trigger + dedup/cooldown, no
   morning-stack rerun, no shorting/options/margin, not HFT.

   **Intraday evidence corrected (external review round 2):** the scan
   detected on live prices but handed Tech only completed daily bars
   ending at the prior close, so the triggering move was invisible to the
   analyst. Tech now also receives an explicit `CURRENT SESSION (TODAY,
   INCOMPLETE)` block — live price, move vs prior close, session O/H/L and
   partial-day volume — held separate from the completed-bar series, with
   the indicators flagged as predating the move. An incomplete day is
   never presented as a finished daily bar.
3. **Tech batch-response symbol loss** — symbols passed the pre-filter
   and were sent to `tech_analyst` but silently disappeared during batch
   parsing (one chunk parsed 1/10 symbols). Every submitted symbol must
   reach an explicit terminal outcome (parsed / explicitly neutral-
   rejected / visibly failed) — never a silent drop — with bounded
   retry/recovery for partial batch failures.

This is authorized implementation work under the existing architecture
and hard boundaries (Alpaca Paper-only, cash-only/no-margin, Specialist →
PM → AI Risk → deterministic gate → execution unchanged and final,
no direct shorting/options/margin, no live trading). Implementation
branch: `fix/sgov-liquidity-intraday-batch` off `main` at `4b54d5c`.

**Status: implemented, tested, independently reviewed — awaiting
ChatGPT/operator review and merge. Not merged. Production untouched
(structurally inaccessible from the `dev` account).**

Implementation notes that matter for review:

- **No new daemon, service, timer or database was created.** The intraday
  scan attaches to the *existing* `intra_check` cadence, verified from
  `scripts/run_if_et_window.sh` (fires every 30-min tick, 09:30–16:00 ET)
  rather than assumed.
- **The intraday scan ships DISABLED** (`intraday_scan.enabled: false`).
  It is new autonomous-decision surface, so it is reviewable but inert
  until the operator explicitly enables it — the same rollout pattern
  `cash_sweep` followed.
- **Two concurrency defects were found by verifying infrastructure
  assumptions rather than trusting them**, and both are fixed:
  `run_if_et_window.sh` deliberately exempts `intra_check` from the
  cross-mode session lock, justifying that exemption on the grounds that
  all of intra_check's actions are *idempotent*. Opening a new position
  is not. Fixed with (a) a fail-closed 15-minute DB-row guard against
  racing a morning/midday/close session, and (b) an advisory `flock`
  process mutex against two overlapping `intra_check` processes. (b) was
  added because the "ticks are 1800s apart, hard kill at ~1230s" argument
  that makes overlap impossible lives in a deployment config this code
  cannot read.
- Deterministic risk/execution semantics are unchanged. Execution's final
  raw-cash recheck — the backstop that correctly skipped the BUYs in the
  incident — was deliberately **not** touched.

---

**Prior tranche (2026-08-19, closed):** The Mission Control cockpit
cutover authorized by the contract below is complete. PR #46 (Stages
6–6h) merged to `main` and production cutover completed successfully on
2026-08-19; production is at `7668771`. `/cockpit` and `/ui` are both
confirmed healthy. Alpaca remains Paper-only. See `docs/STATE.md` for the
accepted record. The Checkpoint item below asking Claude to stop for a
cutover decision has been satisfied for this tranche and is preserved
here only as historical/architecture context for the new tranche above.

## Goal

Use the running paper soak to materially improve both **QAMC trading intelligence** and **Mission Control as an operator product**. The current vanilla dashboard remains a useful operational fallback, but it is not the target product.

Do not spend substantial effort polishing or restructuring the existing `src/api/static/` dashboard if that work would be discarded. Build the intended richer Mission Control in parallel against the existing read-only Mission Control API, verify it independently, then cut over only after it is clearly superior and preserves all safety boundaries.

This tranche is not an endless audit. Investigate enough to establish causes, then **plan, implement, test, browser-verify, iterate and document** the fixes that are justified inside the accepted architecture.

## Engineering autonomy

Claude is the engineering lead for this tranche and has broad authority inside the existing architecture to:

- inspect repository and runtime evidence;
- decide routine implementation details;
- use subagents and safe parallel worktrees where useful;
- preserve the current dashboard as a fallback while building the replacement cockpit in parallel;
- create the final frontend structure inside the existing QAMC repository;
- refactor read-side/API code when that improves clarity or provides bounded data aggregation for the new cockpit;
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

### Final Mission Control direction

The existing single-column/vertically stacked vanilla dashboard is a **fallback/baseline**, not the final Mission Control.

Build the richer cockpit **in parallel** rather than performing a throwaway intermediate redesign. The prior QAMC Mission Control architecture and the durable visual board are the primary product references:

- `docs/visual/MISSION_CONTROL_VISION_BOARD.png`
- the original QAMC UI vision/component-map decisions preserved in Git history from the bootstrap architecture
- current `docs/OUTCOME.md`

The original architecture specified a QAMC-native rich frontend with a multi-pane cockpit, financial charts and stronger visualization. The current implementation decision is to re-evaluate the exact frontend stack against today’s repository rather than blindly preserve a historical technology choice. However, **React + Vite + Tailwind, TradingView Lightweight Charts, selective OpenTradex presentation primitives, and Orallexa-style multi-agent decision visualization should be evaluated first because they were the original QAMC design direction.**

Do not import donor trading backends, gateway/data assumptions, mock trading state, or unwanted infrastructure. Reuse presentation components/patterns only where cheaper and cleaner than native implementation.

Target cockpit characteristics:

- compact top account/status strip;
- equity, daily P&L, unrealized P&L, deployable liquidity, sweep parking and directional/risk exposure clearly separated;
- market/regime and system status visible near the top when authoritative evidence exists;
- candidates/watchlist as a first-class working area;
- chart-led market/security context using authoritative market data, including candles/volume/trade markers where practical;
- visually prominent **Specialists → Portfolio Manager → AI Risk Manager → deterministic gate → execution** chain;
- agent cards, recommendation/confidence and disagreement/fusion visualization where supported;
- explicit first-class states for **no trade**, proposed trade, rejected/modified trade and executed trade;
- positions/orders/trades available but not allowed to dominate the entire page;
- journal presented as a decision narrative: thesis, candidates, decisions, missed opportunities, lessons and next posture;
- investigations/forensics that make useful questions easy to answer;
- model/provider/cost/latency and raw technical metadata available through progressive disclosure rather than competing with the trading workflow;
- responsive desktop and iPad layout, including a true multi-pane desktop cockpit rather than a long status page.

The visual board and historical UI architecture are **directional product references**, not permission to fabricate unsupported data or enable old write-control concepts. PAUSE/KILL/trade controls remain out of scope because Mission Control is currently read-only.

### Parallel-build/cutover rule

- Keep the current vanilla dashboard operational while the new cockpit is built.
- Avoid broad refactoring of the legacy frontend unless needed for a bug fix or safe coexistence.
- The replacement should consume the existing read-only Mission Control API wherever practical; add only bounded read-only aggregation where needed.
- Do not alter the trading engine or make the frontend a trading dependency.
- Do not cut over merely because the new frontend builds. Cut over only after browser/runtime verification shows it is materially more useful, truthful and usable on desktop + iPad.
- Preserve an easy rollback path to the current dashboard through the review/cutover checkpoint.

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

## Workstream B — final Mission Control cockpit

Build the replacement cockpit so the main screen answers, at a glance:

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
- substantially improved information density, visualization, hierarchy, spacing and responsive layout compared with the current vanilla page;
- meaningful charting and visual decision-flow components where supported by real data;
- desktop multi-pane layout and iPad verification.

Claude may add the frontend build/tooling needed for the replacement cockpit **inside the existing QAMC repository**. This authorization supersedes the previous generic prohibition on a frontend framework migration for this specific bounded Mission Control replacement. It does **not** authorize new durable backend services, databases, security systems, proxies, trading dependencies or a separate Mission Control repository.

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
- verify representative real/sanitized soak states in the replacement cockpit;
- capture desktop and tablet/iPad-sized screenshots under the existing verification convention;
- compare screenshots directly against `docs/visual/MISSION_CONTROL_VISION_BOARD.png` and the product direction in `OUTCOME.md`;
- iterate if the result still reads like a raw database/status page;
- verify a real no-trade run is understandable from the main screen and traceable into detailed evidence;
- verify SGOV is visually unmistakable as cash-equivalent sweep parking;
- verify the legacy dashboard remains available until final cutover review;
- verify no deterministic risk/execution semantics changed;
- verify Alpaca remains Paper-only;
- verify no secrets enter Git, screenshots, UI or logs.

A technically green replacement that is still materially far from the intended cockpit is **not** complete.

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
- A replacement frontend/framework is authorized only for Mission Control inside the existing repository; no new persistent backend service, durable infrastructure layer, security system, database, proxy, or separate application repository without explicit architecture approval.
- No secrets in Git/chat/logs/screenshots/client evidence.
- No silent model fallback or unrecorded model choice.
- Claude does not push implementation directly to `main` and does not merge its own PR.

## Checkpoint

Continue autonomously until a **meaningful implementation checkpoint** is complete, not merely until an audit or plan is written.

At checkpoint:

1. commit and push the dedicated branch;
2. ensure tests and desktop/iPad browser/runtime verification evidence are recorded;
3. preserve the legacy dashboard until external review approves cutover;
4. update existing `STATE.md` / `WORK.md` only as appropriate — do not create new handoff/status docs;
5. report concisely using **finding → decision → change → evidence → remaining uncertainty**;
6. STOP for independent ChatGPT/operator review and merge/cutover decision.

Satisfied for the Mission Control cockpit tranche: PR #46 merged and cut
over to production on 2026-08-19 (see the 2026-08-19 update at the top
of this file and `docs/STATE.md`).
