# QAMC Current Work

Status: **DISCOVERY R1 RECONCILED — AWAITING OPERATOR ACCEPTANCE**

## Goal

Agree the smallest implementation contract that reaches `docs/OUTCOME.md` without reopening trading/risk semantics or rebuilding the documentation/governance stack.

No product implementation is authorized until this reconciled result is accepted and merged.

## Accepted discovery findings

- Stage 2's API is a sound read-only boundary: GET-only, typed responses, independent read-only SQLite access, isolated from trading.
- Research/specialist `agent_logs` are intentionally run-level calls with `decision_id = NULL`; `decision_id` begins at the Portfolio Manager and continues through AI Risk and trades. Do not change that semantic.
- The existing `/runs/{run_id}` surface can explain specialist activity at run/day level, but the operator chose **per-candidate fidelity from the start**.
- Existing validated specialist outputs have different natural scopes. Technical and earnings data are naturally per-symbol; news contains symbol-specific plus broader context; macro is naturally run/sector-level. The UI must preserve those scopes rather than inventing a fake universal per-symbol agent schema.
- `/candidates` is the evening/watchlist-expansion feed, not a complete record of every symbol considered in a trading run. It must be labeled honestly.

## Reconciliation — KEEP / CHANGE / REMOVE / ADD

### KEEP

- Stage 2 API architecture and typed-response discipline unchanged.
- Alpaca Paper only; `yebof/quant-agent` remains the authoritative engine; deterministic Python/broker protections remain final authority.
- Mission Control remains read-only, non-critical to trading, and safe to fail independently.
- The operator's product decision: true per-candidate specialist evidence is required rather than run-level-only fidelity.
- Small local persistence/SQLite posture; no distributed infrastructure without demonstrated need.

### CHANGE

- **Do not force every specialist into the same per-symbol fields.** Persist already-validated structured specialist evidence in an additive, non-authoritative projection while preserving each source model's real scope. Candidate views combine symbol-scoped evidence with clearly labeled run/sector context.
- Research evidence correlates by `run_id` plus its natural scope (for example symbol where applicable). `decision_id` remains reserved for PM → AI Risk → trade correlation. The UI must make that transition explicit rather than fabricating a trace id.
- Stage 3 acceptance is limited to the cockpit surfaces required by the outcome, not "every Stage-2 endpoint."
- Technology choices such as React/Vite/Tailwind, chart libraries, and old UI donors are **implementation candidates, not durable requirements**. Claude may inspect Git history or current options when useful and choose the smallest maintainable approach during build.
- Use a real external checkpoint after Stage 3 before adding the new Stage-4 evidence persistence/interface. Use another external checkpoint after Stage 4 before Stage 5.

### REMOVE

- Do **not** restore the pruned Journal/Search design file now. Git history already preserves it. When Stage 5 is actually authorized, recover only the requirements that still earn their place.
- Do not restore old donor/component maps, build-tranche documents, or other historical planning material into the current tree.
- Do not make "12 historical journal sections," FTS5, a particular frontend stack, or a particular chart library acceptance requirements before the stage that actually needs them.

### ADD

- A small additive evidence projection for validated specialist outputs, plus a typed GET-only API surface. It must not alter LLM decisions, deterministic risk, order eligibility, broker behavior, existing `agent_logs` call semantics, or `decision_id` semantics.
- Candidate/decision UI that can combine symbol-specific specialist evidence with run-level/sector context, then follow the selected symbol through PM → AI Risk → deterministic gate → executed/rejected outcome.

## Reconciled implementation contract

### Stage 3 — Trading cockpit

**Outcome**
- Polished browser/iPad cockpit using real Stage-2 data for account/equity/P&L, positions, orders, trades, health, and the existing watchlist-candidate feed.
- Clear Alpaca Paper identity.
- Honest empty/loading/error/degraded states.
- No production mock fallback.

**Boundaries**
- No new trading/risk semantics.
- No broker-write Mission Control operations.
- No requirement yet for per-candidate specialist drill-down.
- `/candidates` is presented according to its actual watchlist/expansion semantics, not mislabeled as the complete run candidate universe.

**Acceptance**
- Relevant cockpit data is API-backed and correctly labeled.
- Running visual verification at desktop and representative iPad viewport, including empty/loading/error/degraded states.
- Existing backend suite remains green; frontend build/lint/type checks used as applicable to the implementation Claude chooses.
- Push branch and **STOP for ChatGPT/operator review before Stage 4**.

### Stage 4 — Specialist evidence + decision understanding

**Outcome**
- Persist the already-validated specialist outputs needed for durable forensic display using the smallest additive non-authoritative projection that preserves native scope.
- Expose that evidence through typed GET-only API responses.
- Per-candidate Mission Control view shows available symbol-specific tech/earnings/news evidence and clearly labeled broader macro/news context where applicable.
- Show disagreement/consensus without pretending every agent emitted the same kind of signal.
- Show PM proposal → AI Risk response/modification → deterministic gate → executed/rejected result and proposed-versus-executed delta.
- Show requested/actual model/provider, fallback, tokens/cost/latency where available.

**Boundaries**
- Never parse raw LLM blobs in the client to reconstruct canonical structured evidence.
- Do not assign `decision_id` to research-phase `agent_logs` or otherwise change the tested PM/Risk/trade correlation model.
- Do not invent per-symbol macro conclusions when the source model is run/sector scoped.
- New persistence remains observational/non-authoritative and cannot affect trading.

**Acceptance**
- Tests prove existing research `agent_logs.decision_id` semantics remain unchanged.
- Tests prove structured evidence is persisted from validated model output and can be read back for the correct run/scope.
- Against a real historical or controlled run, one selected symbol can be followed from relevant specialist evidence into the PM/Risk/gate/trade chain without reading raw logs; scope transitions are visible and honest.
- Existing full suite remains green; visual verification covers the decision interface.
- Push branch and **STOP for ChatGPT/operator review before Stage 5**.

### Stage 5 — Journal and forensic search

Stage 5 remains a product capability, not a frozen historical design.

When Stage 4 is accepted, Claude should re-evaluate the current canonical data and recover from Git history only those prior journal/search ideas that still help. The implementation must remain a read-only/derived projection over authoritative data, support useful prior-day browsing/search, avoid arbitrary LLM-generated SQL, and avoid becoming a second trading-memory system.

Exact sections, indexing/search technology, and UI structure are decided at that stage rather than carried as dormant requirements now.

## Operator decision already recorded

- **Per-candidate fidelity from the start:** accepted as the product direction for Stage 4.

## Remaining decision

The operator only needs to accept or reject this reconciled contract. Technical reconciliation is complete.

## Handoff

If accepted, merge this reconciled result, update `STATE.md` to authorize **Stage 3 only**, and update this file to implementation status. Then start a fresh Claude Code session from accepted `main` and run `/qamc-build`.
