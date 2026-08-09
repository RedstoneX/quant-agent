# QAMC Acceptance Criteria

These are global acceptance invariants. Current phase/gate status lives in `docs/STATE.md`; product sequencing lives in `docs/ROADMAP.md`.

## Global criteria

- No regression to deterministic hard risk or broker protection.
- Alpaca remains unmistakably paper-only unless a future explicit live authorization supersedes this boundary.
- Trading remains independent of Mission Control/API/journal/search/UI availability.
- Mission Control cannot directly place, cancel, modify, or bypass broker orders during read-only phases.
- No mock/demo data masquerades as production state.
- No secrets are exposed through API responses, client bundles, logs, or committed configuration.
- Actual provider/model attribution remains analyzable for LLM invocations relevant to the experiment.
- New UI/search/journal persistence is derived and rebuildable unless explicitly accepted as canonical.
- No new infrastructure without a demonstrated and accepted need.
- Current state/authorization is updated in `docs/STATE.md`; do not duplicate live status across governance documents.

## Discovery / architecture-reconciliation gate

For substantial new work, discovery is complete only when:
- Claude inspected the actual repository rather than relying on the prior plan alone;
- `docs/work/ACTIVE.md` contains evidence-based KEEP / CHANGE / REMOVE / ADD findings;
- repository facts were investigated rather than pushed to the operator;
- routine engineering decisions were not needlessly escalated;
- any genuine operator product/value decisions were obtained one at a time and recorded;
- material architecture/safety/governance questions were reconciled through GitHub with ChatGPT;
- the resulting implementation contract states capabilities, constraints and verifiable acceptance conditions without prescribing file-by-file execution;
- no product implementation was performed during the discovery-only authorization.

Claude pushes the discovery result and stops. ChatGPT/operator independently review it. An accepted discovery/reconciliation result is merged before a fresh implementation session starts.

## Implementation gates

During implementation, relevant targeted checks run as work proceeds. The governed full suite runs at an external implementation checkpoint.

When `docs/STATE.md` authorizes internal gates, each internal gate should include:
- verification against the accepted outcome contract;
- appropriate targeted tests/build/type checks;
- runtime/visual verification for UI work where tools allow;
- fresh-context independent review;
- fixes for verified blockers/important findings;
- a clear commit boundary.

At an external implementation STOP:
- verify the complete authorized outcome;
- run the governed full backend suite and existing frontend checks;
- re-verify paper-only, hard-risk, read-only API, secret and trading-isolation boundaries;
- perform fresh independent review and resolve verified blockers/important findings;
- push an auditable branch and STOP without merging or starting unauthorized work.

ChatGPT/operator independently review the actual GitHub implementation before acceptance/merge.

## Engineering stop rule

Stop and escalate instead of silently widening scope when success would require deterministic trading/risk changes outside authorization, write-capable Mission Control operations, a broad safety-sensitive canonical-schema redesign, a new distributed service, or optional integration effort disproportionate to experimental value. Deferral is acceptable.
