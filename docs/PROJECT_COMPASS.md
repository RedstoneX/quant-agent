# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🚦 RIGHT NOW

### 🟢 Stage 4 done (internal checkpoint passed) — building Stage 5

Stage 4 (per-candidate specialist evidence + decision chain) is implemented, independently reviewed, one confirmed IMPORTANT finding fixed, tests green, and committed/pushed as an internal checkpoint. Claude is continuing straight into Stage 5 per the standing authorization — no return trip needed for a numbered stage finishing.

🎯 Stage 3 ✅ → 🧠 Stage 4 build ✅ → 🔎 internal review/fix ✅ → 📚 Stage 5 build (now) → 🔎 integrated review → 🛑 external ChatGPT/operator checkpoint

Claude stops early only for a genuine unresolved product decision, a material architecture/safety/scope conflict, or evidence that invalidates the accepted direction.

## 🗺️ PROJECT MAP

| Status | Stage | Plain-English outcome |
|---|---|---|
| ✅ DONE | 0 | Existing engine/integration audit |
| ✅ DONE | 0.5 | Actual-model attribution |
| ✅ DONE | 1 | Provider/model/correlation plumbing |
| ✅ DONE | 2 | Isolated read-only Mission Control API |
| ✅ DONE | Discovery/Reconciliation R1 | Repository challenge and accepted implementation direction |
| ✅ DONE | 3 | Browser/iPad Trading Cockpit |
| ✅ DONE (internal) | 4 | Per-candidate specialist evidence + decision chain |
| 🟡 NOW | 5 | Journal/searchable forensic history |
| ⬜ LATER | — | Learning/write controls and paper-soak analytics only if separately authorized |

## ✅ WHAT JUST HAPPENED

- Stage 4 shipped: a new additive, non-authoritative `specialist_evidence` table captures already-validated macro/news/tech/earnings/PM/AI-Risk output with natural (run vs symbol) scope; two new read-only endpoints (`/runs/{run_id}/candidates`, `/runs/{run_id}/candidates/{symbol}`) expose it; the cockpit gained its first drill-down UI — Runs → candidates → full evidence, PM→AI Risk→execution chain with a proposed-vs-executed delta, and a disagreement/consensus summary that never fabricates alignment (a reviewer-caught all-neutral-signals bug was fixed before acceptance).
- Independent review ran against the backend diff; the one IMPORTANT finding (consensus falsely reporting "aligned" when every signal was neutral) is fixed with regression tests. A second pass verified the UI visually (desktop/iPad/dark-mode screenshots) across full-evidence, partial-evidence, RM-modified, and rejected-decision candidates.
- Full backend suite: **1557 passed, 0 failed**.

## ⏭️ NEXT MOVES

1. 🤖 Claude builds Stage 5 (journal day-browsing + forensic search) backend and cockpit UI.
2. 🔎 Fresh independent review of the integrated Stage 4+5 result; fix anything verified.
3. 🖥️ Desktop/iPad runtime verification across populated/empty/error/degraded states.
4. 🛑 Push and stop once for ChatGPT/operator external review — the mandatory gate after the complete tranche.

## 🚧 BLOCKERS / DECISIONS NEEDED

**None from me right now.** The remaining read-only Mission Control tranche is authorized.

## 🛡️ SAFETY

- 🧪 Alpaca Paper only.
- 🔒 Deterministic Python/broker protections remain final authority.
- 🖥️ Mission Control remains read-only and non-critical to trading.
- 🗝️ Secrets stay out of Git/client surfaces.
- 🧱 No unnecessary infrastructure.
- 🚫 Claude cannot merge its own work, force-push, or push directly to `main`.

_Last refreshed: 2026-08-09 — active project view only; retired/superseded work lives in Git history._
