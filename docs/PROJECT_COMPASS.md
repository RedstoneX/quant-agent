# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🚦 RIGHT NOW

### 🖥️ VPS deployment / hardening is the active tranche

Stage 4–5 is accepted and PR #24 is merged into `main` as `105cc91a14faebd8a981061b3098eb181b306dda`.

The next bounded engineering objective is now authorized: move the accepted QAMC paper-trading engine + read-only Mission Control bundle to the OVH VPS, establish durable/supervised operation and private access, verify the deployed runtime/browser behavior, then stop for independent review and operator UAT.

Dedicated dashboard visualization/UX polish remains **after** deployed-MVP acceptance.

## 🗺️ PROJECT MAP

| Status | Stage / milestone | Result |
|---|---|---|
| ✅ DONE | 0–2 | Trading-engine audit, model/provider attribution/plumbing, isolated read-only Mission Control API. |
| ✅ DONE | Discovery/Reconciliation R1 | Post-Stage-2 architecture/data direction independently challenged and accepted. |
| ✅ DONE | 3 | Browser/iPad Trading Cockpit at `/ui`; accepted verification 1531 passed. |
| ✅ DONE | 4–5 | Specialist evidence + decision chain, journal + forensic search; accepted verification 1558 passed plus committed browser/runtime evidence. PR #24 merged. |
| 🟨 AUTHORIZED NOW | VPS cutover / deployment hardening + deployed verification | Deploy accepted bundle to OVH Ubuntu 24.04 VPS; private access, durable data, secrets outside Git, supervision/restarts, logs/health/recovery, runtime/browser QA; then push and stop for independent review. |
| ⬜ NEXT GATE | Independent review + operator UAT | Challenge the deployed result; operator determines whether the solid deployed MVP is usable and accepted. |
| ⬜ AFTER MVP ACCEPTANCE | Mission Control visualization / UX polish | TradingView-style charting, richer visualizations/navigation/density and desktop/iPad refinement without changing safety architecture. |
| ⬜ LATER | Learning/write controls + paper-soak analytics | Separate future authorization only. |

## 🖥️ DEPLOYMENT TARGET

- OVH VPS: `vps-37b5f875.vps.ovh.us` / `135.148.120.105`
- Ubuntu 24.04
- 100 GB storage; no extra storage purchased
- OVH automated daily backup active
- $14.50/month, no commitment
- Current operator environment: iPad only
- Claude Code environment: Anthropic cloud

Initial access uses a disposable Ed25519 bootstrap key generated in Claude's cloud environment. The private key stays there only; the operator receives only the public key for OVH installation. Once persistent VPS access is established, the disposable credential is revoked.

## ✅ DEPLOYMENT ACCEPTANCE DIRECTION

The active work contract requires the deployed system to remain Alpaca Paper-only, preserve deterministic risk/broker protections, keep Mission Control read-only and non-critical, keep secrets outside Git/client surfaces, recover cleanly from process/reboot failure, preserve durable data, and pass deployed automated + browser/runtime verification before external review.

The operator should not be used as the routine technical architect. Claude owns implementation planning and safe parallel engineering inside the accepted work contract, then stops at the checkpoint.

## ⏭️ NEXT MOVES

1. Claude rehydrates from `main` and the newly authorized `STATE.md` / `WORK.md`.
2. Claude generates the disposable Ed25519 bootstrap keypair in its cloud environment and returns **only the public key**, then pauses.
3. Operator installs that public key in OVH from the iPad, one guided action at a time.
4. Claude verifies SSH, performs the authorized deployment/hardening/verification tranche, pushes its branch and stops.
5. ChatGPT independently reviews the pushed deployment result; operator UAT follows only after that review.
6. If UAT passes, accept the deployed MVP; only then authorize dedicated visual polish.

## 🚧 BLOCKERS / DECISIONS NEEDED

The only immediate dependency is installing Claude's disposable SSH public key on the OVH VPS. No product or architecture decision is currently blocked.

_Last refreshed: 2026-08-09 18:39 EDT (America/Toronto) — active project view only; retired/superseded work lives in Git history._
