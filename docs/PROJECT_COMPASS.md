# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🚦 RIGHT NOW

### 🟡 Stage 3 accepted — Stages 4–5 are one coordinated functional-foundation tranche

The Trading Cockpit is merged and accepted. Claude is authorized to continue through Stage 4 and Stage 5 without returning to me merely because a numbered stage finished.

🎯 Stage 3 ✅ → 🧠 Stage 4 build → 🔎 internal review/fix → 📚 Stage 5 build → 🔎 integrated review → 🛑 external ChatGPT/operator checkpoint

Claude stops early only for a genuine unresolved product decision, a material architecture/safety/scope conflict, or evidence that invalidates the accepted direction.

**Lifecycle principle:** build a solid functional foundation first. Cloud/ephemeral environments are staging only. The MVP becomes accepted only after VPS deployment, VPS verification, independent review and operator UAT. Dedicated dashboard polish comes after that gate.

## 🗺️ PROJECT MAP

| Status | Stage / milestone | What it actually includes |
|---|---|---|
| ✅ DONE | 0 — Engine/integration audit | Verify the existing `quant-agent` trading engine, broker/risk boundaries, persistence, agent chain and safe seams for QAMC rather than redesigning the trading core. |
| ✅ DONE | 0.5 — Actual-model attribution | Capture enough telemetry to distinguish the model/provider requested from the model/provider that actually answered, including fallback evidence where available. |
| ✅ DONE | 1 — Provider/model/correlation plumbing | Centralize provider routing, harden retry/deadline/failover behavior, and correlate PM → AI Risk → trade evidence without creating a parallel attribution store. |
| ✅ DONE | 2 — Read-only Mission Control API | Separate-process GET-only FastAPI read adapter for health, account, positions, orders, trades, runs, decisions, agents, reflections and candidates; historical SQLite reads are read-only and Mission Control cannot trade. |
| ✅ DONE | Discovery/Reconciliation R1 | Challenge the post-Stage-2 plan against the real repository, then reconcile the accepted product/data direction before UI implementation. |
| ✅ DONE | 3 — Browser/iPad Trading Cockpit | Static HTML/CSS/JS UI mounted on the existing FastAPI process at `/ui`; account/equity/P&L sparkline, positions, orders, trades, health, watchlist/expansion candidates, Paper badge, responsive desktop/iPad layout, and honest empty/error/degraded states. |
| 🟡 NOW | 4 — Specialist evidence + decision chain | Persist validated non-authoritative specialist evidence at each source's real scope; show symbol-specific technical/earnings/news evidence plus broader macro/news context; expose consensus/disagreement; follow PM proposal → AI Risk → deterministic gate → executed/rejected result; show proposed-vs-executed deltas and model/provider/cost/latency/token/fallback evidence where available. |
| 🟡 AUTHORIZED NEXT | 5 — Journal + forensic search | Prior-day browsing/journal plus read-only search/filtering across historical trades, decisions, agents/models and relevant forensic context without raw-log reading or arbitrary SQL. Ends with integrated backend/frontend/runtime review and the next external ChatGPT/operator checkpoint. |
| ⬜ PLANNED — NOT YET AUTHORIZED | VPS cutover / deployment hardening | **Immediately after Stage 5 is accepted.** Move the stable trading engine + Mission Control API/UI bundle to the small Linux VPS/server runtime; configure private access, secrets/environment, persistent data paths, process supervision/restart behavior, logs/health checks and basic operational recovery. Because Stage 3 serves the static UI from FastAPI at `/ui`, this should be a deployment cutover rather than a frontend rewrite. |
| ⬜ PLANNED — NOT YET AUTHORIZED | Deployed-MVP verification + UAT | On the VPS, Claude performs automated/integration/runtime/browser QA across desktop/iPad and meaningful populated/empty/error/degraded states, fixes known functional/runtime defects, then a fresh independent review runs. **Only after those gates does the operator perform final UAT.** UAT decides whether the deployed system is genuinely usable; it should not be the primary bug-finding loop. |
| ⬜ AFTER MVP ACCEPTANCE | Mission Control visualization / UX polish | Dedicated operating-surface refinement only after the solid deployed MVP is accepted. Revisit TradingView-style charting, donor-dashboard ideas, trade/decision visualizations, navigation, density and desktop/iPad usability. Polish must not substitute for missing functionality, safety, observability or forensic completeness. |
| ⬜ LATER | Learning/write controls + paper-soak analytics | Only if separately authorized after the deployed MVP baseline is stable. Writable controls must remain bounded, validated and unable to bypass deterministic risk/execution authority. |

