# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> This is the fast, plain-English view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🚦 RIGHT NOW

### 🟢 Discovery R1 complete — awaiting ChatGPT/operator reconciliation

Claude Code finished the Discovery R1 challenge and pushed its findings to `docs/WORK.md`. **No implementation has started.** ChatGPT/operator now review the actual GitHub branch and accept/reject before any Claude implementation session begins.

🎯 outcome → 🔎 Claude discovery ✅ → 👤 genuine product decision obtained ✅ → 🏗️ ChatGPT architecture reconciliation ⏳ → ✅ accepted contract → 🛠️ fresh Claude implementation session

Headline finding: the Stage-2 API already answers "what specialists concluded and where they disagreed" at the **run/day level** for free, but a true **per-candidate** agent-disagreement view needs a small new additive read-model (same non-authoritative pattern as the existing `risk_gate` forensic row). I asked; you chose **per-candidate from the start**, so that read-model is now part of the proposed contract, sequenced before/alongside Stage 4.

> [!important] ⛔ No Mission Control implementation is currently authorized — that still requires your acceptance of the discovery result below.

## 🗺️ PROJECT MAP

| Status | Stage | Plain-English outcome |
|---|---|---|
| ✅ DONE | 0 | Existing engine/integration audit |
| ✅ DONE | 0.5 | Actual-model attribution |
| ✅ DONE | 1 | Provider/model/correlation plumbing |
| ✅ DONE | 2 | Isolated read-only Mission Control API |
| ✅ DONE | Discovery R1 | Challenged Mission Control direction; proposed contract in `docs/WORK.md`, pending ChatGPT/operator accept |
| ⏸ HELD | 3 candidate | Trading cockpit/dashboard capability |
| ⏸ HELD | 4 candidate | AI decision understanding/decision chain |
| ⏸ HELD | 5 candidate | Journal/searchable forensic history |
| ❌ REMOVED | 6 | AgentLens |
| ⬜ LATER | 7–9 | Learning/write controls/paper-soak analytics only if later authorized |

Stages 3–5 are candidate groupings, **not frozen architecture**. Discovery may simplify, regroup or remove them.

## ✅ WHAT JUST HAPPENED

- Stages 0–2 are accepted; Stage 2 finished at **1530 passing tests, 0 failures**.
- Discovery R1 inspected the real repo (API, pipeline, storage, git history of pruned donor/journal docs) rather than trusting the prior plan.
- Found: specialist-analyst calls (macro/tech/news/earnings) log **one batched row per run**, not per symbol/decision — `decision_id` is correctly `NULL` there by tested design. Run-level "what specialists concluded/disagreed" needs no new backend work; per-candidate drill-down does.
- Asked you one genuine product question; you chose **per-candidate attribution from the start**, so a small additive read-model (same non-authoritative pattern as the existing `risk_gate` forensic row) is now part of the proposed contract.
- Recommended dropping the old "Stage 3→4→5 with no intermediate STOP" tranche model in favor of a real checkpoint between Stage 3 and Stage 4.
- Recovered and endorsed the pruned Journal & Search design (Stage 5) as still sound; it needs to be re-added to `docs/architecture/`, not re-decided.
- Full evidence and the proposed contract are in `docs/WORK.md`.

## ⏭️ NEXT MOVES

1. ~~**Claude — Discovery R1:** run `/qamc-discover`, inspect the actual repo, challenge KEEP / CHANGE / REMOVE / ADD, and push/STOP.~~ ✅ done, this branch.
2. **ChatGPT — architecture reconciliation:** review the actual GitHub discovery branch and `docs/WORK.md`'s proposed contract.
3. **Me — final acceptance:** accept/reject the reconciled contract (the per-candidate product choice is already made).
4. **Claude — implementation:** only after accepted merge, start a fresh Claude session and run `/qamc-build`.

## 🧠 WHO DECIDES WHAT

- 👤 **Me:** outcome, product experience, priorities/trade-offs, final acceptance.
- 🤖 **Claude Code:** repository discovery, engineering/architecture judgment, implementation orchestration/testing.
- 🏗️ **ChatGPT:** architecture challenge/reconciliation, independent review, GitHub governance/integration.
- 🗃️ **GitHub:** durable shared memory/handoff.

## 🚧 BLOCKERS / DECISIONS NEEDED

**None from the operator right now** — your only pending decision (Stage 4 fidelity) is already recorded above; what's left is ChatGPT reconciliation and your final accept/reject.

## 🛡️ SAFETY

- 🧪 Alpaca Paper only; no live-money trading authorized.
- 🔒 Deterministic Python risk/execution is final authority.
- 🖥️ Mission Control remains non-critical and currently read-only.
- 🗝️ Secrets stay out of Git/client surfaces.
- 🧱 No unnecessary distributed infrastructure.

_Last refreshed: 2026-08-09 — Discovery R1 complete, pushed for reconciliation._
