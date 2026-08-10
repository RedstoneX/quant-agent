# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🤖 What I’m Building, in Plain English

> [!note] 👤 **Human explanation**
> This section is deliberately non-technical. It exists so I can keep the big picture in sight and explain the project to someone else. It is **not** a technical specification or an instruction to the trading system.

### 🎯 The 20-second explanation

**QAMC is an autonomous AI-assisted paper-trading system that acts like a small virtual trading desk.**

Instead of one AI making a guess, several specialist AI agents examine the market from different angles, a Portfolio Manager forms a plan, an AI Risk Manager challenges it, and then **hard deterministic safety rules** decide what is actually allowed to happen.

For now, it trades **fake money only through Alpaca Paper Trading** while we test whether the AI is genuinely useful.

### ✨ The important features

#### 🧠 A team of AI specialists

Different agents have different jobs, including technical, news, macro and fundamental analysis. The Portfolio Manager combines their evidence into a trading thesis and the AI Risk Manager independently challenges the proposed trade.

The point is not simply “ask an AI whether to buy.” It is a **structured decision process where agreement, disagreement, evidence and confidence can be inspected**.

#### 🪪 Agent Cards and decision evidence

Mission Control exposes what each specialist thought, its conviction, reasoning, supporting evidence and how that view influenced the final decision. The goal is to make the system explainable instead of presenting only a BUY / SELL / HOLD result.

#### 📊 Mission Control dashboard

The browser/iPad cockpit is the operating view for the system. It is intended to bring together:

- account value, cash, positions, orders and trades;
- specialist-agent evidence;
- Portfolio Manager and Risk Manager decisions;
- performance and system health;
- model/provider usage and cost;
- journal, search and decision forensics;
- later reflection/learning visibility.

Think of it as the **cockpit for the trading system**.

#### 📉 TradingView-style charts

After deployed-MVP acceptance, the dedicated visualization/UX phase can add richer TradingView-style charting so entries, exits, price action and decision context are easier to inspect visually.

#### 📔 Automatic trading journal

The journal is meant to reconstruct the story of each trading day:

- what the market looked like;
- what opportunities the agents found;
- where the agents agreed or disagreed;
- what the Portfolio Manager proposed;
- what Risk approved, rejected or reduced;
- what actually executed;
- how the trades turned out;
- what the system learned afterward.

The goal is to answer questions like **“Why did it make that trade three weeks ago?”** without relying on memory.

#### 🔍 Inspectable AI decision trail

QAMC records and surfaces enough evidence to trace a decision from specialist inputs through the Portfolio Manager, risk checks and execution outcome. The operator should be able to see which model answered, what evidence was used, what was proposed, what actually happened and what followed.

#### 🧪 The real experiment

This project is not being built merely because AI trading sounds interesting.

The central question is:

> **Does inexpensive modern AI improve trading results out-of-sample compared with ordinary deterministic signals?**

If the answer is no, the experiment should be capable of showing that too.

#### 🧠 Reflection and learning — controlled

The system can review trading results and reflect on what worked or failed. It may eventually propose improvements to prompts or approach, but it is deliberately **not allowed to rewrite hard deterministic safety rules on its own**.

#### 🤖 Largely autonomous operation

The intended operating loop is:

**watch → analyze → debate → decide → risk-check → paper trade → record → review → learn**

The system runs on the VPS so routine operation does not require keeping a desktop trading program alive all day. The operator should be able to inspect it from a browser or iPad.

### 🛡️ The important safety idea

The AI gets to **think**. It does **not** get unlimited authority to trade.

Deterministic Python rules remain the final gate for matters such as:

- position size;
- available cash;
- exposure and concentration;
- daily-loss limits;
- stops;
- whether an order is actually eligible to execute.

If the risk system fails, the design is to **fail closed rather than trade anyway**.

**Live-money trading is not authorized.**

### 🧩 What already existed vs. what QAMC added

The project did **not** start by building a trading engine from scratch. The open-source `quant-agent` foundation already supplied much of the machinery: scheduled autonomous operation, Alpaca paper execution, AI agents, risk controls, persistent records, reflection and longer-term learning.

QAMC has focused on making that engine more understandable, testable and operable — including correct model/provider attribution, Mission Control, specialist evidence, the decision chain, journal/search/forensics, browser/iPad operation and the VPS deployment architecture.

---

## 🚦 RIGHT NOW

### 🖥️ VPS deployment / hardening checkpoint complete

The accepted QAMC paper-trading engine + read-only Mission Control bundle has been deployed to the OVH VPS.

Current status:
- Mission Control API/UI deployed under supervised `systemd --user` service
- Private bind only (`127.0.0.1:8800`)
- Full deployed test suite verified: 1558 passed
- Trading timers installed but intentionally disabled
- Real secrets have not been placed on the VPS
- Independent review completed after branch push
- Claude Code installed under the neutral `dev` account as an on-demand development tool

Dedicated dashboard visualization/UX polish remains **after deployed-MVP acceptance**.

## 🗺️ PROJECT MAP

| Status | Stage / milestone | Result |
|---|---|---|
| ✅ DONE | 0–2 | Trading-engine audit, model/provider attribution/plumbing, isolated read-only Mission Control API. |
| ✅ DONE | Discovery/Reconciliation R1 | Post-Stage-2 architecture/data direction independently challenged and accepted. |
| ✅ DONE | 3 | Browser/iPad Trading Cockpit at `/ui`; accepted verification completed. |
| ✅ DONE | 4–5 | Specialist evidence + decision chain, journal + forensic search; accepted verification completed. PR #24 merged. |
| ✅ DONE | VPS deployment / hardening | OVH deployment completed. Runtime separated from development environment. |
| 🟨 NEXT GATE | Operator UAT | Validate deployed MVP usability before acceptance. |
| ⬜ AFTER MVP ACCEPTANCE | Mission Control visualization / UX polish | TradingView-style charting, richer visualizations/navigation/density and desktop/iPad refinement without changing safety architecture. |
| ⬜ LATER | Learning/write controls + paper-soak analytics | Separate future authorization only. |

## 🖥️ DEPLOYMENT TARGET

- OVH VPS: `vps-37b5f875.vps.ovh.us` / `135.148.120.105`
- Ubuntu 24.04
- Runtime account: `qamc`
- Development account: `dev`

Account separation:

- `ubuntu` = OVH administration / recovery
- `qamc` = QAMC runtime only
- `dev` = development, Claude Code, and future projects

Development environment:

```text
/home/dev/
    projects/
        quant-agent/
```

Claude Code runs on demand as `dev`. No persistent agent daemon is currently authorized.

## ✅ DEPLOYMENT ACCEPTANCE DIRECTION

The deployed system remains Alpaca Paper-only, preserves deterministic risk/broker protections, keeps Mission Control read-only and non-critical, keeps secrets outside Git/client surfaces, and separates runtime from development accounts.

## ⏭️ NEXT MOVES

1. Supply the required real API secrets directly to the runtime environment outside chat/Git.
2. Install the remaining headless-browser OS libraries required for full screenshot verification.
3. Complete deployed runtime/browser verification and operator UAT.
4. Only after MVP acceptance, authorize dedicated visualization/UX polish.
5. Keep development work under `dev`; keep runtime under `qamc`.

## 🚧 BLOCKERS / DECISIONS NEEDED

Current blockers:
- Real API secrets must be supplied directly to the VPS before real paper-trading verification.
- Full browser screenshot verification requires the remaining OS package installation.

No product or architecture decision is currently blocked.

_Last refreshed: 2026-08-09 22:28 EDT (America/Toronto) — active project view only; retired/superseded work lives in Git history._
