# QAMC Current Work

Status: **TRADING-UTILITY RECOVERY — DEPLOYED AND VERIFIED; AWAITING NATURAL PAPER-MARKET EVIDENCE**

## Current integration truth

PR #56 (trading-utility recovery) is merged to `main` and deployed to production at `d14e28dfc63ca6e4da920229b0ab5ba0f33b93df`, via the governed rollout script (`ops/review/qamc-recovery-rollout.sh` on `claude/trading-utility-recovery-rollout`), run by the operator as `ubuntu` on 2026-08-20 23:58:56 UTC and ending `GATE E / FINISH LINE PASSED`. It was independently corroborated live from `dev` immediately after (`health=ok`, `paper=true`, `/cockpit` and `/ui` 200). Full deployment evidence and rollout-review history are recorded in `docs/STATE.md` and `ops/review/README-recovery-rollout.md`; they are not duplicated here.

Deployment proves the machinery is wired correctly. It does not prove the recovery works — see Goal and Natural validation required below.

## Product/architecture principle

QAMC is an autonomous Alpaca trading system, not a separate “paper-trading architecture.” Alpaca Paper is the **currently authorized execution environment** used to validate the system before any future live-capital authorization.

The same trading-critical architecture must apply across environments:

**discovery → Specialists → Portfolio Manager → AI Risk Manager → deterministic gate → funding → broker execution → position/exit management → reflection.**

Paper/live differences belong at the broker/configuration boundary (credentials, endpoint/account selection and genuine execution-mechanics differences). Do not create paper-specific decision logic, weaker safety, alternate position management, or shortcuts that would require a later live-trading rearchitecture.

## Recovery finding and accepted changes

Production forensics across the natural validation runs found that QAMC was often analyzing legitimate opportunities without converting them into exposure for mechanical reasons rather than deliberate investment judgment. The reviewed recovery addresses seven evidenced blockers:

1. **PM parse destruction** — nested `targets` fragments could outscore and replace the complete PM decision. Parser selection is corrected and production payloads are regression-pinned.
2. **SGOV funding race** — approved BUYs could be abandoned before the funding SELL completed. Funding now waits/polls within a bounded fail-closed window and execution still requires confirmed broker cash.
3. **Schema-complete decisions rejected** — approving PM/Risk decisions could die on missing narrative schema fields. One bounded repair is allowed only for non-decision fields; decision-bearing content is strictly preserved or the run fails closed.
4. **Invisible execution kills** — deterministic BUY skips were log-only. Skip reasons are now durable evidence and surfaced in the funnel.
5. **Fully unfunded approved BUYs lost** — approved morning runs that could not obtain funding now return a safely retryable status through the full decision chain rather than silently terminating as if no investment opportunity existed.
6. **Artificial macro conservatism** — transient FRED failures and impossible freshness rules suppressed confidence. FRED gets bounded retry/breaker behavior and staleness now follows each series' actual cadence.
7. **Risk misread deterministic sizing** — constructor risk-budget caps are now explicitly identified so AI Risk does not mistake deterministic sizing for PM inconsistency.

Full reviewed recovery suite: **1997 passed**. The deployed Gate C focused suite passed **163/163**.

## Goal

Use natural market evidence to determine whether QAMC reliably:

**finds opportunity → evaluates it → makes a defensible bullish, bearish or neutral decision → executes when eligible → manages/exits the position → measures the result.**

Success is **not** “more trades.” Do not force activity, weaken safety, or hindsight-tune. When QAMC does not trade, the reason must be specific and defensible.

## Next authorized work

1. ~~Complete external GitHub integration of PR #56.~~ Done — merged to `main` at `d14e28d`.
2. ~~Deploy the exact accepted merged SHA through the existing governed rollout path.~~ Done — `GATE E / FINISH LINE PASSED`, verified live and recorded in `docs/STATE.md`.
3. Observe natural Alpaca Paper sessions against the criteria below. No forcing, manufacturing or hindsight tuning. The next engineering work should be justified by observed evidence rather than by a target trade count.

Deployment passing proved the machinery, not trading success.

## Natural validation required

Before declaring the recovery successful, natural market sessions should demonstrate:

- worthwhile opportunities can survive discovery and reach PM/Risk;
- defensible eligible trades can reach funded broker submission;
- supported bearish opportunities can be expressed through the approved inverse ETFs;
- no-trade decisions remain possible and explainable;
- position management and exits behave coherently after entry;
- execution/funding failures are visible rather than silently interpreted as investment decisions;
- observed performance and missed opportunities can be measured without hindsight tuning.

## Remaining uncertainty

The fixes are strongly supported by recorded production evidence and deterministic tests but have not yet been validated as a complete chain after deployment. Two lower-priority observed issues remain outside this recovery gate unless they materially distort validation: news-narrative factual drift and `actual_provider` attribution oddity.

