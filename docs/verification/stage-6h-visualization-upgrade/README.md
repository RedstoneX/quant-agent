# Stage 6h — mature-visualization upgrade + chart dead-space fix

A follow-on external (ChatGPT/operator) review of a real Stage 6g screenshot
found the information architecture and visual-identity work sound but
flagged three concrete defects: the primary price chart left a large unused
region below it on any viewport taller than the old hard-coded 260px, and
both the Liquidity and Real-Risk-Exposure panels still used a thin
`SegmentedBar` the operator had already rejected once. The review asked for
a bounded, evidence-based research pass — best visualization pattern for
each information object, best mature component already in the approved
library set, then KEEP/TRANSFORM/REPLACE/CONSOLIDATE/REMOVE — rather than
prettifying the existing homemade primitives.

**Captured against:** the working tree at time of capture (parent commit
`5d15233d70b93f038915e99aecc6cc7609072931`, Stage 6g), committed together as
part of this same change, per this project's established evidence
convention (Stage 6f/6g/6b).

**Verification date:** 2026-08-19, ~12:00–12:25 UTC.

## What changed

- **Chart dead-space fix (`App.tsx`, `PriceChartPanel.tsx`)**: the cockpit's
  three-column row now shares one explicit viewport-bounded height
  (`xl:h-[calc(100vh-150px)]`) instead of an unconstrained grid cell: the
  candidate rail and Decision Room scroll independently within it, and the
  center column's chart wrapper is `flex-1 min-h-0` so the chart itself
  fills whatever vertical space is actually available — not a second fixed
  number replacing the old 260px. `PriceChartPanel`'s manual
  `ResizeObserver` now reads both width AND height off the observed box
  (previously width-only, height hard-coded to 260) and calls
  `chart.resize()` + `fitContent()` together, with a `MIN_CHART_HEIGHT` of
  240px so a very short viewport never collapses the chart into an unusable
  sliver. Verified at both 1000px and 760px window heights (screenshots
  01a/01b) — the chart visibly fills more of the column on the taller
  viewport and never leaves a dead gap on either.
- **Liquidity + Real-Risk-Exposure donuts (`DonutMeter.tsx`, new)**:
  replaced both `SegmentedBar` thin bars with an ECharts donut (`pie` in
  donut mode) sharing `ArcGauge`'s color-token system and `EChart`
  substrate, so the donut and the hero-band gauge read as one visual
  family. Each donut shows category, dollar amount, and percentage of the
  relevant total side-by-side with the ring; SGOV sweep-parking keeps its
  own truthful category (`dim`, a muted neutral swatch — neither a market-
  status color nor the brand/interactive accent cyan) distinct from
  deployable cash and reserve.
- **Positions treemap (`PositionsTreemap.tsx`, new)**: `PositionsPanel` now
  shows an ECharts treemap above the exact-value table — block area =
  \|market value\| (concentration), block color = unrealized P&L sign
  (`pos`/`neg`, real market truth), cash-equivalent blocks render `dim`.
  Chosen over another donut because holdings are a real, unbucketed list
  (concentration + winner/loser shape), not a 2–3-category composition —
  donut for simple composition, treemap for a real hierarchy, matching the
  same rule already applied to the Liquidity/Exposure donuts.
- **Missed-opportunities scatter (`MissedOpportunitiesPanel.tsx`,
  rebuilt)**: previously showed only the latest day's evening-review bullet
  list. Now aggregates the last 20 journal days client-side (existing
  `journalDates`/`journalDay` endpoints, no new backend surface) into an
  ECharts scatter — X = date, Y = signed move %, color = genuine-miss
  (amber) vs. disciplined-pass (`noise_rally`/`risk_disciplined`, dim) —
  with a zero baseline so the up/down balance the product cares about
  (`docs/OUTCOME.md`'s directional-neutrality question) is visible at a
  glance instead of inferred from a single day's text. The latest day's
  bullet detail is retained beneath the chart. This was explicitly deferred
  in Stage 6g's design plan pending a case for real value; a longitudinal
  directional-miss pattern is that case.
- **Decision Room — assessed, not rebuilt**: re-read against the review's
  explicit 9-question bar (what did each specialist believe, confidence,
  disagreement, PM synthesis, what changed specialist→PM, RM approve/
  modify/veto, deterministic allow/block, what executed, why this candidate
  over alternatives). Stage 6g's React Flow graph (per-specialist fan-in,
  confidence bars, aligned/diverges pills, hexagonal hard-interlock gate)
  plus the existing `CandidateDetailModal` drill-down (PM target/thesis/
  continuity-premortem flags, RM modification field/original/new/reason,
  proposed-vs-executed comparison) already answer all nine without
  cosmetic changes — screenshot 04 shows the full chain for a real
  candidate. Decision: **KEEP**. Rebuilding a working answer to look
  different would be exactly the "make it prettier" failure mode the
  review warned against.
