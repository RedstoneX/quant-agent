# Stage 6b acceptance evidence — agent deliberation, journal narrative, directional bias

Representative browser/runtime verification screenshots for this session's
multi-workstream tranche on top of the already-accepted Stage 6 React
cockpit (`docs/verification/stage-6-react/`): specialist agent cards +
decision-flow diagram + richer decision-process detail, a day-by-day
journal narrative rebuild, a directional-bias observability panel, and a
follow-on visual-QA pass. Captured per
`.claude/rules/frontend-verification.md`. Curated, not exhaustive.

**Captured against commit:** `ae0f1c1` ("feat(qamc): visual QA pass —
depth, contrast, agent identity, responsive fixes").

**Verification date:** 2026-08-19, ~05:22–05:30 UTC.

**Method:** same seeding pattern as every prior Mission Control
verification pass (`src.storage.db.Database`, monkeypatched
`src.api.broker_reads`/`routes_live` broker reads and synthetic OHLCV
price bars, real FastAPI app via `uvicorn.run`, real Chromium via
Playwright) — see `docs/verification/stage-6-react/README.md` for the
full method description, unchanged here except for a rebuild against
the new frontend source. Zero browser console errors confirmed at
every viewport across three separate capture passes during this
session (before visual-QA changes, after the depth/contrast pass, and
after the two responsive-layout fixes).

## Screenshots

| # | File | Viewport | Scenario |
|---|---|---|---|
| 01 | `01-desktop-dashboard-full.png` | Desktop (1440px) | Full main dashboard: decision-flow diagram (Specialists → PM → AI Risk → Gate → Execution, with per-stage reached/rejected/not-reached coloring) replacing the old four-number step row, the new Directional Bias panel (candidate/proposal/verdict breakdowns by direction, outcome histogram, explicit "not a trading signal" framing), the rebuilt Journal narrative (morning regime, per-run decision cards with candidate direction tags, full evening reflection), and the visual-QA depth/contrast pass (panel shadows, accent-bordered narrative panels) all together. |
| 02 | `02-desktop-specialist-cards-decision-flow.png` | Desktop (1440px) | TSLA candidate drill-down: the new `SpecialistCards` (colored agent-identity badges — "T"/"E" — per-specialist direction/conviction/reasoning, "Aligned with consensus" indicator) and the same decision-flow diagram reused at candidate scope. |
| 03 | `03-desktop-run-detail-executed.png` | Desktop (1440px) | Run detail modal for the executed run: funnel counts, AAPL (bullish/long) and SQQQ (bullish-on-the-hedge, correctly tagged `bearish_hedge` — this was a real gap found and fixed during Stage 6 integration, confirmed still correct here) candidate chips, agent-calls table. |
| 04 | `04-ipad-dashboard-full.png` | iPad (820px) | Full dashboard at iPad width, post-fix: Cash & Risk Exposure and System Health panels now render as a clean 2×2 grid instead of 4 cramped columns (see #07), all panels reflow sensibly. |
| 05 | `05-ipad-specialist-cards.png` | iPad (820px) | Specialist cards / decision-flow diagram at iPad width — responsive, no overflow. |
| 06 | `06-desktop-dark-mode-full.png` | Desktop (1440px), dark color scheme | Full dashboard under `prefers-color-scheme: dark` — every new component (decision-flow diagram, specialist card badges, directional-bias bars, journal narrative cards) themed correctly. |
| 07 | `07-ipad-top-strip-wrap-fix.png` | iPad (820px), header only | Before/after-relevant: the top account-stat strip now wraps all 5 stats (Equity, Day P&L, Unrealized P&L, Deployable Cash, Sweep Parked) across two lines instead of hiding 4 of them behind an undiscoverable horizontal scroll — a real bug found and fixed during this session's visual-QA pass. |

## Bugs found and fixed during this session (not just cosmetic — verified via real rendering, not assumed)

- Two `sm:` (640px) viewport-width Tailwind breakpoints driving 4-column
  grids inside panels that are only ever half-width once the main layout
  goes 2-column at `md:` (768px) — cramped labels into ~90px at iPad's
  820px. Fixed in `LiquidityPanel.tsx`/`HealthPanel.tsx` by moving the
  escalation to `lg:` (1024px).
- The top account-stats strip used `overflow-x-auto`; once its flex-1'd
  width got squeezed by the brand/meta blocks at tablet width, it hid 4
  of 5 stats behind an undiscoverable horizontal scroll. Replaced with
  `flex-wrap` + a real `min-width` in `TopStrip.tsx`.
- Light-mode background/panel/panel-alt contrast sat only ~5 units apart,
  reading as flat/monochrome — increased contrast and added a subtle
  panel shadow.

## Coverage checklist

- [x] Desktop viewport
- [x] iPad viewport
- [x] Dark mode
- [x] Decision-flow diagram at both run-level (Directional Bias/Funnel panels) and candidate-level (drill-down modal)
- [x] Specialist agent cards with identity badges, direction, conviction, alignment indicator
- [x] Directional-bias panel: candidate/proposal/verdict breakdowns, outcome histogram, neutral factual framing (no recommendation language)
- [x] Journal narrative: morning regime, per-run decision cards, full evening reflection fields, prev/next date navigation
- [x] `reason_category` and full RM/PM reasoning-chain breakdowns surfaced in the candidate decision chain
- [x] Bearish-hedge (inverse ETF) tagging correct in both the main watchlist and the run-detail modal
- [x] Zero browser console errors at any captured viewport, across three separate passes this session
- [x] Two real responsive bugs found and fixed (not merely noted)

Not re-captured in this set (unchanged from the prior Stage 6 pass,
already covered there): the hard-risk-block/no-proposal funnel states,
broker-degraded/error states — `tests/test_api_funnel.py`,
`tests/test_api_contract.py`.
