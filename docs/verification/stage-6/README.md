# Stage 6 acceptance evidence — richer Mission Control cockpit

Representative browser/runtime verification screenshots for the new
`/cockpit` static bundle (`src/api/static_cockpit/`), captured per
`.claude/rules/frontend-verification.md`. This is a curated acceptance
set, not an exhaustive capture — routine/transient screenshots from the
same verification pass were discarded.

**Captured against commit:** `73c68bf` ("feat(qamc): Stage 6 richer
Mission Control cockpit (src/api/static_cockpit)").

**Verification date:** 2026-08-19, ~01:37–01:44 UTC.

**Method:** seeded a throwaway SQLite DB (`src.storage.db.Database`, same
pattern as `tests/test_api_evidence.py` / `tests/test_api_funnel.py`)
with two representative runs — an executed BUY (AAPL) alongside a
considered-but-neutral bearish-hedge candidate (SQQQ), and a newer
proposed-but-rejected candidate (TSLA) — plus daily P&L history and an
evening reflection with missed up/down opportunities. Broker reads
(`src.api.broker_reads`) were monkeypatched in-process to realistic
values, including an SGOV cash-sweep position, an SQQQ bearish-hedge
position, and an open order, since this dev environment has no live
Alpaca credentials. Ran the real FastAPI app (`python -m` equivalent via
`uvicorn.run`) against this seeded/stubbed state and drove a real
Chromium instance (Playwright, installed for this verification pass) at
each viewport. Each image was visually inspected, not just captured; the
run also confirmed zero browser console errors at every viewport.

## Screenshots

| # | File | Viewport | Scenario |
|---|---|---|---|
| 01 | `01-desktop-dashboard-proposed-not-executed.png` | Desktop (1440px) | Main dashboard: compact top account/liquidity strip, prominent decision-funnel panel showing the latest run's **PROPOSED — NOT EXECUTED** state (TSLA rejected by the AI Risk Manager) with quoted PM/RM/macro text, candidates/watchlist, honest cash/SGOV/risk-exposure breakdown, positions with direction tagging, missed opportunities surfaced on the main screen, orders/trades, runs history, journal, search, and compact system health. |
| 02 | `02-desktop-candidate-drilldown-rejected.png` | Desktop (1440px) | TSLA candidate drill-down: full decision chain (PM reasoning → target → constructed order → AI Risk verdict **REJECTED** → "no trade" outcome), consensus/evidence sections, broader macro context — preserved from the Stage 4-5 content model. |
| 03 | `03-desktop-run-detail-executed.png` | Desktop (1440px) | Run detail modal for the older run: **EXECUTED** state badge, funnel step counts, AAPL (bullish) and SQQQ (bullish-on-the-hedge, bearish-hedge tagged) candidate chips, and the agent-calls table with model/provider/cost/latency. |
| 04 | `04-ipad-dashboard.png` | iPad (820px) | Same main dashboard as #01 at iPad width — confirms the denser 2-column grid reflows without the near-universal full-width stacking of the legacy `/ui` dashboard. |
| 05 | `05-ipad-candidate-drilldown.png` | iPad (820px) | Candidate drill-down modal at iPad width — confirms responsive layout, no overflow, full decision chain still readable. |
| 06 | `06-desktop-dark-mode-dashboard.png` | Desktop (1440px), dark color scheme | Main dashboard under `prefers-color-scheme: dark` — confirms the CSS custom-property theme covers every new Stage 6 component (funnel state badges, liquidity cards, direction pills). |

## Coverage checklist

- [x] Desktop viewport
- [x] iPad viewport
- [x] Dark mode
- [x] Populated state (account, positions, liquidity, funnel, missed opportunities, journal)
- [x] Explicit decision-funnel states: executed, proposed-not-executed, rejected
- [x] SGOV cash-equivalent sweep visually distinct from real positions (dedicated pill, excluded from risk-exposure stat, explanatory note)
- [x] Bearish-hedge (inverse ETF) candidate visible and tagged
- [x] Drill-down / detail views (run detail, candidate detail)
- [x] Zero browser console errors at any captured viewport

Not captured in this curated set (covered instead by the automated test
suite — `tests/test_api_funnel.py`, `tests/test_api_contract.py`,
`tests/test_broker_reads.py`): the hard-risk-block and no-proposal funnel
states, and broker-degraded/error account and position states. Those are
exercised as explicit assertions there rather than re-screenshotted here,
consistent with this rule's "representative, not exhaustive" scope.
