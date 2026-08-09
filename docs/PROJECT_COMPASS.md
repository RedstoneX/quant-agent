# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🚦 RIGHT NOW

### 🟢 Stage 3 built and pushed — awaiting ChatGPT/operator review

Claude Discovery R1 ✅ → ChatGPT reconciliation ✅ → my approval ✅ → 🛠️ Stage 3 built ✅ → 🛑 pushed, STOPPED → 🏗️ **your/ChatGPT review next**

Stage 3 shipped as a static HTML/CSS/JS cockpit (no build step, no framework) mounted directly on the existing read-only Stage-2 API at `/ui`. Branch: `claude/qamc-build-stage3-cockpit`. **Stage 4 and Stage 5 remain blocked** until this is accepted.

## 🗺️ PROJECT MAP

| Status | Stage | Plain-English outcome |
|---|---|---|
| ✅ DONE | 0 | Existing engine/integration audit |
| ✅ DONE | 0.5 | Actual-model attribution |
| ✅ DONE | 1 | Provider/model/correlation plumbing |
| ✅ DONE | 2 | Isolated read-only Mission Control API |
| ✅ DONE | Discovery R1 | Claude challenged the post-Stage-2 plan |
| ✅ DONE | Reconciliation R1 | ChatGPT independently checked/tightened it; operator approved |
| 🟢 BUILT (pending review) | 3 | Trading cockpit/dashboard |
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
- R1 was approved; `STATE.md`/`WORK.md` authorized **Stage 3 only**.
- Claude chose the smallest maintainable build: a static HTML/CSS/JS cockpit (no Node/build step, no framework, no new chart library) mounted at `/ui` on the existing FastAPI process. Old React/Tailwind/chart/donor ideas were evaluated and skipped as unnecessary for this stage.
- Backend full suite still green (**1530 passed, 0 failed**) — no application/runtime behavior touched.
- Verified with real screenshots at desktop and iPad (portrait + landscape) viewports: populated data, empty state, broker-degraded state, and a hard backend-error state — every panel fails honestly, nothing is faked.

## 🛠️ STAGE 3 — WHAT WAS BUILT

- 📈 account/equity/P&L (with a small equity sparkline)
- 📦 positions
- 🧾 orders (open/closed/all) + trades
- ❤️ system health
- 👀 watchlist/expansion candidates, explicitly labeled as such (not "every symbol considered")
- 🧪 obvious Alpaca Paper/Live badge in the header
- 🖥️ responsive desktop + iPad layout
- ⚠️ real empty/loading/error/degraded states per panel — verified with screenshots, not assumed
- 🚫 no production mock fallback — every number comes from the real Stage-2 API or is shown as unavailable

## ⏭️ NEXT MOVES

1. ~~🤖 **Fresh Claude Code session from `main`** → run `/qamc-build`.~~ ✅ done
2. ~~🛠️ Claude builds and verifies Stage 3 on a dedicated branch.~~ ✅ done, branch `claude/qamc-build-stage3-cockpit`
3. ~~🛑 Claude pushes and **STOPS**.~~ ✅ done — this update
4. 🏗️ **ChatGPT independently reviews the actual implementation next.**
5. 👤 I accept/reject before Stage 4 can begin.

## 🚧 BLOCKERS / DECISIONS NEEDED

**None from me right now.** Stage 3 is built, verified, and waiting on ChatGPT/operator review of the pushed branch — not on any further decision from me.

## 🛡️ SAFETY

- 🧪 Alpaca Paper only.
- 🔒 Deterministic Python/broker protections remain final authority.
- 🖥️ Mission Control remains read-only and non-critical to trading.
- 🗝️ Secrets stay out of Git/client surfaces.
- 🧱 No unnecessary infrastructure.
- 🚫 Claude cannot merge its own implementation or push directly to `main`.

_Last refreshed: 2026-08-09 — Stage 3 cockpit built, verified and pushed; awaiting review._
