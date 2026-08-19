# Stage 6e — branch-preview API contract fix, verified against real production data

Evidence that `ops/preview/branch_preview.py`'s version-skew blocker (the
known limitation recorded in Stage 6d — the decision funnel, directional
bias, and liquidity/direction panels showing an honest "could not load"
state because `qamc` production predates this branch's new backend
surface) is fixed, and that the resulting cockpit renders **real** `qamc`
production data end-to-end, not seeded/fabricated data.

**Captured against:** `ops/preview/branch_preview.py` as committed in this
change (working tree at the time of capture, committed immediately after).
The `/cockpit` static bundle itself is unchanged since `05b4b59` — this
tranche did not touch `frontend/` or `src/api/`; only the ephemeral,
dev-account-only preview proxy was fixed.

**Verification date:** 2026-08-19, ~08:18–08:20 UTC.

**Method:** started `ops/preview/branch_preview.py` for real (`--host
100.111.170.97 --port 8810`, bound only to the VPS's Tailscale interface,
exactly as documented in `ops/preview/README.md`), confirmed `qamc`
production's `/health` was unaffected before/after (identical
`last_run_files` timestamps, `session_lock_active: false`, same PID/port
ownership), then drove it two ways:

1. Interactively, in a real connected Chrome browser (screenshots
   reviewed live during the session, not saved to this repo — see the
   session transcript) — confirmed the top-level dashboard, run detail
   modal, and candidate drill-down all render, and that zero requests
   failed (59/59 network requests returned 200, checked via
   `read_network_requests`).
2. With a local headless Playwright script (`playwright` is already a
   project dependency) driving a real Chromium instance against the same
   running preview server, to produce the committable screenshots below
   and an independent, scripted **zero console errors** check across
   every viewport captured (desktop, iPad, and the legacy `/ui`
   fallback).

## What was actually broken and what the fix is

Old `qamc` production predates this branch's Stage 6 backend additions:
`AccountResponse.liquidity`, `PositionItem.direction`/`is_cash_equivalent`,
and `GET /runs/{id}/funnel` don't exist on the running production API
(verified directly: `curl 127.0.0.1:8800/account` has no `liquidity` key,
`/positions` items have no `direction` key, `/runs/{id}/funnel` 404s).
Since the preview proxies to production for real data by design (never
duplicating or faking state), every panel depending on those was blank.

The fix reconstructs exactly those fields/endpoints inside
`branch_preview.py` itself, from data upstream **already** exposes,
using the same typed schemas (`src.api.schemas`) and the same pure
derivation rules this branch's own backend uses
(`_compute_liquidity`'s raw-cash/sweep/reserve/deployable math,
`_position_direction`'s symbol-based labeling, and
`get_run_funnel`'s specialist→PM→risk→execution aggregation over
`GET /runs/{id}/candidates(/{symbol})`, which **are** already deployed).
It never reads `qamc`'s database or broker credentials directly — see
the updated module docstring and `_reconstruct_funnel`'s docstring for
exactly which already-deployed endpoint each reconstructed field is
sourced from. Every reconstruction is self-obsoleting: once this branch
merges and `qamc` production actually returns the field/route, the real
upstream value passes straight through untouched (`if data.get("liquidity")
is not None: return data`, etc.) — verified structurally, not just
asserted, by the `if` guards in `_patch_account`/`_patch_positions`/the
`runs/.../funnel` branch of `proxy()`.

One field is **not** reconstructed: `GET /prices/{symbol}` (new this
branch) still degrades honestly (empty bars + an explanatory `error`
string) because real bars require Alpaca market-data credentials this
`dev`-account preview process deliberately does not have. This was a
known, accepted limitation before this fix and remains one — the price
chart component's own correctness against real bars was already verified
with seeded data in `docs/verification/stage-6-react/`.

## Screenshots

