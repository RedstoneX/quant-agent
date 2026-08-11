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

The Mission Control API/UI is live and supervised on the VPS (`127.0.0.1:8800`, restarts on crash/reboot); the full test suite passes there (1558/0); the trading-schedule timers are installed but intentionally left off. All 9 agents now route through OpenRouter. A custom credential-injecting proxy was built in an earlier slice as a substitute for blocked-on-Docker OneCLI, then reverted after a new architectural-authority rule ruled that kind of substitution out. No credential-delivery mechanism currently exists. Claude stopped and pushed branch `claude/vps-deployment-hardening-q3f7k2` because the path forward is an architectural fork needing an operator decision — see Blockers below.

Dedicated dashboard visualization/UX polish remains **after** deployed-MVP acceptance.

## 📌 WHAT JUST HAPPENED

- Screenshot blocker (below, previously open) is **resolved**: operator installed the missing Chromium shared libraries; full desktop/iPad screenshot verification completed against the live UI with real, non-fabricated evidence.
- All 9 agents migrated to route through OpenRouter instead of direct Anthropic/OpenAI (config-only change, verified against the live `resolve_provider()` logic).
- `CLAUDE.md` gained a HARD architectural-authority rule: a blocked approved product is a stop-and-ask fork, not license to build a substitute. Under that rule, the custom credential-proxy built in the prior slice was reverted in full.
- OneCLI's Docker+Compose+Postgres requirement was re-verified directly against upstream today (not trusted secondhand) and reproduced live on `dev`: still blocked, no Docker, no sudo. The reusable empirical findings (which env vars each HTTP stack needs) were kept — see `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`.
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

1. Operator decides how real OneCLI gets provisioned on the VPS (see Blockers below) — this is an architectural fork, not a routine engineering choice, per `CLAUDE.md`'s architectural-authority HARD RULE.
2. Once a path is approved and OneCLI is actually running, wire `qamc`'s `.env` to it and add real secrets — operator only, never through chat.
3. Claude re-verifies `/health` shows `broker_reachable:true`, confirms real OpenRouter/Alpaca/FRED calls succeed, then decides on enabling trading timers, refreshes this checkpoint, pushes.
4. ChatGPT independently reviews the pushed deployment result; operator UAT follows only after that review.
5. If UAT passes, accept the deployed MVP; only then authorize dedicated visual polish.

## 🚧 BLOCKERS / DECISIONS NEEDED

**Architectural fork — needs operator decision, not a routine engineering choice:**

A prior slice built a custom credential-injecting proxy after finding OneCLI's installer requires Docker. That custom proxy has been reverted — `CLAUDE.md` now has a HARD rule that a blocked approved product must stop-and-ask, not get replaced with an in-house substitute. Re-verified today, directly against upstream: OneCLI's install still unconditionally requires Docker + Docker Compose + PostgreSQL, and `dev` still has no Docker and no passwordless sudo. See `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md` for the evidence.

Real options, needing an operator decision:
1. Operator (or someone with sudo) installs Docker on the VPS, then Claude provisions real OneCLI through its normal install path.
2. OneCLI is pointed at an external managed Postgres instead of Docker's bundled one — itself a new infrastructure dependency needing approval.
3. Some other approach the operator prefers.

No credential-delivery mechanism currently exists. The four real secrets remain fully blocked until this is decided.

## 🛡️ SAFETY

Paper-only, deterministic risk/broker protections untouched, Mission Control stayed read-only/non-critical this tranche — none of this work touched `src/agents`, `src/risk`, or execution/broker code. The Mission Control API is bound to `127.0.0.1` only; nothing is publicly exposed.

_Last refreshed: 2026-08-11 22:15 UTC — active project view only; retired/superseded work lives in Git history._
