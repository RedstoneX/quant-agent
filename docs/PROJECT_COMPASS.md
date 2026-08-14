# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🤖 What I’m Building, in Plain English

### 🎯 The 20-second explanation

**QAMC is an autonomous AI-assisted Alpaca paper-trading experiment that acts like a small virtual trading desk.**

Specialist AI agents examine the market from different angles, a Portfolio Manager combines their evidence, an AI Risk Manager challenges the plan, and deterministic Python safety rules decide what is actually allowed to execute.

The experiment is trying to answer:

> **Does inexpensive modern AI add measurable out-of-sample trading value beyond ordinary deterministic signals?**

For now it trades **fake money only through Alpaca Paper Trading**.

### 🧠 Intelligence + safety

- Specialist agents handle technical, news, macro, earnings and other focused analysis.
- The Portfolio Manager synthesizes their evidence into a proposed plan.
- The AI Risk Manager independently reviews that proposal.
- Deterministic Python risk/execution remains the final safety authority.
- QAMC records model/provider, evidence, costs, decisions and outcomes so the process can be inspected rather than trusted blindly.
- Live-money trading is **not authorized**.

### 📊 Mission Control

The browser/iPad cockpit is already at the minimum useful level for paper soak. It provides read-only visibility into account state, positions/orders/trades, agent evidence, PM/RM decisions, model usage, journal/forensics and system/broker health.

It does not need to be visually finished before paper trading starts.

---

## 🚦 RIGHT NOW

### 🟨 Final gate: one corrected runtime verifier rerun

The major engineering work is complete:

- stages 0–5 accepted and deployed;
- OVH runtime/security/account isolation complete;
- OneCLI credential delivery complete;
- Alpaca Paper, market data and FRED connectivity verified;
- Mission Control private/read-only and deployed;
- cost-optimized model routing accepted;
- decision-chain/agent audit accepted;
- all seven trading timers directly confirmed **disabled**.

The latest runtime verifier was **36 PASS / 1 FAIL / 1 SKIP**. The only failure was a verifier bug: systemd printed `disabled enabled` (STATE=disabled, PRESET=enabled), and the old parser searched the whole line for `enabled`.

PR #33 fixed that bug and is merged to `main` as `aa52f5f9fd5912914a1640f74bdab84d1e30cd51`.

**What remains before trading:** pull current `main` into the `qamc` runtime and rerun the live commissioning verifier. If it exits 0 with zero FAIL results, commissioning is complete by combining that runtime evidence with the already-green `dev` isolation evidence.

### ▶️ Paper-trading priority is now explicit

The operator has authorized the Alpaca Paper soak once commissioning passes.

That means:

- **do not wait for more agent/intelligence work;**
- **do not wait for more model benchmarking;**
- **do not wait for dashboard polish;**
- start collecting real paper-trading evidence first, then improve the system from what it actually does.

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
| ✅ DONE | Decision-chain audit | PM/RM evidence flow, auditability and inherited priors reviewed without changing deterministic safety semantics. |
| ✅ DONE | Timer-parser defect | PR #33 merged; false commissioning FAIL corrected. |
| 🟨 NOW | Final runtime acceptance rerun | One corrected `qamc` verifier run against current `main`. |
| ⬜ NEXT | Start scheduled Alpaca Paper soak | Already operator-authorized once the runtime gate is green. |
| ⬜ AFTER START | Observe and evaluate | Watch positions, reasoning, vetoes, fills, costs, missed opportunities and operator usability. |
| ⬜ ITERATE | Intelligence/code/dashboard improvements | Driven by soak evidence rather than pre-soak speculation. |

## 🖥️ DEPLOYMENT

- OVH VPS: Ubuntu 24.04
- Runtime account: `qamc`
- Development account: `dev`
- Administration account: `ubuntu`
- Claude Code runs on demand under `dev`.
- QAMC and OneCLI remain private; no public service exposure is authorized.

## ⏭️ NEXT MOVES

1. Run the corrected commissioning verifier once on the real `qamc` runtime.
2. If zero FAIL / exit 0, accept commissioning and begin the already-authorized Alpaca Paper soak.
3. Use the first real paper sessions to decide what agent, reasoning, code and dashboard work deserves attention next.

## 🚧 CURRENT BLOCKERS / DECISIONS

**No architecture, model-routing, agent-intelligence or dashboard blocker remains.**

The only current gate is the corrected runtime acceptance rerun. Paper-soak authorization is already recorded and does not need to be asked again after a green result.

_Last refreshed: 2026-08-14 EDT (America/Toronto) — active project view only; retired/superseded detail lives in Git history._