## ✅ WHAT JUST HAPPENED

- Stage 3 is accepted at **1531 passing tests / 0 failures** plus desktop/iPad runtime review.
- The cockpit remains read-only and uses real Stage-2 API data for account/P&L, positions, orders, trades, health and the watchlist/expansion feed.
- Stage 4 preserves each specialist's real data scope while adding the accepted per-candidate forensic view.
- The current build contract treats Stage 4 as an internal checkpoint and continues directly into Stage 5 when green.
- The current Stage 4–5 tranche is about **functional completeness of the read-only Mission Control**, not hosting migration or dedicated visual polish.
- Cloud/ephemeral environments are explicitly **staging only**. The intended MVP runtime is the small Linux VPS/server with private access.
- The deployed-MVP acceptance path is: Stage 5 acceptance → VPS cutover → Claude QA/browser verification on VPS → independent review → operator UAT → **MVP ACCEPTED** → dedicated dashboard visualization/UX polish.

## 🖥️ MVP / VPS CUTOVER

**Planned placement:** directly after the Stage-5 external checkpoint is accepted and before dedicated visual polish.

What moves together:
- the existing `quant-agent` trading engine;
- the read-only Mission Control FastAPI process;
- the static cockpit served at `/ui`;
- SQLite/other persisted artifacts required by the engine and forensic read side.

What the deployment milestone should establish:
- small Linux VPS/server as the normal 24/7 runtime;
- private operator access rather than unnecessary public exposure;
- secrets and environment configuration kept outside Git/client surfaces;
- durable data locations, permissions and backup/recovery expectations;
- supervised services with restart-on-failure/reboot behavior;
- health/log visibility sufficient to diagnose engine/API failure independently;
- verification that Mission Control failure still has zero effect on trading or broker protections.

Then, before the MVP is accepted:
- Claude runs the full applicable automated suite on the deployed system;
- Claude exercises real browser workflows and visual states on desktop and iPad-sized viewports;
- Claude fixes known functional/runtime/browser defects before handoff;
- a fresh independent review challenges the deployed result;
- the operator performs final UAT for product usability and acceptance.

**Important:** VPS deployment, deployed-MVP verification/UAT and the later polish phase are planned roadmap markers, not current implementation authorization. `STATE.md` + `WORK.md` must explicitly authorize each next tranche after Stage 5 is accepted.

## 🎨 POLISH COMES AFTER SOLID MVP

The dashboard is the operator's daily workplace, so quality matters. But the order is deliberate:

**correct + complete + observable + deployed + tested + accepted first; polished second.**

TradingView-style charts, richer trade markers/indicators, donor-dashboard concepts and visual refinement belong in the dedicated post-MVP Mission Control polish phase unless a small visualization is genuinely required to make an earlier functional workflow understandable.

## ⏭️ NEXT MOVES

1. 🤖 Claude continues with `/qamc-build` from its current Stage 4–5 work.
2. 🧠 Build, test and independently review Stage 4 internally.
3. 📚 Continue directly into Stage 5 when Stage 4 is green.
4. 🛑 After integrated Stage-5 verification, push and stop once for ChatGPT/operator external review.
5. 🖥️ If Stage 5 is accepted, authorize VPS cutover/deployment hardening next.
6. 🧪 Verify the deployed VPS system through Claude QA/browser testing + independent review, then operator UAT.
7. ✅ Declare the solid deployed MVP accepted only after UAT passes.
8. 🎨 Authorize dedicated Mission Control visualization/UX polish after that gate.

## 🚧 BLOCKERS / DECISIONS NEEDED

**None from me right now.** The remaining read-only Mission Control tranche is authorized. VPS deployment, deployed-MVP verification/UAT and dedicated visual polish are sequenced but not yet authorized.

## 🛡️ SAFETY

- 🧪 Alpaca Paper only.
- 🔒 Deterministic Python/broker protections remain final authority.
- 🖥️ Mission Control remains read-only and non-critical to trading.
- 🗝️ Secrets stay out of Git/client surfaces.
- 🧱 No unnecessary infrastructure.
- 🚫 Claude cannot merge its own work, force-push, or push directly to `main`.

_Last refreshed: 2026-08-09 14:41 EDT (America/Toronto) — active project view only; retired/superseded work lives in Git history._
