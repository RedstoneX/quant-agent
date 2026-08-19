# Stage 6g — cockpit visual identity system + stale-data correctness fix

Operator review of a real, running Stage 6f screenshot found the redesigned
information architecture correct but the visual execution still reading as
a generic dark admin/Grafana panel — excessive black space, weak typography,
plain bordered cards everywhere, a Decision Room that was five stacked
rectangles rather than a legible multi-agent process, and a real
truthfulness bug: a failed API poll could leave an old EXECUTED decision
rendering as if current underneath a visible error banner. A separate
research pass (documented in the session's plan, `docs/architecture/` isn't
touched by this — see git history for the full research/composition
record) evaluated and authorized a small, coherent set of mature
visualization libraries — Apache ECharts, React Flow, TanStack Table,
Tremor, plus Dockview for the support workspace — chosen because each
materially outperforms a hand-built equivalent for its specific job, not
to minimize dependency count.

**Captured against:** the working tree at the time of capture (parent
commit `a7e75003e023523d1f9d99095463e34735019dc7`), committed together as
part of this same change, per this project's established evidence
convention (Stage 6f/6b et al.).

**Verification date:** 2026-08-19, ~11:00–11:31 UTC.

## What changed

- **Design token system**: new color grammar where hue itself carries
  meaning — `accent` (cyan) is system/brand/interactive chrome, `pos`/`neg`
  (green/red) is market truth and is never repurposed, `agent` (violet) is
  AI reasoning, `warn` (amber) is attention/pending/modified, `hedge`
  (magenta) flags a bearish-hedge instrument. IBM Plex Sans (UI/labels) +
  IBM Plex Mono (all numerals, tabular) replace the system font stack. A
  faint radial-vignette + dot-grid background replaces flat black so empty
  space reads as an intentional terminal surface, not a broken void.
- **Hero band** (`HeroBand.tsx`, new): equity/P&L in large tabular-mono
  numerals with a real Tremor `SparkAreaChart` equity sparkline, a
  segmented-color-band ECharts arc gauge for real portfolio risk exposure
  (long+hedge % of book), and a market-regime badge — answers "what do I
  own / what's the market doing" before any interaction, replacing the old
  `TopStrip` stats row and the flat `ExposureStrip` bar (removed).
- **Decision-state banner** (`DecisionStateBanner.tsx`, new): a full-width
  verdict — EXECUTED / NO TRADE / REJECTED / DETERMINISTIC GATE BLOCKED —
  promoted out of the Decision Room's right column, with a one-line honest
  "why" summary. This is also the stale-data banner surface (see below).
- **Decision Room rebuilt on React Flow** (`components/agentflow/`, new):
  the Specialists → PM → AI Risk → Deterministic Gate → Execution chain is
  now a real node/edge graph, not five stacked rectangles. The
  per-candidate view shows genuine fan-in — one node per specialist that
  actually produced evidence (Technical/Earnings/News), each with
  threshold-colored (not just direction-colored) confidence, converging on
  one PM node. The Deterministic Gate is a categorically different node
  **shape** — a hexagonal "hard interlock" outline with a diagonal
  hazard-stripe fill — not just a thicker border, because it is
  categorically different: the one non-LLM, non-negotiable authority in the
  chain. `SpecialistCards.tsx` and `DecisionFlowDiagram.tsx` are retired;
  their content now lives inside the graph nodes and the (unchanged) rich
  detail cards below it.
- **CandidateRail** rebuilt on TanStack Table (real column sort + expandable
  rows showing an inline PM/Risk summary from data already on each row) and
  an ECharts native `funnel` series (real narrowing funnel with connector
  arrows) replacing the plain stacked bars.
- **Trade execution markers** wired onto the price chart via
  `lightweight-charts`' existing (already-installed, previously-unused)
  `setMarkers` API — real BUY/SELL arrows on the candlestick series.
