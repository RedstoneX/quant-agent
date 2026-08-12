# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🤖 What I’m Building, in Plain English

### 🎯 The 20-second explanation

**QAMC is an autonomous AI-assisted Alpaca paper-trading experiment that acts like a small virtual trading desk.**

Specialist AI agents examine the market from different angles, a Portfolio Manager combines their evidence, an AI Risk Manager challenges the plan, and deterministic Python safety rules decide what is actually allowed to execute.

The experiment is trying to answer one question:

> **Does inexpensive modern AI add measurable out-of-sample trading value beyond ordinary deterministic signals?**

For now it trades **fake money only through Alpaca Paper Trading**.

### 🧠 How the intelligence works

- Specialist agents handle technical, news, macro, earnings and other focused analysis.
- The Portfolio Manager synthesizes their evidence into a proposed plan.
- The AI Risk Manager independently reviews that proposal.
- Deterministic Python risk/execution remains the final safety authority.
- The system records which model answered, its evidence, costs, decisions and eventual outcome so the process can be inspected rather than trusted blindly.

### 📊 Mission Control

The browser/iPad cockpit provides a read-only operating view of:

- account value, cash, positions, orders and trades;
- specialist-agent evidence and disagreement;
- Portfolio Manager and Risk Manager decisions;
- model/provider usage and cost;
- journal, search and decision forensics;
- system and broker health.

Mission Control is **not** allowed to become a second trading authority.

### 📔 Reflection and learning

QAMC records the trading story so prior decisions can be reconstructed and evaluated. Reflection may later suggest prompt/strategy improvements, but AI is not allowed to rewrite deterministic safety rules on its own.

### 🛡️ Safety

The AI gets to **think**; it does not get unrestricted trading authority.

Deterministic controls remain responsible for position size, cash, exposure, loss limits, stops and order eligibility. Failure should **fail closed rather than trade anyway**.

**Live-money trading is not authorized.**

---

## 🚦 RIGHT NOW

### ✅ VPS + security + credential commissioning complete

QAMC is deployed on the OVH Ubuntu VPS with separated accounts:

- `ubuntu` = privileged administration/recovery
- `qamc` = isolated runtime
- `dev` = development / Claude Code

Mission Control is private and read-only. VPS baseline hardening is complete. Upstream OneCLI is running privately and holds the real OpenRouter, Alpaca and FRED credentials; QAMC itself uses placeholders rather than storing the real values in project files.

The deployed runtime can reach **Alpaca Paper** through the approved credential path. `/health` reports `broker_reachable: true`, `db_reachable: true`, and `paper: true`.

PR #27 — the OneCLI/runtime commissioning tranche — was independently reviewed and merged into `main` on 2026-08-12.

### 🧠 Active work: cost-optimized AI routing

The temporary commissioning baseline routed all nine agents through OpenRouter to `openai/gpt-5.5`.

That is **not the intended final paper-trading model policy**.

Claude is now authorized to benchmark current low-cost/high-capability models — including Qwen and DeepSeek candidates — and build an explicit, auditable routing policy that uses inexpensive models for routine work and stronger models only where their added reasoning quality justifies the cost.

The goal is **decision quality per dollar**, not simply the cheapest tokens.

### ⏱️ What “trading timers” means

The trading timers are simply the **automatic schedule that starts QAMC’s trading sessions** without a person launching them manually.

They are currently **OFF**. That means the system can be tested and verified, but it will not automatically begin its scheduled paper-trading runs.

**Authority is explicit:**

- Claude may test the system but may **not** enable the timers.
- ChatGPT performs the technical readiness review.
- **The operator makes the final decision to activate automated paper trading.**

Timer activation is therefore a final operational go/no-go decision, not an AI architecture decision.

---

## 🗺️ PROJECT MAP

| Status | Stage / milestone | Result |
|---|---|---|
| ✅ DONE | 0–2 | Trading-engine audit, provider/model plumbing, isolated read-only Mission Control API. |
| ✅ DONE | Discovery/Reconciliation R1 | Architecture/data direction independently challenged and accepted. |
| ✅ DONE | 3 | Browser/iPad Trading Cockpit. |
| ✅ DONE | 4–5 | Specialist evidence, decision chain, journal and forensic search. |
| ✅ DONE | VPS deployment / hardening | Runtime deployed and separated from development. |
| ✅ DONE | OneCLI commissioning | Credential gateway integrated; Alpaca Paper connectivity verified; PR #27 merged. |
| 🟨 ACTIVE | Cost-optimized model routing | Benchmark and implement auditable Qwen/DeepSeek/strong-model routing through OpenRouter. |
| ⬜ NEXT GATE | Technical readiness review | ChatGPT independently reviews routing, commissioning and safety evidence. |
| ⬜ OPERATOR DECISION | Enable paper-trading timers | Operator decides whether autonomous scheduled paper trading may begin. |
| ⬜ AFTER MVP ACCEPTANCE | Mission Control visualization / UX polish | Richer charts and presentation without changing safety architecture. |
| ⬜ LATER | Learning/write controls + paper-soak analytics | Separate future authorization. |

## 🖥️ DEPLOYMENT

- OVH VPS: Ubuntu 24.04
- Runtime account: `qamc`
- Development account: `dev`
- Administration account: `ubuntu`
- Claude Code runs on demand under `dev`.
- QAMC and OneCLI remain private; no public service exposure is authorized.

## ⏭️ NEXT MOVES

1. Claude finishes the current cost-optimized model-routing work and verification.
2. Claude pushes the completed branch and stops at the external gate.
3. ChatGPT independently reviews the evidence and integrates accepted work.
4. If technically ready, the operator decides whether to switch on automated **paper-trading** timers.
5. After deployed MVP acceptance, proceed to dedicated Mission Control visualization/UX polish and paper-soak evaluation.

## 🚧 CURRENT BLOCKERS / DECISIONS

No operator blocker is currently active while Claude works on model routing.

The next meaningful human decision is **whether to activate automated Alpaca Paper trading after technical review**. Claude cannot make that decision.

_Last refreshed: 2026-08-12 15:41 EDT (America/Toronto) — active project view only; retired/superseded detail lives in Git history._
