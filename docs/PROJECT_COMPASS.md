# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🚦 RIGHT NOW

### 🖥️ VPS deployment / hardening — infra deployed, stopped on two blockers

Stage 4–5 is accepted and PR #24 is merged into `main` as `105cc91a14faebd8a981061b3098eb181b306dda`.

The Mission Control API/UI is live and supervised on the VPS (`127.0.0.1:8800`, restarts on crash/reboot); the full test suite passes there (1558/0); the trading-schedule timers are installed but intentionally left off. Claude stopped and pushed branch `claude/vps-deployment-hardening-q3f7k2` because two things only the operator can supply are missing — see Blockers below.

Dedicated dashboard visualization/UX polish remains **after** deployed-MVP acceptance.

## 📌 WHAT JUST HAPPENED

- The planned "disposable cloud SSH key" bootstrap never happened — Anthropic's cloud sandbox couldn't reach outbound TCP/22. Claude Code connected instead through a Mac-hosted SSH session straight to the VPS; that was already the only key present, so there was nothing to revoke.
- Claude bootstrapped Python on the VPS without root, deployed and supervised the Mission Control API/UI under `systemd --user`, and ran the full test suite there (1558 passed).
- Trading timers are installed but not started — real API keys were never placed on the VPS, and starting them against placeholder keys would just burn retry budget for no verification value.
- Full desktop/iPad screenshot verification is blocked one `sudo apt-get install` away (headless Chromium is missing shared libraries); Claude verified `/health` and `/ui` over HTTP instead and did not fake the screenshot pass.

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

1. Operator places real secrets (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `FRED_API_KEY`, optionally `TELEGRAM_*`/`HEALTHCHECKS_URL`) into `/home/qamc/quant-agent/.env` on the VPS directly (scp/sftp/edit-over-SSH) — not through chat.
2. Operator runs one `sudo apt-get install` for headless-Chromium's missing shared libraries (exact package list in `docs/WORK.md`'s checkpoint section) so full desktop/iPad screenshot verification can complete.
3. Claude enables the trading timers, re-runs full runtime verification (real engine + real broker/LLM calls, live screenshots), refreshes this checkpoint, pushes.
4. ChatGPT independently reviews the pushed deployment result; operator UAT follows only after that review.
5. If UAT passes, accept the deployed MVP; only then authorize dedicated visual polish.

## 🚧 BLOCKERS / DECISIONS NEEDED

Two operator-only actions, neither a product/architecture decision:
1. Real API secrets need to reach the VPS `.env` file directly — Claude will not accept or type secrets through chat.
2. One interactive `sudo apt-get install` for headless-browser system libraries — Claude has no working sudo in this session (no cached auth, no NOPASSWD rule).

## 🛡️ SAFETY

Paper-only, deterministic risk/broker protections untouched, Mission Control stayed read-only/non-critical this tranche — none of this work touched `src/agents`, `src/risk`, or execution/broker code. The Mission Control API is bound to `127.0.0.1` only; nothing is publicly exposed.

_Last refreshed: 2026-08-10 01:15 UTC — active project view only; retired/superseded work lives in Git history._
