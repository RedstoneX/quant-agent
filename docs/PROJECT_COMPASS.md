# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🎯 What QAMC is

**QAMC is an autonomous AI-assisted Alpaca Paper trading experiment that acts like a small virtual trading desk.**

Specialists analyze the market, a Portfolio Manager synthesizes the evidence, AI Risk challenges the plan, and deterministic Python decides what is allowed to execute.

The experiment asks:

> **Does inexpensive modern AI add measurable out-of-sample trading value beyond ordinary deterministic signals?**

Bearish views are currently expressed through approved inverse ETFs (`SH`, `SDS`, `PSQ`, `SQQQ`). Direct stock shorting, options and margin remain outside QAMC.

Live-money trading is **not authorized**.

## 🚦 RIGHT NOW

Production is verified at `16c52715b3ee05ec9e38c12958a14ee77a6d38d7` with rollback SHA `a6758f935910c5cf380cc6a7acedc5f3b78f6366`.

Current priorities:

1. Finish any already-running pipeline-repair work and verify it in Paper.
2. Continue natural Paper validation of the full opportunity → decision → execution → management → measurement chain.
3. Then execute the authorized Research Intelligence Desk + Smart Money Analyst tranche from latest `main`.

## 👥 OPERATING MODEL

- **`ubuntu` — engineering/operator.** Codex/Claude, Git/GitHub, development tooling, tests, browser verification, Docker/sudo engineering work and deployment orchestration.
- **`qamc` — runtime only.** Production checkout, runtime `.env`/OneCLI, services/timers and QAMC Paper execution.
- **`dev` — parked.** Not part of normal work.

## ⚡ PAPER-BETA ENGINEERING MODE

While QAMC remains Alpaca Paper, authorized engineering is end-to-end autonomous:

**diagnose → implement → test → PR → merge → deploy → verify → rollback if needed**

There is no mandatory external code-review, merge or deployment gate in Paper beta. Git/PR history and the known-good production state provide traceability and rollback.

Parallel work/subagents are encouraged when they save time:
- strong reasoning models for architecture, trading logic, hard debugging, safety-sensitive changes and difficult UX/product judgment;
- cheaper/faster workers for tests, searches, logs, inventory and bounded evidence collection;
- no duplicate fan-out just to use more agents.

Live capital, paid dependencies, secrets/credential redesign and material new architecture still require explicit approval.

## 📊 Mission Control

The accepted cockpit uses Tremor/TanStack for ordinary UI, Lightweight Charts for price/trade visualization and Dockview for the desktop workspace. Custom visualization is justified only for QAMC-specific decision topology.

The next major product expansion is the Research Intelligence Desk: readable agent research, disagreement, PM/Risk deltas, Smart Money evidence, after-the-bell learning and a movable/persisted research workspace.

## 🔬 NATURAL PAPER VALIDATION

We still need ordinary Alpaca Paper sessions to demonstrate:

**opportunity → evaluation → PM/Risk decision → deterministic eligibility/funding/execution → management/exit → measured result**

Do not force trades to manufacture proof. A no-trade outcome is valid when the reason is specific and defensible.

## 🚧 CURRENT BLOCKERS

No standing external review/process blocker exists in Paper beta. Stop only for a genuine unresolved product/safety/architecture conflict, live-capital boundary, paid dependency, or external credential/authorization requirement.

_Last refreshed: 2026-08-25._
