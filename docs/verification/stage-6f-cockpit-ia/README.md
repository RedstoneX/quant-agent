# Stage 6f — cockpit information architecture + visual redesign

Operator review of the Stage 6e cockpit found it still read as a long
telemetry/admin page — a flat two-column stack of full-width panels,
including an unbounded ~80-ticker flat pill dump (the old
`WatchlistPanel`) and a nine-panel vertical scroll below the fold. This
tranche is a **frontend-only** restructure: real cockpit regions (top
status/exposure strip, left candidate rail, center chart, right Decision
Room, tabbed lower support area), a real visual toolbox (funnel bars,
segmented exposure bars, conviction meters, a vertically-legible
Specialists → PM → AI Risk → Deterministic Gate → Execution chain with
the gate visually marked as final authority) in place of plain text/
tables, and a true tabbed layout below the `xl` (1280px) breakpoint
(covers every iPad size) instead of a squeezed 3-column desktop layout.
No backend/API changes, no deterministic trading/risk/execution changes,
`/ui` untouched.

**Captured against:** the working tree at the time of capture, committed
together as part of this same change (parent commit `0fab731`).

**Verification date:** 2026-08-19, ~09:05–09:35 UTC.

## What changed

- `App.tsx` rewritten into a real cockpit grid: `CandidateRail` (left) /
  chart + `SelectedSymbolContext` (center) / `DecisionRoomPanel` (right)
  at `xl:` width, collapsing to an explicit `CANDIDATES / CHART /
  DECISION ROOM` tab strip below it — not a squeezed 3-column layout.
  `SupportTabs` replaces nine stacked full-width panels (Positions &
  Risk, Orders & Trades, Runs, Directional Bias, Missed Opportunities,
  Search, System) with a single tab strip, one panel group visible at a
  time. Cockpit vs Journal is now its own top-level view switch.
- `CandidateRail` (replaces `WatchlistPanel`): the candidate universe is
  bucketed by the furthest funnel stage each candidate actually reached
  (rejected by specialist / reached PM / proposed / modified-or-blocked
  by risk / executed) — clickable stage-filter chips, a funnel-bars
  visualization (Considered → Reached PM → Proposed → Executed), a
  symbol filter box, and a height-bounded scrollable list — instead of
  every candidate printed as an equal-weight pill.
- `DecisionRoomPanel` (replaces the old full-width `DecisionFunnelPanel`
  in the main view; shared derivation logic moved to `funnelShared.tsx`):
  a narrow-column condensation of the same `RunFunnelResponse` data —
  state pill, a **vertical** Specialists → PM → AI Risk → Deterministic
  Gate → Execution chain (new `layout="vertical"` mode on
  `DecisionFlowDiagram`, needed because the existing horizontal layout's
  5 nodes don't fit a ~340px rail and left the tail — gate, execution —
  hidden behind an internal scrollbar), clamped market-regime/PM/risk
  excerpt cards, a link into the full `RunDetailModal`.
- `DecisionFlowDiagram`: the deterministic gate node is now visually
  distinguished (thicker border + an explicit "Final authority" marker)
  from the model-driven stages before it, per the project's actual
  decision-chain contract.
- `SpecialistCards`: added a conviction meter bar (`ui/Meter.tsx`) under
  each specialist card, and long reasoning text now defaults to a
  clamped preview with an explicit "Show full reasoning" toggle.
- `LiquidityPanel`: the cash breakdown and real risk exposure are now
  segmented bars (`ui/Meter.tsx`'s `SegmentedBar`) — deployable cash /
  reserve held back / SGOV sweep parked, and separately long / bearish-
  hedge / cash-equivalent — instead of a plain figure grid.
- `ExposureStrip` (new): an always-visible header gauge — real portfolio
  exposure (long / bearish-hedge / cash & equivalents, from
  `account.portfolio_value` + `positions`) — so directional/cash posture
  never requires opening a tab.
- `JournalPanel`: added a real-data equity/P&L sparkline (from
  `account.history`, already fetched, no new endpoint) at the top of the
  day view.
- `PriceChartPanel`: fixed two real bugs found during this pass (see
  "Bugs found and fixed" below) and status now reports `degraded` rather
  than `ok` when a symbol is selected but zero bars render.
- `ui/Panel.tsx`: `accent` panels get a subtle depth/glow treatment
  instead of only a border-color change.

## Bugs found and fixed during this pass

1. **Grid track overflow pushing the Decision Room off-screen.** The
   cockpit's `xl:grid-cols-[300px_1fr_360px]` center column had no
   `min-width: 0`, so CSS Grid's default `minmax(auto, 1fr)` let the
   price-chart's natural content width force the whole track (and the
   page) wider than the viewport, pushing the third column past the
   right edge entirely (confirmed via `document.body.scrollWidth` >
   `window.innerWidth` and a DOM scan for elements wider than the
   viewport). Fixed with `min-w-0` on the center column.
