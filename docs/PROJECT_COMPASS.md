# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🎯 What QAMC is

**QAMC is an autonomous AI-assisted Alpaca paper-trading experiment that acts like a small virtual trading desk.**

Specialist AI agents analyze the market, a Portfolio Manager synthesizes the evidence, an AI Risk Manager challenges the plan, and deterministic Python safety rules decide what is actually allowed to execute.

The experiment asks:

> **Does inexpensive modern AI add measurable out-of-sample trading value beyond ordinary deterministic signals?**

QAMC can express bearish views through approved inverse ETFs (`SH`, `SDS`, `PSQ`, `SQQQ`). Direct stock shorting, options and margin are not currently part of QAMC.

Live-money trading is **not authorized**.

## 🚦 RIGHT NOW

### ▶️ Alpaca Paper soak is ACTIVE — engineering blockers cleared

Production has been reported and verified at `a6758f935910c5cf380cc6a7acedc5f3b78f6366`, including PR #69's IEX intraday chart fix.

Verified production state includes:

- non-empty SPY/AAPL 5m, 15m and 1h bars;
- usable `5m Today`, `15m`, `1h`, and `1D` chart controls;
- live/current chart-price truth already corrected;
- `/health` healthy with `paper=true` and broker reachable;
- all seven paper timers preserved;
- Mission Control private/read-only;
- Telegram and OneCLI preserved;
- `config/settings.yaml: intraday_scan.enabled: true` retained as the intended local production override.

The previously flagged `get_latest_price` feed concern is closed: Alpaca latest trade/quote requests default to the best feed available to the account. Current probes confirm IEX succeeds while explicitly requesting unsubscribed SIP fails as expected. No code change is justified without contrary production evidence.

---

## 🗺️ PROJECT MAP

| Status | Stage / milestone | Result |
|---|---|---|
| ✅ DONE | Trading engine / safety foundation | Decision chain, deterministic risk/execution and Paper-only broker boundary accepted. |
| ✅ DONE | Mission Control | Private browser/iPad cockpit, journal, evidence, search and decision-chain inspection. |
| ✅ DONE | VPS / OneCLI / private access | Runtime isolated under `qamc`; OneCLI and Tailscale paths accepted. |
| ✅ DONE | Trading-utility recovery | Known mechanical opportunity→execution blockers corrected and deployed. |
| ✅ DONE | Session executions + intraday chart | Session fills, 5m/15m/1h/1D controls and IEX intraday data path deployed and verified. |
| ✅ DONE | Workflow stabilization | `ubuntu` engineering/operator; `qamc` runtime-only; `dev` parked; unnecessary account friction removed from normal work. |
| ✅ ACTIVE | Natural Alpaca Paper validation | Prove the full opportunity→decision→execution→management→measurement chain in ordinary market conditions. |

## 👥 OPERATING MODEL

- **`ubuntu` — engineering/operator account.** Claude/Codex, Git/GitHub, dev tooling, private Vite/browser verification, tests, Docker/sudo tasks, and approved deployment orchestration.
- **`qamc` — runtime only.** Production checkout, runtime `.env`/OneCLI wiring, user services/timers, and QAMC Paper execution.
- **`dev` — parked.** Do not use it in the normal stabilization workflow or expand its permissions.

## 📊 Mission Control

The accepted cockpit uses Tremor/TanStack for ordinary UI, Lightweight Charts for price/trade visualization, Dockview for the desktop support workspace, and custom visualization only where QAMC-specific decision topology justifies it.

No dashboard defect or trading-critical feed defect should be reopened merely from historical notes. Require current evidence.

## ⏭️ NEXT MOVE

**Run the system naturally and judge the actual trading chain.**

We now need ordinary Alpaca Paper sessions to show:

**opportunity → evaluation → PM/Risk decision → deterministic eligibility/funding/execution → management/exit → measured result**.

Do not force trades to manufacture proof. A no-trade outcome is valid when the reason is specific and defensible.

The lower-priority news-narrative factual drift and `actual_provider` attribution oddity stay parked unless real evidence shows they materially distort decisions or operator understanding.

## 🚧 CURRENT BLOCKERS

**No active engineering or architecture blocker is currently established.**

The remaining finish-line work is empirical: natural Paper validation of the system we have built.

_Last refreshed: 2026-08-24 — current project view only; retired detail lives in Git history._
