# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🚦 RIGHT NOW

### 🟡 Discovery R1 + ChatGPT reconciliation complete — **waiting for my final acceptance**

Claude inspected the real repository and challenged the old Mission Control plan. ChatGPT then independently checked the branch/code and tightened the proposal.

**No implementation has started.**

🎯 outcome → 🔎 Claude discovery ✅ → 👤 product choice ✅ → 🏗️ ChatGPT reconciliation ✅ → 👤 **my accept/reject now** → 🛠️ Stage 3 build

> [!important] ⛔ Mission Control implementation is still not authorized until I accept the reconciled contract.

## 🗺️ PROJECT MAP

| Status | Stage | Plain-English outcome |
|---|---|---|
| ✅ DONE | 0 | Existing engine/integration audit |
| ✅ DONE | 0.5 | Actual-model attribution |
| ✅ DONE | 1 | Provider/model/correlation plumbing |
| ✅ DONE | 2 | Isolated read-only Mission Control API |
| ✅ DONE | Discovery R1 | Claude challenged the plan |
| ✅ DONE | Reconciliation R1 | ChatGPT independently checked/tightened the architecture |
| ⏸ HELD | 3 | Trading cockpit/dashboard |
| ⏸ HELD | 4 | Per-candidate specialist evidence + decision chain |
| ⏸ HELD | 5 | Journal/searchable forensic history |
| ❌ REMOVED | 6 | AgentLens |
| ⬜ LATER | 7–9 | Learning/write controls/paper-soak analytics only if later authorized |

## ✅ WHAT JUST HAPPENED

- Stage 2 remains accepted at **1530 passing tests, 0 failures**.
- Claude confirmed the existing API is a good read-only foundation and found the real Stage-4 data gap.
- I chose **per-candidate fidelity from the start** rather than a cheaper run-level-only view.
- ChatGPT agreed with the need for small additive evidence persistence, but corrected one important assumption: specialists do **not** all naturally emit the same per-symbol data. Tech/earnings are symbol-oriented; news mixes symbol and broader context; macro is run/sector-level. The UI/persistence must preserve those real scopes instead of inventing fake uniform agent cards.
- ChatGPT also caught that `/candidates` is a watchlist/expansion feed, not a complete record of every symbol considered in a run; Stage 3 must label it honestly.
- Old React/Tailwind/chart/donor choices are no longer frozen requirements. Claude chooses the smallest maintainable implementation when building.
- The old Journal/Search design will **not** be restored now. Git history preserves it; Stage 5 will recover only what is still useful when Stage 5 is actually authorized.
- The old one-shot Stage 3→4→5 tranche stays dead. There will be a real review after Stage 3 and after Stage 4.

## ⏭️ NEXT MOVES

### 1️⃣ Me — accept/reject reconciled contract
The technical reconciliation is finished in `docs/WORK.md` on `chatgpt/qamc-reconcile-r1`.

### 2️⃣ ChatGPT — if accepted
Merge the reconciled result and update `STATE.md`/`WORK.md` to authorize **Stage 3 only**.

### 3️⃣ Claude — fresh implementation session
Start from accepted `main` and run `/qamc-build`.

### 4️⃣ Review boundary
Claude pushes Stage 3 and stops. ChatGPT/operator review before Stage 4 begins.

## 🧠 WHO DECIDES WHAT

- 👤 **Me:** desired product, priorities, trade-offs, final acceptance.
- 🤖 **Claude Code:** repository investigation, engineering decisions, implementation/testing.
- 🏗️ **ChatGPT:** architecture challenge/reconciliation, independent review, GitHub integration.
- 🗃️ **GitHub:** durable shared memory/handoff.

## 🚧 BLOCKERS / DECISIONS NEEDED

**One:** accept or reject the reconciled implementation contract.

No technical architecture question is being pushed onto me.

## 🛡️ SAFETY

- 🧪 Alpaca Paper only.
- 🔒 Deterministic Python/broker protections remain final authority.
- 🖥️ Mission Control remains read-only and non-critical to trading.
- 🗝️ Secrets stay out of Git/client surfaces.
- 🧱 No unnecessary infrastructure.
- 🚫 Claude still cannot merge its own implementation or push directly to `main`.

_Last refreshed: 2026-08-09 — Discovery R1 independently reconciled by ChatGPT; awaiting operator acceptance._
