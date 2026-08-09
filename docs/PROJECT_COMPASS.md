# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🚦 RIGHT NOW

### 🟢 Stage 4–5 functional-foundation tranche built — finishing integrated verification, then stopping for external review

Stage 4 (per-candidate specialist evidence + decision chain) and Stage 5 (journal + forensic search) are both implemented — backend and cockpit UI — independently reviewed, tests green, committed and pushed. Claude is now running the final integrated verification pass required before the mandatory external checkpoint.

🎯 Stage 3 ✅ → 🧠 Stage 4 build ✅ → 🔎 internal review/fix ✅ → 📚 Stage 5 build ✅ → 🔎 integrated review (now) → 🛑 external ChatGPT/operator checkpoint

Claude stops early only for a genuine unresolved product decision, a material architecture/safety/scope conflict, or evidence that invalidates the accepted direction.

**Lifecycle principle:** build a solid functional foundation first. Cloud/ephemeral environments are staging only. The MVP becomes accepted only after VPS deployment, VPS verification, independent review and operator UAT. Dedicated dashboard polish comes after that gate. This Stage 4–5 tranche does **not** expand into VPS work or dedicated visual polish — those need a separate accepted contract after the Stage-5 external gate.

## 🗺️ PROJECT MAP

| Status | Stage / milestone | What it actually includes |
|---|---|---|
| ✅ DONE | 0 — Engine/integration audit | Verify the existing `quant-agent` trading engine, broker/risk boundaries, persistence, agent chain and safe seams for QAMC rather than redesigning the trading core. |
| ✅ DONE | 0.5 — Actual-model attribution | Capture enough telemetry to distinguish the model/provider requested from the model/provider that actually answered, including fallback evidence where available. |
| ✅ DONE | 1 — Provider/model/correlation plumbing | Centralize provider routing, harden retry/deadline/failover behavior, and correlate PM → AI Risk → trade evidence without creating a parallel attribution store. |
| ✅ DONE | 2 — Read-only Mission Control API | Separate-process GET-only FastAPI read adapter for health, account, positions, orders, trades, runs, decisions, agents, reflections and candidates; historical SQLite reads are read-only and Mission Control cannot trade. |
| ✅ DONE | Discovery/Reconciliation R1 | Challenge the post-Stage-2 plan against the real repository, then reconcile the accepted product/data direction before UI implementation. |
| ✅ DONE | 3 — Browser/iPad Trading Cockpit | Static HTML/CSS/JS UI mounted on the existing FastAPI process at `/ui`; account/equity/P&L sparkline, positions, orders, trades, health, watchlist/expansion candidates, Paper badge, responsive desktop/iPad layout, and honest empty/error/degraded states. |
| ✅ DONE (internal) | 4 — Specialist evidence + decision chain | Persist validated non-authoritative specialist evidence at each source's real scope; symbol-specific technical/earnings/news evidence plus broader macro/news context; consensus/disagreement that never fabricates alignment; PM proposal → AI Risk → deterministic gate → executed/rejected result with proposed-vs-executed deltas; model/provider/cost/latency/token/fallback evidence. |
| 🟡 NOW (finishing) | 5 — Journal + forensic search | Prior-day browsing/journal plus read-only search/filtering across historical trades, decisions, agents/models and relevant forensic context without raw-log reading or arbitrary SQL. Backend + UI built; integrated verification in progress before the external checkpoint. |
| ⬜ PLANNED — NOT YET AUTHORIZED | VPS cutover / deployment hardening | **Immediately after Stage 5 is accepted.** Move the stable trading engine + Mission Control API/UI bundle to the small Linux VPS/server runtime; configure private access, secrets/environment, persistent data paths, process supervision/restart behavior, logs/health checks and basic operational recovery. Because Stage 3 serves the static UI from FastAPI at `/ui`, this should be a deployment cutover rather than a frontend rewrite. |
| ⬜ PLANNED — NOT YET AUTHORIZED | Deployed-MVP verification + UAT | On the VPS, Claude performs automated/integration/runtime/browser QA across desktop/iPad and meaningful populated/empty/error/degraded states, fixes known functional/runtime defects, then a fresh independent review runs. **Only after those gates does the operator perform final UAT.** UAT decides whether the deployed system is genuinely usable; it should not be the primary bug-finding loop. |
| ⬜ AFTER MVP ACCEPTANCE | Mission Control visualization / UX polish | Dedicated operating-surface refinement only after the solid deployed MVP is accepted. Revisit TradingView-style charting, donor-dashboard ideas, trade/decision visualizations, navigation, density and desktop/iPad usability. Polish must not substitute for missing functionality, safety, observability or forensic completeness. |
| ⬜ LATER | Learning/write controls + paper-soak analytics | Only if separately authorized after the deployed MVP baseline is stable. Writable controls must remain bounded, validated and unable to bypass deterministic risk/execution authority. |

