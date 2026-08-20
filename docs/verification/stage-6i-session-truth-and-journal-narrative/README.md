# Stage 6i — same-day session truth + Journal Day narrative convergence

Product-convergence pass on top of Stage 6h, working from `docs/OUTCOME.md`
and `docs/WORK.md`'s "Known dashboard correctness debt to carry forward"
list against the actual running cockpit and real (read-only) production
data, per the Mission Control product-convergence work item authorized in
`docs/STATE.md`.

**Captured against:** the working tree at time of capture, parent commit
`56823d4062254fc44fc80e3c40a2e07cd538034a`, committed together as part of
this same change, per this project's established evidence convention
(Stage 6b/6f/6g/6h).

**Verification date:** 2026-08-20, ~23:15–23:40 UTC.

## What changed

Inspecting the deployed cockpit against real, live, read-only production
data (the `qamc` Mission Control API on loopback `127.0.0.1:8800` — the
same account-boundary-respecting read path `ops/preview/branch_preview.py`
already uses, reached here directly via `npm run dev`'s existing API proxy;
no credential, database file, or `qamc`-owned process was touched) surfaced
several concrete, reproducible truth/UX defects, all still open despite
Stage 6f–6h's redesign:

- **Cockpit primary view could go blank while real same-day decisions
  existed.** QAMC runs several session types a day; `midday`/`close` are
  `run_position_review` (see `src/pipeline.py`), structurally distinct from
  a full opportunity scan and near-always reporting zero candidates. The
  cockpit always followed the literal-latest run, so — reproduced live —
  at 23:15 UTC the cockpit showed "NO CANDIDATES CONSIDERED" from the
  19:30 `close` session while two real same-day `run` sessions at 13:30
  and 14:00 (96 and 95 candidates, 5 reaching the Portfolio Manager, one
  AI Risk Manager rejection) were completely hidden. This is exactly the
  "latest empty runs can erase more meaningful session context" item in
  `docs/WORK.md`.
- **Journal Day was a chronological dump, not the accepted narrative.**
  Each run card flat-mapped every screened candidate (60–95+ symbols on a
  real trading day), repeating "PM: no proposal" per symbol — the exact
  pattern `docs/OUTCOME.md`'s Journal Day section says should not be the
  primary experience. A real single day's page rendered at 5995px tall.
- **A run whose only fill was SGOV cash-sweep housekeeping displayed
  identically to a real strategy execution** — `decision_state: "executed"`
  is true for both (the backend counts any trade row), and the UI reused
  the same green "EXECUTED" pill for both, reproduced live against
  `midday-ce23be6e`, whose only trade was `SWEEP_BUY`. Matches
  `docs/WORK.md`'s "SGOV cash-management trades and strategy trades need
  clearer separation."
