# QAMC Current Work

Status: **TRADING-UTILITY RECOVERY — MERGED; GOVERNED DEPLOYMENT + NATURAL VALIDATION PENDING | DASHBOARD PRODUCT CONVERGENCE AUTHORIZED CONCURRENTLY**

## Current integration truth

- Trading-utility recovery branch: `fix/trading-utility-conversion`.
- GitHub PR **#56 — `fix(qamc): restore trading-utility conversion path`** merged into `main` on 2026-08-20.
- Merge commit: `d14e28dfc63ca6e4da920229b0ab5ba0f33b93df`.
- Reviewed recovery head before merge: `04f6f76a65f7c02891449a243320977695523117`.
- Production is **not yet assumed to be running the recovery**; `docs/STATE.md` remains authoritative for the deployed SHA until governed deployment evidence says otherwise.
- The earlier temporary note claiming PR #56 was unrelated is superseded by GitHub's authoritative PR record.

## Terminology / workstream separation

- **QAMC / Mission Control** = the whole product/system.
- **Dashboard** = the browser/iPad read-side UI and the concurrent frontend/UX workstream.
- **Core recovery** = the trading/backend deployment-and-natural-validation workstream.
- Keep Dashboard and core recovery isolated in separate branches/worktrees. Do not call the Dashboard workstream “Mission Control” because that name refers to the whole product.

## Product/architecture principle

QAMC is an autonomous Alpaca trading system, not a separate “paper-trading architecture.” Alpaca Paper is the **currently authorized execution environment** used to validate the system before any future live-capital authorization.

The same trading-critical architecture must apply across environments:

**discovery → Specialists → Portfolio Manager → AI Risk Manager → deterministic gate → funding → broker execution → position/exit management → reflection.**

Paper/live differences belong at the broker/configuration boundary (credentials, endpoint/account selection and genuine execution-mechanics differences). Do not create paper-specific decision logic, weaker safety, alternate position management, or shortcuts that would require a later live-trading rearchitecture.

## Core recovery — accepted changes

Production forensics across the natural validation runs found that QAMC was often analyzing legitimate opportunities without converting them into exposure for mechanical reasons rather than deliberate investment judgment. The reviewed recovery addresses the evidenced blockers:

1. **PM parse destruction** — nested `targets` fragments could outscore and replace the complete PM decision. Parser selection is corrected and production payloads are regression-pinned.
2. **SGOV funding race** — approved BUYs could be abandoned before the funding SELL completed. Funding now waits/polls within a bounded fail-closed window and execution still requires confirmed broker cash.
3. **Schema-complete decisions rejected** — approving PM/Risk decisions could die on missing narrative schema fields. One bounded repair is allowed only for non-decision fields; decision-bearing content is strictly preserved or the run fails closed.
4. **Invisible execution kills** — deterministic BUY skips were log-only. Skip reasons are now durable evidence, surfaced in the funnel, and fully unfunded approved mornings become safely retryable through the full decision chain.
5. **Artificial macro conservatism** — transient FRED failures and impossible freshness rules suppressed confidence. FRED gets bounded retry/breaker behavior and staleness now follows each series' actual cadence.
6. **Risk misread deterministic sizing** — constructor risk-budget caps are now explicitly identified so AI Risk does not mistake deterministic sizing for PM inconsistency.

Full branch suite reported after final review fixes: **1997 passed**.

## Core next authorized work

1. Deploy the exact accepted merged recovery SHA through the existing governed production rollout path, preserving the current Alpaca Paper authorization and production-specific intraday enablement.
2. Verify services/timers/API/Telegram/provider wiring and the new recovery behavior.
3. Allow natural sessions to provide the real evidence for opportunity → decision → execution → position-management performance.

Deployment passing proves the machinery, not trading success.

## Natural validation required

Before declaring the recovery successful, natural market sessions should demonstrate:

- worthwhile opportunities can survive discovery and reach PM/Risk;
- defensible eligible trades can reach funded broker submission;
- supported bearish opportunities can be expressed through the approved inverse ETFs;
- no-trade decisions remain possible and explainable;
- position management and exits behave coherently after entry;
- execution/funding failures are visible rather than silently interpreted as investment decisions;
- observed performance and missed opportunities can be measured without hindsight tuning.

Success is **not** “more trades.” Do not force activity, weaken safety, or hindsight-tune. When QAMC does not trade, the reason must be specific and defensible.

## Concurrent Dashboard product convergence — authorized

Dashboard work may proceed **concurrently** with core deployment/validation on a separate branch/worktree because it is read-side/product work and should not block natural trading evidence.

The governing reference is `docs/visual/MISSION_CONTROL_VISION_BOARD.png` together with `docs/OUTCOME.md`. The earlier donor work is part of the intended product direction and must not be silently lost while fixing semantic correctness.

### Separation from the core recovery

