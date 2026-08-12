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

### 🔒 VPS security hardening complete

Baseline host security hardening is done and verified: UFW active (deny-incoming by default, SSH and Tailscale explicitly allowed), fail2ban protecting SSH from brute-force attempts, kernel updated via a completed reboot, Tailscale connectivity confirmed, `btop`/`iftop` installed for lightweight operator inspection. No subnet router or exit node was configured — out of scope for this pass. Detail in `ops/security/vps-hardening-plan.md`.

### 🔑 OneCLI credential gateway commissioned

OneCLI (the accepted credential-management product) is installed under Docker on the VPS, running private-only (`127.0.0.1`), with `dev` confirmed unable to reach Docker or `qamc`'s files directly. All four real credentials (OpenRouter, Alpaca Key ID + Secret, FRED) are stored in OneCLI and verified working end-to-end — QAMC never holds a real value, only placeholders. See `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`.

### 🟢 Runtime commissioning is live — external review pending

The runtime is wired to the gateway and working. `/health` reports `broker_reachable: true` (plus `db_reachable: true`, `paper: true`) — the objective signal that the whole credential chain is live, verified independently rather than taken on report.

In plain English: **the trading system can now actually talk to its broker, its AI provider and its economic-data source, and it does so without ever holding a real password itself.** It is not trading — the schedule timers are still switched off, deliberately.

Acceptance is checked by one reproducible command rather than a remembered sequence of manual steps (`ops/commissioning/verify_commissioning.py`). It runs on two accounts by design — a few checks are only meaningful from the runtime account, one is only meaningful from *outside* it — and each run says plainly what it still owes the other. Latest run from `dev`: **32 passed, 0 failed, 3 skipped** (all three skips are the runtime-account half).

This tranche is **awaiting external review**. It has not been self-accepted and has not been merged.

## 🗺️ PROJECT MAP

| Status | Stage / milestone | Result |
|---|---|---|
| ✅ DONE | 0–2 | Trading-engine audit, model/provider attribution/plumbing, isolated read-only Mission Control API. |
| ✅ DONE | Discovery/Reconciliation R1 | Post-Stage-2 architecture/data direction independently challenged and accepted. |
| ✅ DONE | 3 | Browser/iPad Trading Cockpit at `/ui`; accepted verification completed. |
| ✅ DONE | 4–5 | Specialist evidence + decision chain, journal + forensic search; accepted verification completed. PR #24 merged. |
| ✅ DONE | VPS deployment / hardening | OVH deployment completed. Runtime separated from development environment. |
| 🟨 EXTERNAL REVIEW PENDING | OneCLI commissioning | Runtime wired to the credential gateway; `broker_reachable: true`; reproducible acceptance tooling + evidence complete. Awaiting ChatGPT/operator review and merge. |
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

1. External review and merge of the commissioning branch (ChatGPT/operator).
2. Run the runtime-account half of the acceptance check so the evidence covers both accounts.
3. Decide separately whether to switch the trading timers on — commissioning does not imply that.
4. Install the remaining headless-browser OS libraries required for full screenshot verification.
5. Complete operator UAT of the deployed MVP.
6. Only after MVP acceptance, authorize dedicated visualization/UX polish.
7. Keep development work under `dev`; keep runtime under `qamc`.

## 🚧 BLOCKERS / DECISIONS NEEDED

Current blockers:
- The commissioning branch needs external review and merge; the runtime checkout picks up the acceptance tooling by `git pull` afterwards.
- Full browser screenshot verification requires the remaining OS package installation.

Resolved since the last refresh: real API secrets are now held in OneCLI and delivered to the runtime at the network layer, so the "supply secrets to the VPS" blocker is closed.

One decision is waiting, and it is deliberately **not** a Claude decision: whether the commissioning evidence justifies switching the trading timers on. No architecture decision is blocked.

_Last refreshed: 2026-08-12 (America/Toronto) — active project view only; retired/superseded work lives in Git history._
