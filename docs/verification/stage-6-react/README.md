# Stage 6 (React rebuild) acceptance evidence — Mission Control cockpit

Representative browser/runtime verification screenshots for the React +
Vite + Tailwind + TradingView Lightweight Charts cockpit
(`frontend/`, compiled into `src/api/static_cockpit/`), captured per
`.claude/rules/frontend-verification.md`. This is a curated acceptance
set, not an exhaustive capture. Supersedes `docs/verification/stage-6/`
(the vanilla-JS prototype) as the acceptance evidence for the `/cockpit`
mount — that directory is left in place as historical record of the
prototype's information architecture, not deleted.

**Captured against commit:** `1265191` ("feat(qamc): React + Vite +
Tailwind cockpit replacing static_cockpit prototype").

**Verification date:** 2026-08-19, ~04:14–04:19 UTC.

**Method:** same seeding pattern as every prior Mission Control
verification pass — a throwaway SQLite DB (`src.storage.db.Database`)
with two representative runs (an executed AAPL BUY + a considered SQQQ
bearish-hedge candidate; a newer TSLA candidate proposed by the PM and
rejected by the AI Risk Manager), daily P&L history, and an evening
reflection with missed up/down opportunities. Broker reads
(`src.api.broker_reads`) were monkeypatched in-process to realistic
values, including an SGOV cash-sweep position, an SQQQ bearish-hedge
position, an open order, and 60 sessions of synthetic daily OHLCV bars
for the price-chart panel (this dev environment has no live Alpaca
credentials). Ran the real FastAPI app (`uvicorn.run`) against this
seeded/stubbed state and drove a real Chromium instance (Playwright) at
each viewport. Each image was visually inspected, not just captured; the
run also confirmed **zero browser console errors** at every viewport
(one real bug — a `lightweight-charts` color-parsing crash from the
theme's space-separated `rgb()` CSS Color-4 syntax — was found and fixed
during this pass, not merely worked around; see `frontend/src/
components/PriceChartPanel.tsx`'s `readThemeColors`).

## Screenshots

| # | File | Viewport | Scenario |
|---|---|---|---|
| 01 | `01-desktop-dashboard-proposed-not-executed.png` | Desktop (1440px) | Main dashboard: compact top strip, prominent decision-funnel panel showing **PROPOSED — NOT EXECUTED** (TSLA rejected by the AI Risk Manager) with quoted PM/RM/macro text, candidates/watchlist, a real daily candlestick + volume chart for the selected candidate, honest cash/SGOV/risk-exposure breakdown, positions with direction tagging, missed opportunities surfaced on the main screen, orders/trades, runs history, journal, search, and compact system health — genuinely two-up/multi-pane on desktop rather than near-universal full-width stacking. |
| 02 | `02-desktop-candidate-drilldown-tsla.png` | Desktop (1440px) | TSLA candidate drill-down: the Orallexa `PerspectivePanelCard`-inspired consensus block (one row per specialist with a directional bias dot + pill, replacing a plain bulleted list), full tech/earnings evidence, macro context, and the numbered PM → AI Risk → execution decision chain ending in **REJECTED**. |
| 03 | `03-desktop-run-detail-executed.png` | Desktop (1440px) | Run detail modal for the older run: **EXECUTED** state badge, funnel step counts (2 considered → 1 target → 1 proposed → 1 executed), AAPL (bullish/long) and SQQQ (bullish-on-the-hedge, explicitly tagged `bearish_hedge`) candidate chips, and the agent-calls table. |
| 04 | `04-ipad-dashboard.png` | iPad (820px) | Same main dashboard as #01 at iPad width — panels genuinely pair up two-per-row (e.g. Cash & Risk Exposure beside Positions) rather than the legacy dashboard's near-total single-column stacking; chart remains fully readable. |
| 05 | `05-ipad-candidate-drilldown.png` | iPad (820px) | Candidate drill-down modal at iPad width — responsive, no overflow, full decision chain and consensus block still readable. |
| 06 | `06-desktop-dark-mode-dashboard.png` | Desktop (1440px), dark color scheme | Main dashboard under `prefers-color-scheme: dark` — confirms the Tailwind CSS-variable theme covers every component including the price chart (grid lines, candle colors, volume bars all correctly themed). |

## Coverage checklist

- [x] Desktop viewport
- [x] iPad viewport
- [x] Dark mode
- [x] Populated state (account, positions, liquidity, funnel, chart, missed opportunities, journal)
- [x] Explicit decision-funnel states visible across the set: executed, proposed-not-executed/rejected
- [x] SGOV cash-equivalent sweep visually distinct from real positions (dedicated pill, excluded from risk-exposure stat, explanatory note)
- [x] Bearish-hedge (inverse ETF) candidate visible and tagged, including in the run-detail modal (fixed during this pass — it was previously only tagged in the main Watchlist panel)
- [x] Real candlestick + volume chart (TradingView Lightweight Charts) populated from `GET /prices/{symbol}`
- [x] Drill-down / detail views (run detail, candidate detail)
- [x] Zero browser console errors at any captured viewport

Not re-captured in this set (already exercised as explicit backend
assertions, consistent with this rule's "representative, not exhaustive"
scope, and structurally unchanged from the prior vanilla-prototype
pass): the hard-risk-block and no-proposal funnel states, and
broker-degraded/error account and position states —
`tests/test_api_funnel.py`, `tests/test_api_contract.py`,
`tests/test_broker_reads.py`.
