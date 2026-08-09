# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🚦 RIGHT NOW

### 🟡 Stage 3 accepted — Stages 4–5 now run as one coordinated tranche

Stage 3 Trading Cockpit is merged and accepted. The workflow has been corrected back to the intended model: **Claude does not stop after every numbered stage.**

🎯 Stage 3 ✅ → 🧠 Stage 4 build → 🔎 internal review/fix → 📚 Stage 5 build → 🔎 final review → 🛑 external ChatGPT/operator checkpoint

Claude may continue through Stage 4 into Stage 5 without returning to me merely because Stage 4 finished. It stops early only for a real product decision, material architecture/safety conflict, or evidence that invalidates the accepted direction.

## 🗺️ PROJECT MAP

| Status | Stage | Plain-English outcome |
|---|---|---|
| ✅ DONE | 0 | Existing engine/integration audit |
| ✅ DONE | 0.5 | Actual-model attribution |
| ✅ DONE | 1 | Provider/model/correlation plumbing |
| ✅ DONE | 2 | Isolated read-only Mission Control API |
| ✅ DONE | Discovery/Reconciliation R1 | Claude challenged the plan; ChatGPT tightened it; I approved |
| ✅ DONE | 3 | Browser/iPad Trading Cockpit |
| 🟡 NOW | 4 | Per-candidate specialist evidence + decision chain |
| 🟡 AUTHORIZED NEXT | 5 | Journal/searchable forensic history |
| ❌ REMOVED | 6 | AgentLens |
| ⬜ LATER | 7–9 | Learning/write controls/paper-soak analytics only if separately authorized |

## ✅ WHAT JUST HAPPENED

- Stage 3 is merged into `main` after Claude's build, independent reviewer PASS, ChatGPT code review, and **1531 passing tests / 0 failures**.
- The cockpit remains read-only and uses real Stage-2 API data for account/P&L, positions, orders, trades, health and the watchlist/expansion feed.
- The prior `Stage 3 only → STOP` authorization was a workflow regression. Earlier QAMC operating-model work had already established coordinated Mission Control implementation with internal stage gates.
- `STATE.md` and `WORK.md` now restore that model while preserving the Discovery R1 refinements for Stage 4.
- `/qamc-build` and `/qamc-checkpoint` now explicitly say that a numbered stage does **not** create an external approval gate by itself.
- Hard protections did not change: Paper only, read-only Mission Control, no self-merge/direct-main push, no secrets, deterministic risk/broker protections remain final authority.

## ⏭️ NEXT MOVES

1. 🤖 Claude continues with `/qamc-build` from current `main`.
2. 🧠 Stage 4 is implemented, tested and independently reviewed as an **internal** checkpoint.
3. 📚 If Stage 4 is green, Claude continues directly into Stage 5.
4. 🛑 After Stage 5 and integrated verification, Claude pushes and stops once for ChatGPT/operator external review.

## 🚧 BLOCKERS / DECISIONS NEEDED

**None from me right now.** The remaining read-only Mission Control tranche is authorized.

## 🛡️ SAFETY

- 🧪 Alpaca Paper only.
- 🔒 Deterministic Python/broker protections remain final authority.
- 🖥️ Mission Control remains read-only and non-critical to trading.
- 🗝️ Secrets stay out of Git/client surfaces.
- 🧱 No unnecessary infrastructure.
- 🚫 Claude still cannot merge its own work, force-push, or push directly to `main`.

_Last refreshed: 2026-08-09 — Stage 3 accepted; coordinated Stage 4–5 tranche restored._
