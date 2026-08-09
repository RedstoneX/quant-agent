# QAMC Current Work

Status: **STAGES 4–5 COORDINATED IMPLEMENTATION AUTHORIZED**

## Goal

Complete the remaining read-only Mission Control outcome as one coordinated engineering tranche. Use `/qamc-build`.

Claude owns routine architecture, decomposition, implementation choices, workers, integration, testing and debugging inside this contract. **Do not stop merely because Stage 4 finishes.** Close Stage 4 as an internal checkpoint and continue into Stage 5 when its checks/review are green.

## Stage 4 — specialist evidence + decision understanding

### Required outcome

- Persist the already-validated specialist evidence needed for durable forensic display using the smallest additive, non-authoritative projection that preserves each source's natural scope.
- Expose that evidence through typed GET-only API responses.
- Per-candidate UI shows available symbol-specific technical/earnings/news evidence plus clearly labeled broader macro/news context where applicable.
- Show disagreement/consensus without pretending every agent emitted the same kind of signal.
- Follow the selected symbol through PM proposal → AI Risk response/modification → deterministic gate → executed/rejected result, including proposed-versus-executed delta.
- Show requested/actual model/provider, fallback, tokens/cost/latency where available.

### Boundaries

- Never reconstruct canonical structured evidence by parsing raw LLM blobs in the client.
- Do not assign `decision_id` to research-phase `agent_logs` or change the tested PM/Risk/trade correlation model.
- Do not invent per-symbol macro conclusions when the source is run/sector scoped.
- New persistence is observational/non-authoritative and cannot affect trading.

### Internal checkpoint

- Tests prove existing research `agent_logs.decision_id` semantics remain unchanged.
- Tests prove structured evidence is persisted from validated model output and read back for the correct run/natural scope.
- Verify the decision interface against a real historical or controlled run without requiring raw-log reading.
- Run appropriate full/targeted checks and fresh independent review; fix verified BLOCKER/IMPORTANT findings.
- Create a clean commit boundary, refresh the Compass, then **continue to Stage 5 without external approval**.

## Stage 5 — journal + forensic search

### Required outcome

- Useful prior-day browsing/journal over authoritative or rebuildable derived data.
- Search/filtering that lets the operator find historical trades, decisions, agents/models and relevant forensic context without reading raw logs.
- Keep journal/search read-only and non-authoritative; do not create a second trading-memory system.
- No endpoint accepts or generates arbitrary SQL.
- Exact journal sections, indexing/search technology and UI structure are engineering choices. Recover old ideas from Git history only when they still earn their place.

### Final acceptance

- Integrated Mission Control remains read-only, non-critical to trading and honest about missing/degraded data.
- Full backend suite remains green; run all applicable frontend/runtime checks.
- Perform final desktop/iPad runtime/visual verification across meaningful populated/empty/error/degraded states.
- Perform fresh independent review and resolve verified BLOCKER/IMPORTANT findings.
- Refresh `docs/PROJECT_COMPASS.md`, commit/push the complete tranche, and **STOP for ChatGPT/operator external review**.

## Hard boundaries for the whole tranche

- Alpaca **Paper only**.
- No deterministic trading/risk semantic changes.
- No broker-write Mission Control operations.
- No secrets or fake production trading state in client/UI surfaces.
- No unnecessary distributed infrastructure or broad canonical-schema redesign.
- Frontend stack, charting, search/index implementation and prior donor projects remain implementation choices, not requirements.

## Escalate early only when necessary

Stop before the final gate only if there is a genuine unresolved operator product/value trade-off, a material architecture/safety/scope conflict, or evidence that invalidates the accepted outcome contract. Routine engineering choices and ordinary implementation problems belong to Claude.