## ✅ WHAT JUST HAPPENED

- Stage 4 shipped: an additive, non-authoritative `specialist_evidence` table captures already-validated macro/news/tech/earnings/PM/AI-Risk output with natural (run vs symbol) scope; two new read-only endpoints (`/runs/{run_id}/candidates`, `/runs/{run_id}/candidates/{symbol}`) expose it; the cockpit gained its first drill-down UI — Runs → candidates → full evidence, PM→AI Risk→execution chain with a proposed-vs-executed delta, and a disagreement/consensus summary that never fabricates alignment (a reviewer-caught all-neutral-signals bug was fixed before acceptance, with regression tests).
- Stage 5 shipped: read-only journal (`/journal/dates`, `/journal/{date}`) and forensic search (`/search?q=`, parameterized-SQL-only) endpoints, plus a cockpit Journal panel (date picker, equity snapshot, evening reflection, that day's runs/trades/candidates) and Search panel, both reusing the Stage 4 drill-down modal rather than duplicating it.
- Two independent review passes ran: one on the Stage 4 backend diff (one IMPORTANT finding fixed), one visual/runtime pass per stage (desktop/iPad/dark-mode screenshots across full-evidence, partial-evidence, RM-modified, rejected-decision, and multi-day journal/search states) — all honest empty/degraded states confirmed, no fabricated data found.
- Full backend suite: **1557 passed, 0 failed**.
- The current Stage 4–5 tranche is about **functional completeness of the read-only Mission Control**, not hosting migration or dedicated visual polish. Cloud/ephemeral environments are explicitly **staging only**; the intended MVP runtime is the small Linux VPS/server with private access.

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

1. 🤖 Claude finishes integrated verification of the complete Stage 4–5 tranche (full suite, fresh independent review of the integrated result, resolve verified findings).
2. 🖥️ Final desktop/iPad runtime/visual verification across meaningful populated/empty/error/degraded states.
3. 🛑 Push and stop for ChatGPT/operator external review — the mandatory gate after the complete tranche.
4. 🖥️ If Stage 5 is accepted, authorize VPS cutover/deployment hardening next.
5. 🧪 Verify the deployed VPS system through Claude QA/browser testing + independent review, then operator UAT.
6. ✅ Declare the solid deployed MVP accepted only after UAT passes.
7. 🎨 Authorize dedicated Mission Control visualization/UX polish after that gate.

## 🚧 BLOCKERS / DECISIONS NEEDED

**None from me right now.** The remaining read-only Mission Control tranche is authorized. VPS deployment, deployed-MVP verification/UAT and dedicated visual polish are sequenced but not yet authorized.

## 🛡️ SAFETY

- 🧪 Alpaca Paper only.
- 🔒 Deterministic Python/broker protections remain final authority.
- 🖥️ Mission Control remains read-only and non-critical to trading.
- 🗝️ Secrets stay out of Git/client surfaces.
- 🧱 No unnecessary infrastructure.
- 🚫 Claude cannot merge its own work, force-push, or push directly to `main`.

_Last refreshed: 2026-08-09 — active project view only; retired/superseded work lives in Git history._