2. **Price chart rendered compressed into a sliver after its pane
   became visible.** `PriceChartPanel` used `lightweight-charts`'
   `autoSize: true`, which correctly resizes the canvas when a
   `display:none` pane (the mobile "Chart" tab; also reachable by
   resizing across the `xl` breakpoint) becomes visible again, but does
   **not** itself re-fit the visible time range afterward — the chart
   stayed zoomed to whatever bar-spacing was last fit while narrow/
   hidden, rendering all 90 days of AAPL bars compressed into a ~140px
   sliver at one edge with the rest of the 750px panel blank. Fixed by
   replacing `autoSize` with an explicit `ResizeObserver` that calls
   `chart.resize()` then `timeScale().fitContent()` together, in that
   order, on every observed size change — reproduced and confirmed fixed
   on a fresh page load, not just after resizing from desktop width.

Both were caught and fixed during this same verification pass, not
discovered afterward.

## Method

Two data sources, matching this project's established verification
precedent (`docs/verification/stage-6-react/README.md`):

1. **Real `qamc` production data**, via the existing tailnet-only
   `ops/preview/branch_preview.py` (already running, unmodified by this
   tranche) — the honest, currently-quiet state (latest run considered
   zero candidates; only SGOV sweep-parked cash held). Proves the empty/
   degraded states render correctly against genuine account data.