- **"Rejected by specialist"** (`CandidateRail`'s bucket label) overstated
  what the data actually shows — a candidate that never reached a PM
  target can mean a negative specialist read, or just as often a PM pass
  on portfolio-balance/conviction grounds despite a fine specialist read;
  the API never distinguishes the two. Matches `docs/WORK.md`'s "Labels
  such as 'Rejected by specialist' must not overstate what actually
  occurred."
- **A real console error on every gauge/donut panel** — `echarts/core`'s
  tree-shaken registration (`lib/echarts.ts`) used `graphic` label
  elements (`ArcGauge`, `DonutMeter`) without registering
  `GraphicComponent`, throwing on every load.
- **A frozen-closure bug silently fighting the operator** — `chartSymbol`
  was read inside a `usePoll(fn, [])` callback, whose closure is captured
  once at mount; every 20s poll tick therefore reset the charted symbol
  back to the funnel's first candidate regardless of what the operator had
  actually clicked to chart.

## Fixes (frontend/read-side only — no backend, trading, or config change)

- `frontend/src/components/funnelShared.tsx`: added `bestPrimaryRunId()`
  (a data-driven rule — latest run with `candidates_considered > 0`, never
  a hardcoded session-type allowlist, so a future session type can't
  silently misclassify), `isSweepOnlyExecution()`, and moved
  `candidateStage`/`STAGE_META`/`STAGE_ORDER` here from `CandidateRail`
  (now shared with `JournalPanel`) with the "rejected" stage's label
  corrected from "Rejected by specialist" to "No PM target".
- `frontend/src/components/TodaySessionsStrip.tsx` (new): a compact,
  selectable same-day session strip so every real session stays visible —
  auto-follows the best primary run by default; an operator click pins a
  different one, with a "Follow latest" control to resume auto-follow.
- `frontend/src/App.tsx`: replaced the single literal-latest-run funnel
  fetch with a day-scoped fetch targeting **today's actual ET calendar
  date** directly (`lib/format.ts::todayEtDate()`) — deliberately NOT
  `journalDates(1)` (the "most recent complete day" the Journal view
  itself defaults to), which stays on yesterday all day until an
  end-of-day snapshot exists and would have silently reintroduced the same
  erasure bug under a "Today" label. Also fixes the frozen-closure
  chart-reset bug: `chartSymbol` now resets via a normal
  `useEffect([selectedRunId])`, not inside the poll closure.
- `frontend/src/components/DecisionStateBanner.tsx`,
  `frontend/src/components/JournalPanel.tsx`: sweep-only runs now show a
  distinct dim "CASH SWEEP ONLY" state instead of the strategic-execution
  green "EXECUTED" pill. Journal's per-run and day-level candidate lists
  now bucket by furthest stage reached (reusing `funnelShared`) and
  collapse the majority "no PM target" bucket behind a `<details>` expand,
  and the Watchlist/Candidates section moved earlier in reading order —
  Market Thesis → Watchlist/Candidates → Runs/Decisions → Trades → Daily
  Result, closer to `docs/OUTCOME.md`'s Journal Day structure.
- `frontend/src/lib/echarts.ts`: registered `GraphicComponent`.

## Screenshots

| # | File | Viewport | Scenario |
|---|---|---|---|
| 01 | `01-before-desktop-cockpit-empty-latest-run.png` | Desktop 1600×1000 | **Before.** Real production data, captured live: the cockpit follows the 19:30 `close` session and shows "NO CANDIDATES CONSIDERED" / an empty chart, even though two real same-day decision sessions exist. |
| 02 | `02-after-desktop-cockpit-best-primary-run.png` | Desktop 1600×1000 | **After**, same real data/moment. Today's Sessions strip shows all four of today's sessions; the cockpit now follows the 14:00 run (95 candidates) — "PROPOSED — NOT EXECUTED", AI Risk Manager rejection reason shown, chart auto-populated with AAPL, full Specialists→PM→Risk→Gate→Execution chain populated. |
| 03 | `03-after-desktop-cockpit-manual-session-override.png` | Desktop 1600×1000 | Operator clicked the 13:30 session chip — the cockpit switches to it ("NO TRADE — PM STAYED NEUTRAL"), auto-follow turns off, and a "Follow latest" control appears. |
| 04 | `04-after-desktop-journal-decluttered-narrative.png` | Desktop 1600×2278 (full page) | Same real day's Journal, full page. 2278px vs. the prior 5995px for an equivalent day — notable candidates shown by name, the ~90-symbol screened majority collapsed behind "N more screened, no PM target reached — expand", `CASH SWEEP ONLY` correctly distinguishing SGOV housekeeping from real proposals, Watchlist section promoted ahead of per-run decisions. |
| 05 | `05-after-ipad-cockpit-sessions-strip.png` | iPad 820×1100 | Today's Sessions strip and the corrected decision banner/candidates at iPad width — scrolls horizontally, no layout breakage. |
| 06 | `06-regression-candidate-detail-from-journal.png` | Desktop 1600×1000 | Regression check: clicking a candidate inside Journal's newly-collapsed "screened" list still opens the full `CandidateDetailModal` drill-down (symbol evidence, broader macro/news context) correctly. |
| 07 | `07-regression-positions-risk-dock-tab.png` | Desktop 1600×1000 | Regression check: the Stage 6h Liquidity/Positions donut+treemap dock tab, untouched by this pass, still renders correctly alongside the now-populated CandidateRail (`RISK`/`PM`/`—` bucket labels visible). |

All six pages were captured with zero browser console errors or page
errors (Playwright's own `console`/`pageerror` listeners), confirming the
`GraphicComponent` fix.

## Method

Same accepted pattern as Stage 6f/6g/6h's Method sections, adapted to use
**real** data instead of a seeded fixture: `npm run dev` (Vite), which
already proxies `/account`, `/positions`, `/runs`, `/journal/*`, etc. to
the real `qamc` production Mission Control API on `127.0.0.1:8800` — the
same account-boundary-respecting, read-only, loopback path
`ops/preview/branch_preview.py` documents and this repo's own
`docs/verification/stage-6-react/README.md` established. Screenshots
captured with a local headless Playwright script
(`chromium.launch()`, no browser extension), run directly on this
machine. Neither `ops/preview/branch_preview.py` nor `qamc` production
itself were started, stopped, restarted, or written to — every request
made during this verification pass was `GET`.

## Checks performed

- [x] Reproduced the "latest empty run erases real same-day context" bug
      live against real production data before fixing it, and confirmed
      the fix resolves the exact reproduced scenario (screenshots 01→02).
- [x] Manual session override and "Follow latest" control both verified
      interactively (screenshot 03).
- [x] Journal Day materially decluttered on a real multi-run day (2278px
      vs. 5995px for an equivalent day) without losing any underlying
      data — every screened symbol remains one click away via `<details>`.
- [x] `CASH SWEEP ONLY` verified against a real SGOV-only run
      (`midday-ce23be6e`) in both the cockpit banner and Journal.
- [x] "No PM target" label verified in place of "Rejected by specialist"
      in the live CandidateRail bucket chips.
- [x] Candidate drill-down from both the cockpit rail and the Journal's
      collapsed list still opens `CandidateDetailModal` correctly
      (screenshot 06) — the shared `funnelShared` bucketing refactor
      introduced no regression.
- [x] Positions & Risk dock tab (Stage 6h donuts/treemap), Missed
      Opportunities tab, and iPad layout all re-verified with zero visual
      or console regressions (screenshot 07 and manual pass).
- [x] Zero browser console errors / zero page errors across every
      captured scenario, confirming the `GraphicComponent` registration
      fix (previously two errors on every load).
- [x] `npm run build` (TypeScript strict + Vite) and `npm test` (Vitest —
      17 tests, including 9 new regression tests in
      `funnelShared.test.ts` pinning the exact real-data scenario found)
      both pass.
- [x] `git diff --stat -- src/ scripts/ config/ ops/` confirms zero
      backend/trading/config changes — only `frontend/src/` and the
      regenerated `src/api/static_cockpit/` build output.
- [x] Real `qamc` production and `ops/preview/branch_preview.py` untouched
      — this pass only issued `GET` requests against the already-running
      loopback API, the same read path already established as safe.
- [x] No secrets in screenshots, scripts, or this document — the
      throwaway Playwright scripts are not committed (scratchpad-only,
      consistent with this project's established convention).

## Known remaining gaps (not addressed this pass)

- The vision board's/`docs/OUTCOME.md`'s "Disagreements" and "Agent
  Analysis" as explicit, dedicated Journal Day sections were not built —
  the underlying per-specialist detail remains one click away via
  candidate drill-down (`CandidateDetailModal`'s decision-flow graph),
  consistent with "detailed event/log stream can remain available for
  forensics, but it is not the primary journal experience," but there is
  no day-level cross-specialist disagreement aggregation yet. Left out
  this pass rather than build a low-confidence aggregation; a real
  candidate for a future focused pass.
- `todayEtDate()`'s pre-first-session-of-the-day / non-trading-day empty
  state (`journal/{date}` 404) is implemented and type-checked but not
  independently screenshotted, since real production data had genuine
  same-day sessions at verification time and forcing the empty case would
  have meant faking a date rather than observing real state.