## Secondary product debt

Mission Control still has semantic/usability debt, including candidate/run attribution and liquidity presentation. Read-side correctness needed for trading diagnosis is valid; broad dashboard redesign remains secondary to proving trading utility.

## Hard boundaries

- **Current execution authorization is Alpaca Paper only.** Live-broker order submission requires a separate future authorization; this is an environment/safety gate, not a separate trading architecture.
- No margin, options or direct stock shorting. Bearish expression remains through approved inverse ETFs.
- Deterministic Python/broker protections remain final safety authority.
- Do not force/manufacture trades or weaken safeguards merely to increase activity.
- Do not introduce paper-only trading semantics that would need replacement for live-capital operation.
- No new daemon/service/database/proxy/security/credential architecture without explicit approval.
- Preserve `dev` / `qamc` / `ubuntu` isolation and OneCLI secret handling.
- Mission Control remains private/read-only; Telegram remains output-only.
- Claude does not merge or deploy its own work; external integration and governed deployment remain required.

## Concurrent authorized tranche — Mission Control data truth, run history & decision explainability

Status: **AUTHORIZED BY OPERATOR (direct, detailed instruction), IN PROGRESS on `claude/mission-control-data-correctness`.** Bounded read-side correctness work, exercising the "Secondary product debt" allowance above (candidate/run attribution, liquidity/pricing presentation). Does not touch the natural-validation gate above, does not change trading/risk behavior, and does not block or accelerate it.

Repository investigation (2026-08-21) confirmed six concrete defects, all inside `src/api/*` + `frontend/src/*` (no trading-core files touched):

1. **Pricing truth** — `PositionItem.current_price` (`src/api/broker_reads.py::read_positions`) already comes live from `AlpacaBroker.get_positions()`; `/prices/{symbol}` (`read_price_bars`) is daily-bar `StockHistoricalDataClient.get_stock_bars` history and, during market hours, ends at most one session behind. Nothing on screen sources or timestamps a true current quote, and nothing distinguishes "historical bar" from "now." Fix stays entirely at the `src/api` layer, reusing the **already-existing, already-safety-reviewed** `AlpacaBroker.get_intraday_snapshots()` (the same read-only bulk snapshot call the accepted, already-enabled intraday scanner uses) via a new `broker_reads.read_live_quotes()` + `GET /quotes` — not a new broker capability.
2. **Cockpit run history** — confirmed: `App.tsx`'s single poll always fetches `api.runs(1)` and overwrites `funnel` with the latest run every 20s; there is no pinned/historical view in the primary Cockpit surface (only the separate `RunDetailModal`). Fix is frontend-only: explicit live/pinned run selection state, a run timeline, "Return to Live," chart/candidate/decision context always following one resolved run.
3. **Execution-skip frontend contract gap** — confirmed real: backend `CandidateFunnelItem` (`src/api/schemas.py`) and `get_run_funnel` (`src/api/routes_evidence.py`) already populate and test (`tests/test_api_funnel.py::test_funnel_surfaces_execution_skip_reason`) `execution_skip_reason`/`execution_skip_detail`; `frontend/src/api/client.ts`'s `CandidateFunnelItem` interface omits both fields, so the UI falls back to a generic "proposed but not executed" even when a specific persisted reason exists.
4. **"Why wasn't it purchased"** — `CandidateDetailModal.tsx` has full forensic evidence but no prominent final-outcome/stopped-at/reason summary; depends on fix 3.
5. **Journal decision ledger legibility** — `JournalPanel.tsx`'s per-candidate line already exists but omits risk outcome and skip reason; depends on fix 3.
6. **Journal date discoverability** — confirmed real: `db_reads.get_journal_dates()` unions only `insights.date` and `daily_pnl.date`; a day with real runs/candidates but no evening reflection and no equity snapshot is invisible in `/journal/dates` even though `/journal/{date}` would already render it correctly if selected directly.

Accepted implementation contract: fix all six read-side, inside `src/api` + `frontend/src`, with the isolation properties in `.claude/rules/mission-control-api.md` intact (no new broker-write surface, no AlpacaBroker construction outside `broker_reads.py`, no `src.pipeline`/`src.risk`/write-`Database` imports); no changes to `src/execution/broker.py`, `src/pipeline*.py`, `src/risk/*`, `src/agents/*`; add/adjust backend + frontend tests for each defect; browser-verify the affected Cockpit/Journal/modal workflows (desktop + tablet viewport) before handoff; run the full relevant backend + frontend test/build suites; commit/push only to `claude/mission-control-data-correctness`; do not merge, do not deploy. Close with `/qamc-checkpoint`.