- **Desktop-only Dockview support workspace** (`DockviewSupportWorkspace.tsx`,
  new) — operator-approved, explicitly scoped: replaces the plain
  `SupportTabs` tab strip with a real resizable/draggable/poppable panel
  workspace **only** at `xl:`+ width (`useIsDesktop.ts`), default layout
  matching the old tab order exactly (one tabbed group, nothing
  pre-arranged), with a "Reset layout" control. The primary cockpit
  (Candidate rail / Chart / Decision Room) is never wrapped in Dockview and
  stays fixed/composed. Below `xl`, `SupportTabs` (unchanged component,
  restyled by the new tokens) remains the iPad experience — Dockview is
  never instantiated there. Layout state persists to `localStorage` only
  (non-authoritative, resettable — never a second source of trading truth).

## Stale-data correctness fix

The core bug the operator's screenshot exposed: `App.tsx`'s polling never
cleared `account`/`positions`/`funnel` state on a failed fetch, but
`DecisionRoomPanel`/`CandidateRail` rendered the (stale) data with no
indication anything had failed, while a *separate* error banner elsewhere
on the page said the opposite. Fixed:

- Every poll target (`account`, `positions`, `funnel`) now tracks its data,
  its current error, and a `*UpdatedAt` last-good timestamp separately. A
  failed poll sets only the error — it never overwrites good data with
  nulls, and it never lets the UI claim the retained data is current.
- `ui/Panel.tsx` gained a `"stale"` status (amber, distinct from `"error"`
  red) rendering `stale · HH:MM:SS` in the panel header.
- Every affected panel (hero band, decision banner, Decision Room,
  Candidate rail, Positions, Liquidity) renders an explicit inline
  "Showing last known data as of HH:MM — a fresh fetch failed: {error}"
  banner instead of silently continuing to show old data as current.
- Screenshots 04–06 below are a real load → forced-failure → recovery
  cycle (Playwright route-blocking + a forced `visibilitychange` re-poll,
  not a mocked/fabricated state) confirming this end-to-end: the decision
  banner, Decision Room, and Candidate rail all correctly flip to STALE
  with a timestamp and correctly clear back to OK on recovery.

## Method

