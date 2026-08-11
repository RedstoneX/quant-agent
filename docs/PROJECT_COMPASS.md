# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## QAMC meaning and origin

QAMC = Quant Agent Mission Control.

QAMC is the operator-facing Mission Control layer built around the open-source quant-agent project from GitHub.

quant-agent is the underlying autonomous AI-assisted trading engine and provides the foundation:
- agent framework
- execution workflow
- risk controls
- memory/journaling
- paper-trading infrastructure

QAMC is the Mission Control/operator layer:
- dashboard
- agent visibility
- decision inspection
- journal and forensic review
- charts and future visualization

## 🚦 RIGHT NOW

### 🔐 Credential architecture built — stopped on one bundled operator action

Stage 4–5 is accepted and PR #24 is merged into `main` as `105cc91a14faebd8a981061b3098eb181b306dda`.

The Mission Control API/UI is live and supervised on the VPS (`127.0.0.1:8800`, restarts on crash/reboot); the full test suite passes there (1558/0); the trading-schedule timers are installed but intentionally left off. All 9 agents now route through OpenRouter. A credential-injecting proxy (so real secrets never touch `qamc` or `dev`) is built, empirically tested, and pushed — but not yet provisioned with real values. Claude stopped and pushed branch `claude/vps-deployment-hardening-q3f7k2` because the remaining steps require operator-only actions — see Blockers below.

Dedicated dashboard visualization/UX polish remains **after** deployed-MVP acceptance.

## 📌 WHAT JUST HAPPENED

- Screenshot blocker (below, previously open) is **resolved**: operator installed the missing Chromium shared libraries; full desktop/iPad screenshot verification completed against the live UI with real, non-fabricated evidence.
- All 9 agents migrated to route through OpenRouter instead of direct Anthropic/OpenAI (config-only change, verified against the live `resolve_provider()` logic).
- OneCLI (a candidate open-source credential gateway) was investigated directly from its own source/install script, not assumed: it unconditionally requires Docker+Postgres with no lighter mode, and `dev` has neither Docker nor sudo. Claude rejected installing the product but implemented its underlying pattern as a minimal, empirically-tested proxy instead — see `docs/architecture/CREDENTIAL_PROXY.md`.
- Zero trading/agent/risk code was touched this slice; full 1558-test suite re-confirmed clean.

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
- Claude Code environment: Mac-hosted SSH session direct to the VPS (Anthropic's cloud sandbox can't reach outbound TCP/22)

Access is the operator's own persistent Ed25519 key (`qamc-vps-deploy-20260809`), installed directly — the originally-planned disposable cloud-bootstrap key was never used.

## ✅ DEPLOYMENT ACCEPTANCE DIRECTION

The active work contract requires the deployed system to remain Alpaca Paper-only, preserve deterministic risk/broker protections, keep Mission Control read-only and non-critical, keep secrets outside Git/client surfaces, recover cleanly from process/reboot failure, preserve durable data, and pass deployed automated + browser/runtime verification before external review.

The operator should not be used as the routine technical architect. Claude owns implementation planning and safe parallel engineering inside the accepted work contract, then stops at the checkpoint.

## ⏭️ NEXT MOVES

1. Operator runs the bundled provisioning steps in `ops/credential-proxy/README.md` (one `sudo` script + real secrets entered directly, never through chat) — see Blockers below.
2. Whoever holds `qamc` access adds 3 `Environment=` lines to `quant-agent-api.service` and restarts it (`dev` cannot write into `/home/qamc`).
3. Claude re-verifies `/health` shows `broker_reachable:true`, confirms real OpenRouter/Alpaca/FRED calls succeed, then decides on enabling trading timers, refreshes this checkpoint, pushes.
4. ChatGPT independently reviews the pushed deployment result; operator UAT follows only after that review.
5. If UAT passes, accept the deployed MVP; only then authorize dedicated visual polish.

## 🚧 BLOCKERS / DECISIONS NEEDED

One bundled operator-only action set, not a product/architecture decision (full exact commands in `ops/credential-proxy/README.md`):
1. Provision the dedicated `credproxy` account + CA + service (`sudo bash ops/credential-proxy/setup.sh`) — `dev` has no sudo.
2. Enter the four real credential values directly into `credproxy`'s vault — operator only, never through chat.
3. Add the proxy's 3 env vars to `quant-agent-api.service` and restart it — needs `qamc` access, which `dev` does not have.

## 🛡️ SAFETY

Paper-only, deterministic risk/broker protections untouched, Mission Control stayed read-only/non-critical this tranche — none of this work touched `src/agents`, `src/risk`, or execution/broker code. The Mission Control API is bound to `127.0.0.1` only; nothing is publicly exposed.

_Last refreshed: 2026-08-11 22:15 UTC — active project view only; retired/superseded work lives in Git history._
