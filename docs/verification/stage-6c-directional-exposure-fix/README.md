# Stage 6c acceptance evidence — instrument signal vs. market exposure fix

External review found an important semantic bug in the Directional Bias
panel (`docs/verification/stage-6b-deliberation-journal-bias/`):
`CandidateFunnelItem.direction` is the **instrument's own signal**
(tech_analyst's rating on the symbol itself), not the resulting
market/portfolio exposure. For an approved inverse ETF (`is_bearish_hedge`
— SH/SDS/PSQ/SQQQ), a bullish instrument signal (BUY SQQQ) expresses
**bearish** exposure to the underlying index. The panel was bucketing
`direction` directly as bullish/bearish exposure, which would silently
hide exactly the bearish positioning it exists to surface — in a panel
whose stated purpose is diagnosing structural long bias, that's the one
mistake it couldn't afford to make.

**Captured against commit:** (this tranche's HEAD at push time — see the
top-level commit this README ships in).

**Verification date:** 2026-08-19, ~05:53–05:55 UTC.

## Fix

`frontend/src/components/DirectionalBiasPanel.tsx` now computes and
displays **two separate, clearly labeled series** everywhere direction is
aggregated (candidates considered, and PM proposals that reached a
proposed order):

- **Instrument signal direction** — unchanged from before, the raw
  `direction` field.
- **Effective market exposure** (labeled "inverse-ETF candidates
  flipped") — `exposureDirection(direction, is_bearish_hedge)`: passes
  direction through unchanged for ordinary long instruments, flips
  bullish↔bearish only when `is_bearish_hedge` is true. Derived solely
  from the API's own `is_bearish_hedge` flag — never a symbol-name
  heuristic.

An explanatory sentence under the candidates bar makes the distinction
explicit in plain language whenever a hedge candidate is present.

## Screenshots

| # | File | Scenario |
|---|---|---|
| 01 | `01-desktop-instrument-vs-exposure.png` | Desktop, light — the exact case the fix targets: 3 candidates all read "bullish" at the instrument level (100% bullish, 0% bearish) — the pre-fix, misleading number. The Effective Market Exposure bar directly below shows the true picture: 67% bullish / 33% bearish, because one of the three (SQQQ) is an inverse ETF and its bullish instrument signal is bearish exposure. |
| 02 | `02-desktop-dark-instrument-vs-exposure.png` | Same scenario, dark mode — bar color split (green→red) themed correctly. |
| 03 | `03-ipad-instrument-vs-exposure.png` | Same scenario, iPad width — both bars remain fully readable, no overflow. |

Zero browser console errors during capture (confirmed via Playwright's
console/pageerror listeners, not just visual inspection).

## Tests

`frontend/src/components/DirectionalBiasPanel.test.ts` (Vitest, new
dependency — the natural test runner for a Vite project, dev-only,
adds no runtime dependency to the built cockpit; confirmed the test file
and `describe`/`vitest` strings are absent from the production bundle).
8 tests, all passing:

- `exposureDirection` unit tests: ordinary instrument passthrough
  (bullish/bearish/neutral/unknown all unchanged when `is_bearish_hedge`
  is false); the flip itself (bullish→bearish and bearish→bullish when
  `is_bearish_hedge` is true); neutral/unknown left unflipped even on a
  hedge instrument.
- `computeAggregates` integration tests over constructed `RunFunnelResponse`
  fixtures: **AAPL bullish (non-hedge) → bullish exposure**; **SQQQ
  bullish-on-instrument → bearish exposure**; a **mixed candidate set**
  (AAPL + MSFT + SQQQ, all instrument-bullish) proving the headline
  *count* itself changes (3 bullish instrument → 2 bullish / 1 bearish
  exposure), not just which candidate lands in which bucket; the same
  split applied to PM proposals specifically; and a bearish-instrument-
  on-inverse-ETF case (SH bearish → bullish exposure) confirming the flip
  isn't one-directional.

```
$ cd frontend && npm test
 ✓ src/components/DirectionalBiasPanel.test.ts (8 tests) 7ms
 Test Files  1 passed (1)
      Tests  8 passed (8)
```

## Verification

- `cd frontend && npm run build` — zero TypeScript errors.
- `cd frontend && npm test` — 8/8 passing.
- Backend suite unaffected (this is a frontend-only change): full suite
  still 1857 passed, 0 failed.
- No trading behavior changed — this is a read-only observability panel;
  the fix only changes how already-computed, already-truthful API data
  (`direction`, `is_bearish_hedge`) is labeled and bucketed for display.
