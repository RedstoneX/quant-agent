# Stage 6d acceptance evidence — operator branch-preview endpoint

Verifies `ops/preview/branch_preview.py`: the current feature branch's
`/cockpit` frontend, reachable over Tailscale only, rendering **real**
`qamc` production paper-account data via a read-only GET proxy to
`127.0.0.1:8800` — not seeded/synthetic data, and not a duplicate/faked
runtime state.

**Verification date:** 2026-08-19, ~05:58–06:03 UTC.

**Preview URL used:** `http://100.111.170.97:8810/cockpit/` (Tailscale IP)
— confirmed equivalent to `http://ovh-vps.wallaby-bowfin.ts.net:8810/cockpit/`
via MagicDNS resolution.

## Screenshots

| # | File | Viewport | Scenario |
|---|---|---|---|
| 01 | `01-desktop-live-production-data.png` | Desktop (1440px) | Real `qamc` paper-account data proxied live: actual equity/day-P&L, real SGOV sweep position, real multi-day runs history (Aug 14–19), real evening-reflection narrative text, real search/health state. Panels dependent on this branch's new-this-tranche backend endpoints (not yet deployed to production) show an honest "Could not load … HTTP 404" state rather than fabricated data — see `ops/preview/README.md`'s documented, expected limitation. |
| 02 | `02-ipad-live-production-data.png` | iPad (820px) | Same live data, responsive layout holds up. |

## Verification performed

- **Tailscale-only reachability:** `curl http://100.111.170.97:8810/` and
  `curl http://ovh-vps.wallaby-bowfin.ts.net:8810/health` both succeed.
- **Public-IP unreachability:** `curl http://135.148.120.105:8810/`
  times out / connection refused — confirmed the bind itself (not a
  firewall rule) makes this structurally unreachable.
- **Loopback unreachability:** `curl http://127.0.0.1:8810/` also fails
  — confirms the bind is scoped to the Tailscale interface specifically,
  not merely "not the public IP."
- **GET-only enforcement:** `POST /account` and `DELETE /positions`
  against the preview both return `405`, before any request reaches the
  upstream production API.
- **Real data, not fabricated:** `/account` through the preview returned
  the exact same live figures as `curl`ing `127.0.0.1:8800/account`
  directly.
- **Production untouched:** `qamc`'s Mission Control API process (PID
  `1790842`) had identical uptime and `/health` output before and after
  this entire preview session — never restarted, never written to.
- **Static mounts:** `/ui/` and `/cockpit/` both served correctly from
  this checkout's actual committed files.
- **Zero *unexplained* console errors:** the only browser console errors
  during this session were the expected, already-diagnosed
  `/runs/{id}/funnel` 404s (30 of them, from the panels that fan out
  over recent runs) — see `ops/preview/README.md`. No other console
  errors, no unhandled exceptions, no crashes.
