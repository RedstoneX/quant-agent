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

### ✅ Commissioning complete

The final `qamc` runtime verifier passed on 2026-08-14:

- **37 PASS / 0 FAIL / 0 WARN / 1 expected SKIP**
- `COMMISSIONING ACCEPTANCE: PASS`
- `EXIT=0`

The one SKIP is intentionally proved from `dev`; that earlier `dev` run is already green. Cross-account commissioning is therefore complete.

Also verified green:

- OVH deployment/security/account isolation;
- OneCLI credential delivery;
- Alpaca **Paper** account, market data, quote and calendar;
- FRED;
- both accepted OpenRouter policy models;
- Mission Control DB, broker and paper-mode health;
- all seven trading timers disabled before activation;
- no committed secrets;
- Mission Control read-only.

### ▶️ NOW: start the Alpaca Paper soak

The operator already authorized paper trading once commissioning passed. That condition is satisfied.

**There is no remaining architecture, model, agent-intelligence or dashboard prerequisite.**

The immediate action is to enable the existing scheduled paper-trading timers and verify their schedule/health. Then QAMC should start collecting real paper-trading evidence.

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
| 🟨 NOW | Activate scheduled Alpaca Paper soak | Operator-authorized; enable existing timers and verify schedule/health. |
| ⬜ AFTER START | Observe and evaluate | Positions, reasoning, vetoes, fills, costs, missed opportunities and usability. |
| ⬜ ITERATE | Intelligence/code/dashboard improvements | Driven by soak evidence, not pre-soak speculation. |

## 📊 Mission Control

The current browser/iPad cockpit is sufficient for initial soak observation. It already shows account/position/order state, agent evidence, PM/RM decisions, model usage, journal/forensics and system/broker health.

Visual polish can proceed **after** paper trading starts.

## 🖥️ DEPLOYMENT

- OVH VPS: Ubuntu 24.04
- Runtime account: `qamc`
- Development account: `dev`
- Administration account: `ubuntu`
- QAMC and OneCLI remain private.

## ⏭️ NEXT MOVES

1. Enable the existing QAMC paper-trading timers under `qamc`.
2. Verify the next scheduled runs and that Alpaca remains Paper-only/healthy.
3. Observe the first real paper sessions and use that evidence to choose the next improvement tranche.

## 🚧 CURRENT BLOCKERS

**None.** Commissioning is accepted and the paper soak is authorized.

_Last refreshed: 2026-08-14 EDT (America/Toronto) — active project view only; retired detail lives in Git history._