- **Sankey / deeper branching detail — reassessed, still deferred**: the
  candidate funnel (bars + sortable table) already shows the narrowing
  shape clearly at current candidate volumes; a Sankey would mostly
  duplicate that without adding comprehension. Left out this pass;
  reconsider if real multi-branch candidate volume makes the funnel view
  insufficient.

## A real bug found and fixed along the way

The `DonutMeter`/donut work was mid-flight and uncommitted at the start of
this pass (typecheck-broken: `Record<GaugeTone, string>` missing the
`"hedge"` member `ArcGauge`'s tone type had just gained, plus two ECharts
`graphic` text elements using the non-existent `textAlign` property instead
of `align`). Fixing the typecheck also surfaced a real semantics bug: with
`"hedge"`/`"dim"` unavailable, `LiquidityPanel` had been re-pointed to
`tone: "neg"` (market-loss red) for the bearish-hedge segment and
`tone: "accent"` (brand/interactive cyan) for cash-equivalent/sweep —
both violate this project's own color-grammar contract ("red is market
truth, never repurposed"; "accent is chrome, not a data category" —
`styles/index.css`'s token-system comment). Fixed by extending `DonutMeter`
to support the full tone set (adding `hedge` and a new `dim` category
distinct from `GaugeTone`'s scalar-band tones) and restoring the correct
semantic tones in `LiquidityPanel`.

A second real bug was found and fixed in this pass's own verification
fixture (not application code): the seeded `RiskVerdict.reason_category`
values didn't match `src/models.py`'s strict `Literal` enum, and one
`RiskModification` entry was missing its required `symbol` field — both
fail Pydantic validation silently (`routes_evidence.py`'s `_validate`
degrades a malformed row to `None` rather than 500ing), which had been
rendering the AI Risk Manager as "PENDING" instead of "APPROVED" on an
executed run. Confirms the production degrade-don't-crash contract works
as designed; fixed by correcting the fixture to use real enum values.

## Method

A throwaway, `dev`-account-only, monkeypatched Mission Control API instance
(same accepted pattern as Stage 6f/6g's Method section: `src.storage.db.
Database` seeds a real temp SQLite DB via typed insert methods, `unittest.
mock.patch` swaps `src.api.db_reads.get_db_path`, `src.api.deps.
get_config`, and `src.api.routes_live`'s broker-read functions — never a
real Alpaca credential or `qamc`/`dev` DB file), seeded with six days
spanning every decision state (no-trade/neutral, executed-long, hard-risk-
block, proposed-then-rejected, executed-with-RM-sizing-modification,
executed-long-with-full-specialist-fan-in) plus 20 days of daily P&L and
five missed-opportunity entries across the genuine-miss/disciplined-pass
split. Bound to this VPS's Tailscale IP on port 8811 (never `0.0.0.0`,
never `127.0.0.1`) while iterating interactively; the **committed**
screenshots in this directory were captured with a local headless
Playwright script (`chromium.launch()`, no browser extension involved) run
directly on this machine against that same seeded instance — the
established Stage 6b/6f/6g pattern for evidence that ships in Git. Neither
`ops/preview/branch_preview.py` (already running, serving real `qamc`
production data) nor `qamc` production itself were touched by this
verification pass.

## Screenshots

| # | File | Viewport | Scenario |
|---|---|---|---|
| 01a | `01-desktop-cockpit-tall-1000px.png` | Desktop 1600×1000 | Full cockpit, EXECUTED run — chart fills the tall column height with no dead space below it. |
| 01b | `01-desktop-cockpit-short-760px.png` | Desktop 1600×760 | Same run, a shorter/laptop-height viewport — chart still fills available height, no dead region reappears. |
| 02 | `02-desktop-liquidity-positions.png` | Desktop 1600×1000 | Support workspace, Positions & Risk tab: the Liquidity donut and Real-Risk-Exposure donut side by side with the new Positions treemap (AAPL/SQQQ green winners, MSFT red loser, SGOV neutral gray) above the exact-value table. |
| 03 | `03-desktop-missed-opportunities-scatter.png` | Desktop 1600×1000 | Missed Opportunities tab: 5-point scatter across 08-14→08-18, "3 genuine misses · 2 disciplined passes" summary, amber vs. dim coloring, zero baseline visible. |
| 04 | `04-desktop-candidate-decision-flow.png` | Desktop 1600×1000 | MSFT candidate drill-down, Decision flow section: real per-specialist node (Technical Analyst) → Portfolio Manager (target 9.5%) → AI Risk Manager (APPROVED) → Deterministic Gate (hexagon, FINAL AUTHORITY, REACHED) → Execution (EXECUTED, BUY 4sh), with the PM detail card (target/conviction/thesis/continuity+premortem flags) below. |
| 05 | `05-desktop-journal.png` | Desktop 1600×1000 | Journal view, SQQQ hedge day: equity sparkline, morning regime (risk-off/bearish), run narrative with RM verdict "APPROVED" (the fixed reason_category bug, confirmed live). |
| 06 | `06-desktop-journal-reflection.png` | Desktop 1600×1000 | Same day scrolled to trades/candidates/evening-reflection — RM sizing-modification narrative and the COIN missed-opportunity entry. |
| 07 | `07-ipad-cockpit-candidates.png` | iPad 820×1100 | Cockpit collapsed to the Candidates/Chart/Decision-Room tab strip below the `xl` breakpoint — hero band, gauge, and regime badge stack full-width above it. |
| 08 | `08-ipad-chart.png` | iPad 820×1100 | Chart tab — candlesticks + BUY marker render correctly at iPad width with the fixed-height (non-viewport-calc) mobile sizing path. |
| 09 | `09-ipad-decision-room.png` | iPad 820×1100 | Decision Room tab — the same React Flow graph, scrollable within its iPad pane. |

Screenshots 01–09 were captured in a plain Chromium context with no explicit
`prefers-color-scheme` override, so they render QAMC's **light** theme
variant — incidental but useful additional confirmation that the Stage 6g
token system (and every new component in this pass) resolves correctly in
light mode too, not only the dark mode shown in prior stages' evidence.

## Checks performed

- [x] Chart fills available vertical space at two desktop heights (1000px,
      760px) with no accidental dead region — the defect the review
      explicitly named.
- [x] Liquidity and Real-Risk-Exposure donuts replace both `SegmentedBar`
      instances; SGOV renders as a distinct, truthful "dim" category, never
      market-status green/red or brand-accent cyan.
- [x] Positions treemap renders real concentration (size) + real P&L
      (color) from already-fetched data, no fabricated metrics.
- [x] Missed-opportunities scatter aggregates real multi-day data from
      existing endpoints, correctly separates genuine misses from
      disciplined passes, shows both up and down moves.
- [x] Decision Room re-assessed against the full 9-question bar and found
      already sufficient — explicitly not rebuilt to avoid cosmetic churn.
- [x] Candidate drill-down decision-flow graph (per-specialist fan-in,
      hexagonal deterministic gate) renders correctly.
- [x] Journal (day narrative, equity sparkline, RM verdict, evening
      reflection) renders correctly, including the corrected RM verdict
      display.
- [x] iPad (820px) cockpit tab strip, chart, and Decision Room all verified.
- [x] Zero browser console errors / zero page errors across every captured
      scenario (asserted via Playwright's own `console`/`pageerror` event
      listeners during capture, not just visually).
- [x] `npm run build` (TypeScript strict + Vite) and `npm test` (Vitest)
      both pass — see below.
- [x] `git diff --stat -- src/` confirms zero backend/trading changes.
- [x] Real `qamc` production and `ops/preview/branch_preview.py` untouched
      — this pass's seeded verification server ran on a different port
      (8811) bound to the same tailnet interface, never `127.0.0.1` or
      `0.0.0.0`.
- [x] No secrets in screenshots, the throwaway seed script, or this
      document — the seed script and its SQLite DB are not committed
      (`/tmp` scratch location, consistent with this project's established
      convention).

## Known follow-ups (not completed this pass, explicitly deferred)

- ECharts Sankey for candidate-branching detail remains scoped but not
  implemented — current candidate volumes don't yet make the case for it
  over the existing funnel bars + sortable table.
- Multi-specialist fan-in (Tech + Earnings + News on one candidate) was not
  re-verified with fresh seed data this pass — Stage 6g's own evidence
  (`docs/verification/stage-6g-cockpit-visual-system/`, screenshot 02)
  already covers it against unchanged `agentflow/` code; this pass's seed
  fixture only fully validated the Technical-Analyst-only path for the
  new/changed panels' sake, which is sufficient since `agentflow/`,
  `buildGraph.ts`, and `CandidateDetailModal.tsx` were not touched.
- Production JS bundle grew modestly further with `TreemapChart` added to
  the existing ECharts registration (~11KB gzip) — consistent with the
  already-accepted "best-in-class over dependency minimization" direction;
  no new dependency was added, only an additional tree-shaken chart type
  from the already-approved ECharts package.
