# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🚦 RIGHT NOW

### 🟢 R1 approved — **Stage 3 Trading Cockpit is authorized**

Claude Discovery R1 ✅ → ChatGPT reconciliation ✅ → my approval ✅ → 🛠️ **Stage 3 build next**

Stage 3 is intentionally narrow: build the browser/iPad cockpit on the existing read-only Stage-2 API. **Stage 4 and Stage 5 are still blocked.**

## 🗺️ PROJECT MAP

| Status | Stage | Plain-English outcome |
|---|---|---|
| ✅ DONE | 0 | Existing engine/integration audit |
| ✅ DONE | 0.5 | Actual-model attribution |
| ✅ DONE | 1 | Provider/model/correlation plumbing |
| ✅ DONE | 2 | Isolated read-only Mission Control API |
| ✅ DONE | Discovery R1 | Claude challenged the post-Stage-2 plan |
| ✅ DONE | Reconciliation R1 | ChatGPT independently checked/tightened it; operator approved |
| 🟢 NOW | 3 | Trading cockpit/dashboard |
| ⏸ HELD | 4 | Per-candidate specialist evidence + decision chain |
| ⏸ HELD | 5 | Journal/searchable forensic history |
| ❌ REMOVED | 6 | AgentLens |
| ⬜ LATER | 7–9 | Learning/write controls/paper-soak analytics only if later authorized |

## ✅ WHAT JUST HAPPENED

- Stage 2 remains accepted at **1530 passing tests, 0 failures**.
- Discovery confirmed the Stage-2 API is a good read-only foundation.
- I chose **per-candidate fidelity from the start** for the later Stage-4 decision interface.
- ChatGPT corrected the data model so each specialist keeps its real scope instead of being forced into fake identical per-symbol cards.
- `/candidates` is explicitly treated as the watchlist/expansion feed, not every symbol considered during a run.
- Old frontend stacks, chart libraries, donor projects, and historical Journal/Search details are not frozen requirements.
- R1 is approved. `STATE.md` and `WORK.md` now authorize **Stage 3 only**.

## 🛠️ STAGE 3 — WHAT CLAUDE BUILDS NOW

- 📈 account/equity/P&L
- 📦 positions
- 🧾 orders + trades
- ❤️ system health
- 👀 watchlist/expansion candidates, honestly labeled
- 🧪 obvious Alpaca Paper identity
- 🖥️ polished desktop + iPad layout
- ⚠️ real empty/loading/error/degraded states
- 🚫 no production mock fallback

Claude chooses the smallest maintainable frontend implementation. The old React/Tailwind/chart/donor ideas are options, not mandates.

## ⏭️ NEXT MOVES

1. 🤖 **Fresh Claude Code session from `main`** → run `/qamc-build`.
2. 🛠️ Claude builds and verifies Stage 3 on a dedicated branch.
3. 🛑 Claude pushes and **STOPS**.
4. 🏗️ ChatGPT independently reviews the actual implementation.
5. 👤 I accept/reject before Stage 4 can begin.

## 🚧 BLOCKERS / DECISIONS NEEDED

**None right now.** Stage 3 has a complete accepted work contract.

## 🛡️ SAFETY

- 🧪 Alpaca Paper only.
- 🔒 Deterministic Python/broker protections remain final authority.
- 🖥️ Mission Control remains read-only and non-critical to trading.
- 🗝️ Secrets stay out of Git/client surfaces.
- 🧱 No unnecessary infrastructure.
- 🚫 Claude cannot merge its own implementation or push directly to `main`.

_Last refreshed: 2026-08-09 — R1 approved; Stage 3 authorized._