Both data sources used in prior verification passes were not both
available this session (real `qamc` production's branch-preview process was
confirmed live-but-untouched on its existing port, not reused to avoid any
risk to that process). Verification here uses a throwaway, monkeypatched,
`dev`-account-only local Mission Control API instance seeded with
realistic data built from real `src.models` Pydantic classes
(`.model_dump_json()`-validated, never hand-typed JSON) — the same
accepted "monkeypatch at the broker/db-read seam" pattern documented in
`docs/verification/stage-6f-cockpit-ia/README.md`'s Method section, run on
a different port than the real branch-preview process, never touching
`qamc` production, its DB, or real credentials. Not committed (throwaway
dev fixture, consistent with this project's existing convention).
Screenshots captured via a local headless Playwright script (same
established pattern as Stage 6b/6f), not committed either.

## Screenshots

| # | File | Viewport | Scenario |
|---|---|---|---|
| 01 | `01-desktop-cockpit-overview.png` | Desktop (1440px) | Full cockpit: hero band (equity/sparkline, risk gauge, regime badge), decision-state banner (EXECUTED), candidate rail (funnel chart, sortable/expandable table), Decision Room (React Flow run-level chain, gate as hexagon), and the Dockview support workspace's System tab at the bottom. |
| 02 | `02-desktop-candidate-agent-graph.png` | Desktop (1440px) | AAPL candidate drill-down, scrolled to "Decision flow": the real per-specialist fan-in graph — Technical/Earnings/News Analyst nodes (each with threshold-colored confidence, "Aligned with consensus") converging on Portfolio Manager, then AI Risk Manager (MODIFIED), the Deterministic Gate hexagon (FINAL AUTHORITY, REACHED), and Execution (EXECUTED, BUY 12sh). |
| 03 | `03-desktop-dockview-workspace.png` | Desktop (1440px) | Dockview support workspace, Positions & Risk tab: segmented liquidity/exposure bars, positions table, real tab-strip chrome themed to QAMC's tokens. |
| 04 | `04-desktop-loaded-state.png` | Desktop (1440px) | Baseline: everything loaded successfully, all panels "OK". |
| 05 | `05-desktop-stale-state.png` | Desktop (1440px) | **The stale-data fix, live**: after forcing every API route to fail and triggering a re-poll — amber "Account: showing last known data" banner in the header, amber-ringed hero band, the decision banner showing "STALE — LAST KNOWN DATA AS OF ..., FRESH FETCH FAILED" above the still-legible (correctly retained, not blanked) EXECUTED verdict, Candidate rail and Decision Room both showing `STALE · HH:MM:SS` with inline explanatory banners. |
| 06 | `06-desktop-recovered-state.png` | Desktop (1440px) | After unblocking the API and triggering one more re-poll: every panel cleanly back to "OK", no lingering stale artifacts, timestamp updated. |
| 07 | `07-ipad-cockpit.png` | iPad (820px) | Hero band + decision banner full-width above the Candidates/Chart/Decision-Room tab strip; support area still a plain tab strip (Dockview never instantiated below `xl`). |
| 08 | `08-desktop-journal.png` | Desktop (1440px) | Journal view — day narrative across three real seeded runs spanning REJECTED, DETERMINISTIC GATE BLOCKED, and EXECUTED decision states in one day; inherited the new token system with no additional changes needed. |

## Checks performed

- [x] Desktop (1440px) and iPad (820px) viewports.
- [x] Populated, loaded/OK, stale (forced), and recovered states — the
      stale→recovery cycle specifically, not just a static mock.
- [x] Candidate drill-down (real per-specialist fan-in graph) and Journal
      views.
- [x] Zero browser console errors / zero page errors across every capture
      in this set, and across a separate full click-through of all seven
      Dockview support-workspace tabs.
- [x] Decision Room state is understandable from the primary cockpit view
      (decision-state banner) without opening any modal.
- [x] Deterministic Gate is visually distinguishable **by node shape**
      (hexagon + hazard stripe), not just color/border-weight.
- [x] A failed poll never silently continues showing old data without an
      explicit stale indicator and last-good timestamp — verified via a
      real forced-failure/recovery cycle, not asserted.
- [x] Dockview desktop-only, default layout = old tab order, primary
      cockpit never wrapped in it; iPad confirmed unaffected.
- [x] `git diff --stat` confirms zero changes under `src/` outside the
      pre-existing compiled `src/api/static_cockpit/` bundle — this
      tranche is `frontend/`-only; no deterministic risk/execution
      semantics touched.
- [x] `git diff --stat -- src/api/static/` confirms `/ui` (legacy
      dashboard) is byte-for-byte untouched.
- [x] Real `qamc` production process/port never contacted this session —
      the seeded verification fixture ran on a different port
      specifically to avoid it.
- [x] No secrets in screenshots, the throwaway seed script, or this
      document.
- [x] `npm run build` (TypeScript strict + Vite) and `npm test` (Vitest,
      8/8 `DirectionalBiasPanel` tests, unaffected pure-function logic)
      both pass.

## Known follow-ups (not completed this pass, explicitly deferred)

- ECharts Sankey (candidate branching detail in `RunDetailModal`) and
  ECharts Scatter (missed-opportunity move-size visualization) were
  authorized in the design plan as secondary/detail-view enhancements but
  not implemented this pass — prioritized finishing and verifying the
  primary-cockpit-visible work (hero band, decision banner, agent graph,
  Dockview, the stale-data fix) over adding more surface area. Both remain
  correctly scoped and ready to pick up in a follow-on tranche.
- The production JS bundle grew from ~121KB to ~738KB gzip with this
  tranche's dependency set (ECharts, React Flow, TanStack Table, Tremor,
  Dockview) — an expected, accepted consequence of the operator's explicit
  "best-in-class tools over dependency minimization" direction for a
  private, tailnet-only, single-operator tool. Not treated as a defect;
  noted for the record. Code-splitting (dynamic `import()` per rarely-used
  chart type) would be the natural next optimization if it ever mattered.