2. **A throwaway seeded scenario** — a temp SQLite DB built through
   `src.storage.db.Database` (one run spanning every `CandidateRail`
   bucket: an executed AAPL BUY, an NVDA proposal trimmed by the AI Risk
   Manager for concentration, a clean MSFT proposal, a bearish-hedge
   SQQQ proposal, an AMD PM-target-only candidate, and 10 candidates
   that never reached PM), with `src.api.routes_live`'s broker-read
   functions monkeypatched to realistic account/position/order values
   (this dev box has no live Alpaca market-data credentials — the
   pattern documented as "not fabrication" because it substitutes for a
   real broker call at a defined seam, never invents evidence inside the
   product's own derivation logic) and synthetic-but-realistic daily
   OHLCV bars for the price chart. Run via the real FastAPI app
   (`uvicorn.run`), never touching `qamc` production. Proves the
   populated/graphical states (funnel bars, conviction meters, segmented
   exposure bars, the full 5-stage Decision Room chain, candidate
   drill-down) actually render as designed. This script was not
   committed (throwaway verification tooling, consistent with this
   project's existing convention of not committing prior verification
   passes' seed scripts either).

Screenshots below were captured with a local headless Playwright script
(`playwright` is an existing project dependency) against both running
servers. Console errors were tracked programmatically across every page
in the same script: **zero console errors, zero page errors, across all
12 captures.** The interactive session that iterated on this design (the
screenshots referenced during development, not committed here) ran in a
real connected Chrome browser in dark mode; the committed set below is
Playwright's default light color scheme — both are exercised, only one
set is committed as the representative evidence per the "curated, not
exhaustive" rule.

## Screenshots

| # | File | Viewport | Scenario |
|---|---|---|---|
| 01 | `01-desktop-cockpit-decision-room.png` | Desktop (1440px) | Seeded data. Full cockpit: candidate rail (funnel bars, bearish-hedge badge, stage chips, filterable list), center chart (AAPL, real candlesticks) with selected-symbol context header, Decision Room showing the full **EXECUTED** chain with the AI Risk Manager **MODIFIED** and the Deterministic Gate visually marked **FINAL AUTHORITY**. |
| 02 | `02-desktop-support-tabs-positions-risk.png` | Desktop (1440px) | Seeded data, scrolled to the lower tabbed support area — **Positions & Risk** tab: segmented liquidity bar (deployable/reserve/SGOV) and real-risk-exposure bar (long/hedge/cash-equivalent) replacing the old plain figure grid, positions table with direction pills. |
| 03 | `03-desktop-support-tabs-orders-trades.png` | Desktop (1440px) | Seeded data — **Orders & Trades** tab, confirming tab switching only renders one group at a time. |
| 04 | `04-desktop-candidate-drilldown-conviction-meter.png` | Desktop (1440px) | Seeded data — NVDA candidate drill-down modal: Technical Analyst specialist card with the new conviction meter bar (green, HIGH). |
| 05 | `05-desktop-journal-sparkline.png` | Desktop (1440px) | Seeded data — Journal view (now its own top-level tab) with the new equity/P&L sparkline, day narrative below (morning regime, per-run decisions, candidate chips). |
| 06 | `06-ipad-candidates-pane.png` | iPad (820px) | Seeded data — the Candidates pane as an explicit tab (not a squeezed column). |
| 07 | `07-ipad-chart-pane.png` | iPad (820px) | Seeded data — the Chart pane tab, confirming the ResizeObserver fix: full-width, correctly-fit candlesticks, not the compressed-sliver bug this pass found and fixed. |
| 08 | `08-ipad-decision-room-pane.png` | iPad (820px) | Seeded data — the Decision Room pane tab: full 5-stage vertical chain fits without any scrolling. |
| 09 | `09-ipad-journal.png` | iPad (820px) | Seeded data — Journal at iPad width. |
| 10 | `10-desktop-real-data-empty-state.png` | Desktop (1440px) | **Real `qamc` production data.** Latest run genuinely considered zero candidates — Decision Room honestly shows **NOT REACHED** at every stage, `ExposureStrip` correctly shows only Cash & equivalents (no long/hedge positions currently held). Proves the redesign doesn't fabricate activity when there is none. |
| 11 | `11-ipad-real-data.png` | iPad (820px) | Real `qamc` production data at iPad width — same honest empty state, tabbed layout. |
| 12 | `12-desktop-legacy-ui-fallback.png` | Desktop (1440px) | `/ui/` (the Stage 3-5 dashboard) confirmed still served, unmodified — the required rollback path remains intact. |

## Checks performed

- [x] Desktop (1440px) and iPad (820px) viewports.
- [x] Populated (seeded, spans every candidate-funnel bucket) and honest
      empty/degraded (real production, zero candidates today) states.
- [x] New drill-down surfaces: candidate modal conviction meter, Decision
      Room's "Open full run detail" link.
- [x] Zero browser console errors / zero page errors across all 12
      captures (scripted, not just visually inspected).
- [x] Cockpit main screen shows the latest substantive decision
      (Specialists → PM → AI Risk → Deterministic Gate → Execution, gate
      marked as final authority) without opening any scroll section.
- [x] 80+ real production candidates would bucket into grouped summaries
      with a height-bounded, filterable, drill-down list — confirmed
      structurally against the seeded run's 15 candidates (real
      production's current latest run has zero, so the large-N case is
      exercised via the funnel math, same derivation used for real runs
      in Stage 6c/6e's directional-bias aggregation).
- [x] iPad layout is a true tab strip (Candidates / Chart / Decision
      Room, and Cockpit / Journal) below the `xl` breakpoint, not a
      squeezed desktop layout.
- [x] SGOV visually unmistakable as cash-equivalent sweep parking in both
      the segmented liquidity bar and the exposure gauge/strip.
- [x] `/ui` legacy fallback confirmed still served, unmodified.
- [x] No fake trades/charts/decisions: seeded scenario is explicitly a
      throwaway dev-only fixture never touching `qamc` production or its
      database/broker credentials; real-production screenshots (10, 11)
      show genuinely current, unmodified account state.
- [x] No deterministic risk/execution semantics touched — this tranche
      is `frontend/` only (`git diff --stat` confirms zero changes under
      `src/` outside the pre-existing compiled `src/api/static_cockpit/`
      bundle).
- [x] Alpaca remains Paper (`"paper": true` throughout, confirmed via the
      real production `/health`/`/account` responses shown in screenshots
      10–11).
- [x] No secrets in screenshots, seed script, or this document.
- [x] `npm run build` (TypeScript strict + Vite) and `npm run test`
      (Vitest, 8/8 `DirectionalBiasPanel` tests, unaffected pure-function
      logic) both pass.

## Remaining known limitation

The committed screenshots are all light color-scheme (Playwright's
default); dark mode was exercised interactively during development but
that session's screenshots were not saved to disk (only the committed
set above is the curated acceptance evidence, per this project's
existing "don't commit transient verification screenshots" convention).
Dark-mode CSS itself is unchanged by this tranche — Stage 6's original
dark-mode pass (`docs/verification/stage-6-react/06-desktop-dark-mode-
dashboard.png`) remains the dark-mode evidence of record for the
underlying theme tokens.
