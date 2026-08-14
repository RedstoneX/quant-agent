# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🎯 What QAMC is

**QAMC is an autonomous AI-assisted Alpaca paper-trading experiment that acts like a small virtual trading desk.**

Specialist AI agents analyze the market, a Portfolio Manager synthesizes the evidence, an AI Risk Manager challenges the plan, and deterministic Python safety rules decide what is actually allowed to execute.

The experiment asks:

> **Does inexpensive modern AI add measurable out-of-sample trading value beyond ordinary deterministic signals?**

Live-money trading is **not authorized**.

## 🚦 RIGHT NOW

### ▶️ Alpaca Paper soak is ACTIVE

Commissioning is complete and accepted:

- **37 PASS / 0 FAIL / EXIT=0** on the final `qamc` runtime run;
- prior green `dev` run proves the complementary isolation boundary;
- OneCLI, Alpaca Paper, FRED, both policy models, Mission Control and safety checks are green.

On 2026-08-14 the operator enabled all seven existing QAMC user timers. Systemd confirmed all seven as `enabled`.

The six trading-stage timers run every 30 minutes and self-gate to their ET windows. Their first scheduled post-activation tick was **18:30 UTC / 2:30 PM ET** on Friday, 2026-08-14. The daily P&L export runs **Mon–Fri at 9:00 AM ET**.

**The project has crossed from pre-launch engineering into live paper-soak observation.**

No further agent/model/dashboard work is required before QAMC begins making scheduled paper-trading decisions.

---

## 🗺️ PROJECT MAP

| Status | Stage / milestone | Result |
|---|---|---|
| ✅ DONE | 0–2 | Trading-engine audit, provider/model plumbing, isolated read-only Mission Control API. |
| ✅ DONE | 3 | Browser/iPad Trading Cockpit. |
| ✅ DONE | 4–5 | Specialist evidence, decision chain, journal and forensic search. |
| ✅ DONE | VPS deployment / hardening | Runtime deployed and separated from development. |
| ✅ DONE | OneCLI commissioning | Private credential gateway; Alpaca Paper/OpenRouter/FRED path verified. |
| ✅ DONE | Model routing | 8 seats on Gemini 2.5 Flash Lite; Risk Manager on Qwen3 235B via OpenRouter. |
| ✅ DONE | Decision-chain audit | PM/RM evidence flow and auditability reviewed without changing deterministic safety semantics. |
| ✅ DONE | Runtime commissioning | 37 PASS / 0 FAIL / EXIT 0, combined with prior green dev isolation evidence. |
| ✅ ACTIVE | Scheduled Alpaca Paper soak | All seven timers enabled; autonomous paper schedule armed. |
| 🟨 NOW | Observe and evaluate | Positions, reasoning, vetoes, deterministic blocks, fills, costs, missed opportunities and usability. |
| ⬜ ITERATE | Intelligence/code/dashboard improvements | Driven by soak evidence, not pre-soak speculation. |

## 📊 Mission Control

The current browser/iPad cockpit is sufficient for initial soak observation. It already shows account/position/order state, agent evidence, PM/RM decisions, model usage, journal/forensics and system/broker health.

Visual polish can proceed after the running soak tells us what actually needs improvement.

## 🖥️ DEPLOYMENT

- OVH VPS: Ubuntu 24.04
- Runtime account: `qamc`
- Development account: `dev`
- Administration account: `ubuntu`
- QAMC and OneCLI remain private.

## ⏭️ NEXT MOVES

1. Observe the first scheduled paper sessions and confirm the expected decision/run records appear.
2. Review positions, rejected candidates, PM/RM reasoning, deterministic blocks, orders/fills and costs.
3. Use that evidence to select the next intelligence/code/dashboard improvement tranche.

## 🚧 CURRENT BLOCKERS

**None.** The paper soak is running. A session may legitimately produce no trade; use the recorded decision chain to distinguish intentional restraint from a defect.

_Last refreshed: 2026-08-14 EDT (America/Toronto) — active project view only; retired detail lives in Git history._
