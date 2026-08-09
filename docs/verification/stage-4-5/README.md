# Stage 4–5 acceptance evidence — Mission Control cockpit

Representative browser/runtime verification screenshots for the Stage 4
(per-candidate specialist evidence + decision chain) and Stage 5
(journal + forensic search) cockpit UI, captured per
`.claude/rules/frontend-verification.md`. This is a curated acceptance
set, not an exhaustive capture — routine/transient browser-test
screenshots from the same verification pass were discarded.

**Captured against commit:** `dcda4814be1b96ab6f03c1cc9208a7a3dcf0dc9e`
("feat(qamc): Stage 5 journal + forensic search cockpit UI").
`src/api/static/` is byte-identical between that commit and the current
HEAD (`c4965ea5ac9c056eb714d8df1b3873723216dd24`) — verified via
`git diff --quiet dcda481 HEAD -- src/api/static/` — so this evidence
remains accurate for the reconciled branch; no UI files changed in the
reconciliation/pruning follow-up.

**Verification date:** 2026-08-09, ~18:56–19:12 UTC.

**Method:** seeded a throwaway SQLite DB (`src.storage.db.Database`, same
pattern as `tests/test_api_evidence.py` / `tests/test_api_journal.py`)
with representative runs/candidates/journal days covering the states
below, ran `python -m src.api` against it, and drove a real Chromium
instance (Playwright) at each viewport to load `/ui/` and click through
the flows. Each image was visually inspected, not just captured.

## Screenshots

| # | File | Viewport | Scenario |
|---|---|---|---|
| 01 | `01-desktop-dashboard-populated-degraded.png` | Desktop (1280px) | Main dashboard: populated Recent Trades/Runs/Watchlist/Health panels alongside an honestly **degraded** Account/Positions/Orders state (sandboxed network has no broker route — shown as intended, not masked). |
| 02 | `02-desktop-candidate-drilldown-full-evidence.png` | Desktop (1280px) | Candidate drill-down (AAPL) with full evidence: tech + earnings + symbol news, clearly-labeled broader macro/news context, aligned consensus, and the complete PM → AI Risk → execution decision chain with model/provider/cost/latency per agent call. |
| 03 | `03-desktop-candidate-drilldown-partial-evidence.png` | Desktop (1280px) | Candidate drill-down (MSFT) with only partial specialist evidence — confirms missing fields render an honest "not available" state rather than blank/fabricated data. |
| 04 | `04-desktop-candidate-drilldown-rm-modified-delta.png` | Desktop (1280px) | Candidate drill-down (NVDA) where the AI Risk Manager modified the PM's proposed order — the proposed-vs-executed delta table highlights the size/price difference explicitly (docs/WORK.md Stage 4 requirement). |
| 05 | `05-desktop-candidate-drilldown-rejected.png` | Desktop (1280px) | Candidate drill-down (TSLA) for a decision the AI Risk Manager **rejected** before execution — "REJECTED" verdict badge, no trade row, insufficient-data consensus (only one specialist signal fired). |
| 06 | `06-ipad-candidate-drilldown-full-evidence.png` | iPad (820px) | Same full-evidence candidate drill-down as #02, at iPad width — confirms responsive layout, no overflow. |
| 07 | `07-dark-mode-candidate-drilldown.png` | Desktop (1280px), dark color scheme | RM-modified candidate drill-down (#04's scenario) under `prefers-color-scheme: dark` — confirms the CSS custom-property theme covers the new drill-down components. |
| 08 | `08-desktop-journal-search-populated.png` | Desktop (1280px) | Journal panel showing a fully-populated trading day (equity snapshot, evening reflection with grades/missed-opportunities, that day's runs/trades/candidates) plus the Search panel. |
| 09 | `09-desktop-journal-partial-day-empty-fields.png` | Desktop (1280px) | Journal panel on a day with only partial data (daily P&L recorded, no evening reflection yet) — confirms honest "No equity snapshot recorded" / "No evening reflection recorded yet" messages instead of blank fields. |
| 10 | `10-desktop-search-no-matches-empty-state.png` | Desktop (1280px) | Forensic search with a query that matches nothing — honest empty-result state, not an error. |
| 11 | `11-ipad-journal-search.png` | iPad (820px) | Journal + Search panels at iPad width — confirms responsive layout for the Stage 5 additions. |

## Coverage checklist

- [x] Desktop viewport
- [x] iPad viewport
- [x] Dark mode
- [x] Populated state
- [x] Empty/partial-data state (candidate evidence, journal day, search)
- [x] Degraded state (broker-unreachable panels)
- [x] Error/rejected state (AI Risk Manager rejection)
- [x] Drill-down / detail views (run detail, candidate detail)
- [x] Proposed-vs-executed delta (RM modification)
