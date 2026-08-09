# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> This page is the fast, plain-English view of where QAMC stands. It is intentionally visual, concise, and easy to scan.
>
> **Machine authority remains:** `docs/STATE.md` + `docs/ROADMAP.md` + `docs/work/ACTIVE.md` + `docs/decisions/ACTIVE.md`.
> If this page ever disagrees with those files, those files win and this Compass must be refreshed.

<!--
CLAUDE MAINTENANCE CONTRACT — preserve this presentation style when updating:
- Write for a non-coder/operator with attention-fragmentation in mind.
- Use emojis as visual landmarks, not decoration on every sentence.
- Keep sections short, scannable and logically ordered.
- Prefer plain-English outcome/status over implementation detail.
- Keep the most important current information above the fold.
- Preserve these core sections: RIGHT NOW, PROJECT MAP, WHAT JUST HAPPENED, NEXT MOVES, BLOCKERS/DECISIONS, SAFETY.
- Use ✅ DONE, 🟡 NOW, ⏸ HELD, ⬜ LATER, ❌ REMOVED consistently.
- Update this projection whenever a meaningful discovery pass, accepted implementation slice, or checkpoint changes project state.
- Do not turn this into a technical handoff, changelog, or duplicate architecture document.
-->

## 🚦 RIGHT NOW

### 🟡 We are here: **Mission Control architecture discovery / challenge**

Claude Code is **not coding Stage 3–5 yet**.

It will first inspect the real repository and challenge the Mission Control plan against the outcome we actually want.

**Current flow:**

🎯 **Desired outcome**  
⬇️  
🔎 **Claude explores + challenges the plan**  
⬇️  
👤 **I answer only genuine product/preference questions — one at a time**  
⬇️  
🏗️ **Technical/architecture issues go through GitHub for ChatGPT review**  
⬇️  
✅ **We accept one outcome contract**  
⬇️  
🛠️ **Fresh Claude session implements it**

> [!important] ⛔ **No Mission Control implementation is currently authorized.**
> Discovery/reconciliation comes first.

---

## 🗺️ PROJECT MAP

| Status | Stage | Plain-English outcome |
|---|---|---|
| ✅ DONE | **Stage 0** | Audited the existing trading engine and integration seams |
| ✅ DONE | **Stage 0.5** | Fixed actual-model attribution so history records which model really answered |
| ✅ DONE | **Stage 1** | Added provider/model flexibility, attribution, costs and decision correlation |
| ✅ DONE | **Stage 2** | Added the isolated read-only Mission Control API |
| 🟡 NOW | **Discovery R1** | Let Claude independently challenge the Mission Control architecture before coding |
| ⏸ HELD | **Stage 3 candidate** | Native trading cockpit / dashboard |
| ⏸ HELD | **Stage 4 candidate** | AI decision interface, disagreement and decision-chain explanation |
| ⏸ HELD | **Stage 5 candidate** | Daily journal and searchable forensic history |
| ❌ REMOVED | **Stage 6** | AgentLens — no longer part of QAMC |
| ⬜ LATER | **Stage 7** | Learning Center / Meta Reflector UI if still useful |
| ⬜ LATER | **Stage 8** | Carefully governed operator write controls, if later authorized |
| ⬜ LATER | **Stage 9** | Long paper-trading soak + experiment analytics |

**Important:** Stage 3–5 capabilities are still the leading candidate outcome, but their architecture and sequencing are deliberately **not frozen** until Claude completes Discovery R1.

---

## ✅ WHAT JUST HAPPENED

- ✅ Stages **0 → 2 are accepted**.
- ✅ Stage 2 finished with **1530 passing tests, 0 failures**.
- ✅ GitHub was redesigned to use Claude Code’s native progressive-disclosure model instead of loading a large governance packet every session.
- ✅ Claude project auto-memory is disabled; **GitHub is durable shared memory**.
- ✅ Claude now has repo-controlled permissions, sandboxing and deterministic safety hooks.
- ✅ A new `/qamc-discover` workflow makes Claude an **architecture/engineering participant before implementation**, not merely a coder.
- ✅ `/qamc-build` is gated so Claude cannot skip discovery and start building from the old plan.

---

## ⏭️ NEXT MOVES

### 1️⃣ Claude — Discovery R1
Claude opens the current QAMC repo and runs `/qamc-discover`.

It should:
- inspect the actual code/data/API;
- challenge **KEEP / CHANGE / REMOVE / ADD**;
- resolve facts itself;
- make routine engineering judgments itself;
- ask me only true product/value questions, **one at a time**;
- put consequential technical/architecture questions into GitHub for ChatGPT.

### 2️⃣ ChatGPT — architecture reconciliation
ChatGPT reviews Claude’s actual GitHub discovery result and challenges/reconciles the technical architecture.

### 3️⃣ Me — product acceptance
I decide whether the resulting outcome/product direction is what I want.

### 4️⃣ Claude — implementation
Only after that result is accepted and merged, a **fresh Claude Code session** implements the accepted outcome contract.

---

## 🧠 WHO DECIDES WHAT

- 👤 **Me:** desired outcome, product experience, priorities, trade-offs, final acceptance.
- 🤖 **Claude Code:** repository discovery, engineering judgment, architecture proposals, implementation orchestration, testing.
- 🏗️ **ChatGPT:** architecture challenger/reconciliation, independent checkpoint review, GitHub governance/integration.
- 🗃️ **GitHub:** durable project memory and handoff between all three.

**I should not be asked to make routine technical decisions that Claude or ChatGPT can resolve.**

---

## 🚧 BLOCKERS / DECISIONS NEEDED

**None from the operator right now.**

The next unresolved questions should emerge from Claude’s Discovery R1 investigation rather than being invented in advance.

---

## 🛡️ SAFETY — STILL NON-NEGOTIABLE

- 🧪 **Alpaca Paper only.** No live-money trading authorized.
- 🔒 Deterministic Python risk/execution keeps final authority.
- 🖥️ Mission Control must remain non-critical to trading.
- 🚫 Current Mission Control work remains read-only; no broker write controls.
- 🗝️ Real secrets stay out of Git. Runtime credentials are supplied through environment configuration when needed.
- 🧱 No unnecessary distributed infrastructure.

---

## 📍 QUICK STATUS

**Completed:** Stages 0, 0.5, 1, 2  
**Now:** Discovery R1  
**Next external decision:** accept/reject Claude + ChatGPT reconciled Mission Control outcome contract  
**Implementation after that:** Mission Control outcome, architecture and sequence determined by discovery  
**Live trading:** ❌ Not authorized

_Last refreshed: 2026-08-09 — after the outcome-first Claude workflow redesign._
