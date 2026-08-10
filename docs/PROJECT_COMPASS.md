# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🚦 RIGHT NOW

### 🖥️ VPS deployment / hardening checkpoint complete

The accepted QAMC paper-trading engine + read-only Mission Control bundle has been deployed to the OVH VPS.

Current status:
- Mission Control API/UI deployed under supervised `systemd --user` service
- Private bind only (`127.0.0.1:8800`)
- Full deployed test suite verified: 1558 passed
- Trading timers installed but intentionally disabled
- Real secrets have not been placed on the VPS
- Independent review completed after branch push

Dedicated dashboard visualization/UX polish remains **after deployed-MVP acceptance**.

## 🗺️ PROJECT MAP

| Status | Stage / milestone | Result |
|---|---|---|
| ✅ DONE | 0–2 | Trading-engine audit, model/provider attribution/plumbing, isolated read-only Mission Control API. |
| ✅ DONE | Discovery/Reconciliation R1 | Post-Stage-2 architecture/data direction independently challenged and accepted. |
| ✅ DONE | 3 | Browser/iPad Trading Cockpit at `/ui`; accepted verification completed. |
| ✅ DONE | 4–5 | Specialist evidence + decision chain, journal + forensic search; accepted verification completed. PR #24 merged. |
| ✅ DONE | VPS deployment / hardening | OVH deployment completed. Runtime separated from development environment. |
| 🟨 NEXT GATE | Operator UAT | Validate deployed MVP usability before acceptance. |
| ⬜ AFTER MVP ACCEPTANCE | Mission Control visualization / UX polish | TradingView-style charting, richer visualizations/navigation/density and desktop/iPad refinement without changing safety architecture. |
| ⬜ LATER | Learning/write controls + paper-soak analytics | Separate future authorization only. |

## 🖥️ DEPLOYMENT TARGET

- OVH VPS: `vps-37b5f875.vps.ovh.us` / `135.148.120.105`
- Ubuntu 24.04
- Runtime account: `qamc`
- Development account: `dev`

Account separation:

- `ubuntu` = OVH administration / recovery
- `qamc` = QAMC runtime only
- `dev` = development, Claude Code, and future projects

Development environment:

```
/home/dev/
    projects/
        quant-agent/
```

Claude Code runs on demand as `dev`. No persistent agent daemon is currently authorized.

## ✅ DEPLOYMENT ACCEPTANCE DIRECTION

The active work contract requires the deployed system to remain Alpaca Paper-only, preserve deterministic risk/broker protections, keep Mission Control read-only and non-critical, keep secrets outside Git/client surfaces, recover cleanly from process/reboot failure, preserve durable data, and pass deployed automated + browser/runtime verification before external review.

## ⏭️ NEXT MOVES

1. Complete operator UAT of the deployed MVP.
2. Only after acceptance, authorize visualization/UX polish.
3. Keep development work under `dev`; keep runtime under `qamc`.

## 🚧 BLOCKERS / DECISIONS NEEDED

Current blockers:
- Real API secrets must be supplied directly to the VPS before live paper-trading verification.
- Full browser screenshot verification requires remaining OS package installation.

No product or architecture decision is currently blocked.

_Last refreshed: 2026-08-10 22:00 EDT (America/Toronto) — active project view only; retired/superseded work lives in Git history._
