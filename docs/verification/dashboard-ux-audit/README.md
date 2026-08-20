# Dashboard UX audit — full screenshot set

Every page/view of the deployed QAMC dashboard, captured against **live
production data**, for a trader-perspective UX/utility review.

No UI or application code was changed to produce these. No mock, seeded
or fixture data was used: the browser was pointed at the real production
Mission Control instance over the tailnet and every request was a plain
read-only GET.

## Capture conditions

- **Target:** live production Mission Control — `https://ovh-vps.wallaby-bowfin.ts.net/cockpit/`
  (React cockpit) and `/ui/` (legacy surface).
- **Served cockpit bundle:** `index-DesT650h.js`.
- **Date/time:** 2026-08-20, 08:02–08:12 UTC.
- **Repository HEAD at capture time:** `ae223313f9b18c2ca859e693e8fc54df8fd70a10`
  (`claude/finish-line-rollout`). The production checkout's own SHA is not
  readable from the `dev` account (it lives under `qamc`), so it is
  deliberately not asserted here — the served bundle hash above is the
  exact artifact these screenshots depict.
- **Method:** local headless Playwright Chromium on this VPS, one page per
  view, full-page capture. Modal/overlay views are viewport captures
  because the modal is a fixed, viewport-anchored surface with its own
  internal scroll — those ship as a top + scrolled pair.
- **Console health:** zero browser console errors and zero page errors
  across every captured view (asserted via Playwright's `console` /
  `pageerror` listeners during capture).
- **Production untouched:** Mission Control is read-only by construction;
  no trading path, timer, service or config was involved.

## Live state at capture time — read this before judging empty panels

Several panels are legitimately sparse right now. That is the real state
of the system, not a broken capture:

- The **latest run** is `evening-2dc33ad6` (evening session, 00:00 UTC)
  and it considered **0 candidates**. The cockpit always renders the
  latest run, so the candidate rail, price chart and Decision Room show
  their empty/"not reached" states.
- The portfolio holds a **single position, SGOV** ($9,857.82,
  cash-equivalent sweep parking), with $45 deployable cash. Real risk
  exposure is therefore 0% long / 0% hedge.
- **Missed Opportunities has zero entries** in the underlying data across
  every journal day — the panel renders a large blank region with no
  empty-state message. That is a genuine UX observation, not a capture
  failure.
- Richer decision-chain state is reachable through the Journal and the
  drill-downs (screenshots 09–13), which show a real 94-candidate run
  with PM targets, an AI Risk Manager modification and an execution.

## Screenshots

### Desktop — 1600×1000, dark

| # | File | View / scenario |
|---|------|-----------------|
| 01 | `01-cockpit-overview.png` | Cockpit, default landing. Hero band (equity, risk gauge, market regime), decision-state banner, candidate rail / price chart / Decision Room row, and the Positions & Risk support panel. |
| 02 | `02-cockpit-orders-and-trades.png` | Support workspace → Orders & Trades. |
| 03 | `03-cockpit-runs.png` | Support workspace → Runs (25 real pipeline runs, cost per run). |
| 04 | `04-cockpit-directional-bias.png` | Support workspace → Directional Bias. Real aggregate over the last 25 runs: 914 candidate considerations, bullish/bearish/neutral split, inverse-ETF hedge counts. |
| 05 | `05-cockpit-missed-opportunities.png` | Support workspace → Missed Opportunities. Empty in live data (see note above). |
| 06 | `06-cockpit-search-empty.png` | Support workspace → Search, initial/empty state. |
| 07 | `07-cockpit-search-results.png` | Search executed for a real term (`SGOV`) with live results. |
| 08 | `08-cockpit-system-health.png` | Support workspace → System health. |
| 09 | `09-run-detail-modal.png` | Run detail modal for `run-74cbb7c7` — the 5-stage chain (94 considered → 3 reached PM → 3 proposed, 2 modified → gate → 1 executed) plus the full candidate list. |
| 10 | `10-run-detail-modal-scrolled.png` | Same modal, scrolled to the agent-call detail. |
| 11 | `11-journal.png` | Journal view, full day (2026-08-19) — morning thesis/regime, per-run decisions, candidate chips, trades, evening reflection. |
| 12 | `12-candidate-detail-considered-no-pm-proposal.png` | Candidate drill-down, AAPL — the common "considered, PM made no proposal" shape: earnings/filing evidence and macro context, no decision flow. |
| 12b | `12-candidate-detail-considered-no-pm-proposal-scrolled.png` | Same, scrolled. |
| 13 | `13-candidate-detail-full-decision-chain.png` | Candidate drill-down, XLF — a candidate that went all the way through. |
| 13b | `13-candidate-detail-full-decision-chain-scrolled.png` | Same, scrolled to the decision flow graph: Technical Analyst → Portfolio Manager (REACHED, target 8%) → AI Risk Manager (MODIFIED) → Deterministic Gate (PENDING) → Execution, with the PM narrative below. |
| 14 | `14-legacy-ui-dashboard.png` | The legacy `/ui` Mission Control surface, still deployed and linked from the cockpit's top-right "legacy view". |

### Desktop — light theme

The cockpit's default palette is dark; a light variant resolves under
`prefers-color-scheme: light`. Both ship because an operator's OS setting
decides which one they actually see.

| # | File | View / scenario |
|---|------|-----------------|
| 15 | `15-cockpit-light-theme.png` | Cockpit, light theme. |
| 16 | `16-journal-light-theme.png` | Journal, light theme. |

### Tablet — 820×1180, dark

Below the 1280px breakpoint the cockpit's three columns collapse into a
Candidates / Chart / Decision Room tab strip, and the draggable support
workspace is replaced by a plain tab strip.

| # | File | View / scenario |
|---|------|-----------------|
| 17 | `17-tablet-cockpit-candidates.png` | Cockpit, Candidates tab (default). |
| 18 | `18-tablet-cockpit-chart.png` | Cockpit, Chart tab. |
| 19 | `19-tablet-cockpit-decision-room.png` | Cockpit, Decision Room tab. |
| 20 | `20-tablet-journal.png` | Journal. |

### Phone — 390×844, dark

| # | File | View / scenario |
|---|------|-----------------|
| 21 | `21-phone-cockpit.png` | Cockpit. |
| 22 | `22-phone-journal.png` | Journal. |

## Scope

This directory is evidence only — a complete visual inventory of what the
dashboard currently shows a trader. It records no findings and proposes no
changes.