The concurrent Dashboard stream may change frontend/read-side presentation and read-only API/schema support needed to represent existing authoritative trading evidence correctly.

It must **not**:
- change trading decisions, risk thresholds, execution eligibility, broker writes or position-management semantics;
- introduce a second trading-memory/decision system;
- invent data to satisfy a visual mockup;
- collide with the production-deployment worktree/branch;
- use broad dashboard redesign as a reason to delay the merged recovery's deployment or natural validation.

If a Dashboard requirement exposes missing trading telemetry, add only the minimum read-side evidence/contract needed to expose already-authoritative state; escalate if satisfying the UI would require a material trading-core change.

### Product priorities

The QAMC Dashboard should converge toward the vision board's **graphical trading cockpit**, not a cleaner admin/log page.

Prioritize:
1. **Truth and attribution** — fix candidate/run attribution, execution provenance, capital/liquidity semantics and misleading labels before decoration.
2. **Capital/exposure clarity** — distinguish raw cash, SGOV sweep liquidity, reserve, capital available and actual market-risk exposure.
3. **Opportunity/decision funnel** — compress runs into meaningful progression such as `screened → advanced → proposed → risk-modified/rejected → deterministic gate → executed`, with drill-down rather than giant ticker dumps.
4. **Oralexa-style agent cards** — compact specialist cards with stance, key evidence, disagreement and genuine confidence/calibration only where authoritative.
5. **Debate / signal fusion** — visually show specialist agreement/conflict and how it flows into PM intent.
6. **PM / Risk / deterministic gate cards** — emphasize what PM proposed, what Risk changed, what deterministic Python allowed/blocked, and what actually executed.
7. **Chart-led candidate context** — use TradingView Lightweight Charts concepts (candlesticks, indicators, volume, trade markers, streaming updates) where existing authoritative data supports them.
8. **Structured Journal Day page** — summarize the day into Market Thesis; Watchlist/Candidates; Agent Analysis; Disagreements; PM Proposal; Risk Review; Proposed→Executed Difference; Trades; Daily Result; Lesson Learned; Tomorrow/monitoring. Raw event logs remain forensic drill-down, not the primary journal UX.
9. **Learning Center** — preserve useful Oralexa donor ideas such as scoreboards and bias/self-correction presentation only when backed by real QAMC metrics/evidence.
10. **Desktop + iPad first** — phone is secondary.

### Donor decisions from the vision board

**OpenTradex — adapt/keep:** resizable panes, terminal-style layout, top account bar, public components and the run-control visual concept where safe/read-only. **Discard:** its trading engine/gateway, command chat and internal data models.

**Oralexa — adapt/keep:** agent cards, debate/signal fusion, PM decision cards, scoreboards and bias/self-correction UI. **Do not use:** mock-data layers or Oralexa trading logic.

**TradingView Lightweight Charts — use/adapt:** candlesticks, indicators, volume, trade markers and streaming updates where QAMC data supports them truthfully.

### Known dashboard correctness debt to carry forward

The prior visual/source audit found issues that remain acceptance gates for product work:
- Candidate Detail can misattribute run-wide PM/Risk evidence to an individual ticker.
- Latest empty runs can erase more meaningful session context.
- SGOV cash parking is too visually dominant.
- “Deployable” liquidity presentation is misleading.
- Journal/Run Detail are too verbose and developer-oriented.
- SGOV cash-management trades and strategy trades need clearer separation.
- Empty chart states consume excessive space.
- Decision Room should explain decision deltas rather than dump fields.
- Missed Opportunities empty state should look intentional.
- Invented pseudo-confidence percentages and arbitrary UI risk thresholds must be removed or tied to authoritative data.
- Labels such as “Rejected by specialist” must not overstate what actually occurred.

## Remaining core uncertainty

The trading fixes are strongly supported by recorded production evidence and deterministic tests but have not yet been validated as a complete chain after deployment. Two lower-priority observed issues remain outside the core recovery gate unless they materially distort validation: news-narrative factual drift and `actual_provider` attribution oddity.

## Hard boundaries

- **Current execution authorization is Alpaca Paper only.** Live-broker order submission requires a separate future authorization; this is an environment/safety gate, not a separate trading architecture.
- No margin, options or direct stock shorting. Bearish expression remains through approved inverse ETFs.
- Deterministic Python/broker protections remain final safety authority.
- Do not force/manufacture trades or weaken safeguards merely to increase activity.
- Do not introduce paper-only trading semantics that would need replacement for live-capital operation.
- No new daemon/service/database/proxy/security/credential architecture without explicit approval.
- Preserve `dev` / `qamc` / `ubuntu` isolation and OneCLI secret handling.
- Dashboard remains private/read-only; Telegram remains output-only.
- Dashboard work must remain non-critical to trading and must not delay core validation.
- Claude does not merge or deploy its own work; external integration and governed deployment remain required.