| # | File | Viewport | Scenario |
|---|---|---|---|
| 01 | `01-desktop-dashboard-top.png` | Desktop (1440px) | Top strip + **Latest Decision** funnel panel, populated with the real latest run (`evening-19ce5f13`, honest `NO CANDIDATES CONSIDERED`) + **Directional Bias** panel aggregated from 21 real recent runs (820 real candidate-considerations, instrument-signal vs. effective-market-exposure split, inverse-ETF hedge consideration counts) — both were blank/"Loading…" before this fix. |
| 02 | `02-desktop-dashboard-lower.png` | Desktop (1440px) | **Cash & Risk Exposure** panel with real reconstructed `liquidity` breakdown (raw cash $145.11, SGOV sweep-parked $9,857, reserve floor, deployable cash) and **Positions** with SGOV correctly tagged `CASH_EQUIVALENT` — both fields absent from old production's raw `/account`/`/positions` response. |
| 03 | `03-desktop-journal.png` | Desktop (1440px) | Journal rebuilt as a day narrative, showing a real run's `81 considered → 0 reached PM → 0 proposed → 0 executed` funnel line — sourced from the same reconstructed funnel data. |
| 04 | `04-desktop-run-detail-modal.png` | Desktop (1440px) | Run detail modal for a real full-universe-scan run (`run-d7bbdebe`): 81 real candidates each correctly direction-tagged (bullish/bearish/neutral/unknown), `SQQQ` tagged `BEARISH_HEDGE`, and real per-agent model/cost/latency (`tech_analyst`, $0.03, 74.27s, etc.). |
| 05 | `05-desktop-candidate-drilldown.png` | Desktop (1440px) | Candidate drill-down for `AAPL` within that run — real Earnings Analyst evidence (bullish, high conviction, actual 10-Q-derived thesis) and real macro regime context, reached via the same reconstructed data. |
| 06 | `06-desktop-candidate-decision-flow.png` | Desktop (1440px) | Same drill-down, scrolled — real macro sector-guidance table and news/market narrative context, both genuinely from the run's stored evidence. |
| 07 | `07-ipad-dashboard-top.png` | iPad (820px) | Same Latest Decision + Directional Bias panels at iPad width — responsive, populated with the same real data. |
| 08 | `08-ipad-dashboard-lower.png` | iPad (820px) | Same Cash & Risk Exposure / Positions pairing at iPad width. |
| 09 | `09-legacy-ui-fallback.png` | Desktop (1440px) | `/ui/` (the Stage 3-5 dashboard) confirmed still served, unmodified, real data — the required rollback path remains intact. |

## Checks performed

- [x] Real production data renders end-to-end for the decision funnel,
      directional bias, liquidity breakdown, and position direction
      panels — the specific blocker this tranche was asked to fix.
- [x] Reconstruction is genuinely derived from real upstream data (traced
      by hand against `curl 127.0.0.1:8800/...` output during
      development), never fabricated — confirmed a nonexistent run_id
      still 404s (`GET /runs/does-not-exist-12345/funnel` → 404), and a
      real candidate-less run (`close-be17952f`) returns an honest
      `no_candidates` funnel rather than inventing content.
- [x] Zero browser console errors, desktop + iPad + legacy `/ui`
      (scripted Playwright check).
- [x] Zero failed network requests observed interactively (59/59 → 200).
- [x] SGOV unmistakably `CASH_EQUIVALENT`, excluded from risk exposure,
      with the explanatory line intact.
- [x] `qamc` production `/health` identical before and after (same
      `last_run_files`, `session_lock_active: false`) — untouched,
      never restarted.
- [x] Legacy `/ui` fallback still served and functional.
- [x] Preview process still binds only to the Tailscale interface
      (`100.111.170.97:8810`), still GET-only (`POST /account` → 405).
- [x] Alpaca remains Paper (`"paper": true` throughout); no deterministic
      risk/execution code touched by this tranche.
- [x] No secrets in this reconstruction — only `config/settings.yaml`'s
      non-secret `cash_sweep` section is read directly (bypassing
      `src.api.deps.get_config()`'s full `AppConfig` validation, which
      would otherwise raise for this `dev`-account process over a
      missing `OPENROUTER_API_KEY` it has no reason to have).

## Remaining known limitation (unchanged from Stage 6d)

`GET /prices/{symbol}` still degrades honestly in this preview (no real
Alpaca market-data credentials on `dev`) — expected, documented, not a
regression. Resolves automatically once this branch is merged and
deployed to `qamc` production.
