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

### ▶️ Alpaca Paper soak is ACTIVE — production-converged

Production has been reported and verified at `a6758f935910c5cf380cc6a7acedc5f3b78f6366`, including PR #69's explicit Alpaca IEX feed fix for the read-only intraday chart path.

Production verification reported:

- non-empty SPY/AAPL 5m, 15m and 1h bars;
- usable `5m Today`, `15m`, `1h`, and `1D` chart controls;
- `/health` healthy with `paper=true` and broker reachable;
- all seven existing paper timers preserved;
- Mission Control private/read-only;
- Telegram and OneCLI preserved;
- no broker order submitted, cancelled or modified by the read-side deployment.

The chart live/current-price truth issue was already fixed earlier by commit `796558f184f8dd800c7e1cbb57f11173ad3d6f6b`: live `/quotes` data is kept separate from historical bars and the chart renders explicit `LIVE` / `PREV CLOSE` lines.

The production checkout keeps one intended tracked local config delta: `config/settings.yaml` with `intraday_scan.enabled: true`.

---

## 🗺️ PROJECT MAP

| Status | Stage / milestone | Result |
|---|---|---|
| ✅ DONE | Trading engine / safety foundation | Decision chain, deterministic risk/execution and Paper-only broker boundary accepted. |
| ✅ DONE | Mission Control | Private browser/iPad cockpit, journal, evidence, search and decision-chain inspection. |
| ✅ DONE | VPS / OneCLI / private access | Runtime isolated under `qamc`; OneCLI and Tailscale paths accepted. |
| ✅ DONE | Trading-utility recovery | Mechanical opportunity→execution blockers corrected and deployed. |
| ✅ DONE | Session executions + intraday chart | Session fills, 5m/15m/1h/1D controls and IEX intraday data path deployed and verified. |
| ✅ ACTIVE | Natural Alpaca Paper validation | Prove the full opportunity→decision→execution→management→measurement chain in ordinary market conditions. |

## 👥 OPERATING MODEL

- **`ubuntu` — engineering/operator account.** Claude/Codex, Git/GitHub, dev tooling, private Vite/browser verification, tests, Docker/sudo tasks, and approved deployment orchestration.
- **`qamc` — runtime only.** Production checkout, runtime `.env`/OneCLI wiring, user services/timers, and QAMC Paper execution.
- **`dev` — parked.** Do not use it in the normal stabilization workflow or expand its permissions.

Engineering work may proceed autonomously under `ubuntu`, but merge and production deployment remain separate explicit gates.

## 📊 Mission Control

The accepted cockpit uses Tremor/TanStack for ordinary UI, Lightweight Charts for price/trade visualization, Dockview for the desktop support workspace, and custom visualization only where QAMC-specific decision topology justifies it.

No current dashboard defect is authorized merely from historical notes. Reopen UI work only from current operator evidence or current `STATE.md` / `WORK.md`.

## ⏭️ NEXT MOVES

1. Continue natural Alpaca Paper validation without forcing trades or weakening safety.
2. Separately authorize a production investigation of `src/execution/broker.py::get_latest_price` before any code change. The method still leaves Alpaca feed unspecified and silently returns `None` on failure; this is a trading-critical read path and is not yet proven to be failing in production.
3. Keep the lower-priority news-narrative factual drift and `actual_provider` attribution oddity parked unless real validation evidence shows they materially distort decisions or operator understanding.

## 🚧 CURRENT BLOCKERS

No architecture blocker is currently established.

The remaining work is primarily **natural trading validation**, plus the separately gated `get_latest_price` production investigation above. A lack of natural end-to-end evidence is not permission to manufacture trades.

_Last refreshed: 2026-08-24 — current project view only; retired detail lives in Git history._
