# QAMC Acceptance Criteria

These are global acceptance invariants. Stage-specific outcomes and whether a gate is internal or external live in `docs/ROADMAP.md` and `docs/STATE.md`.

## Global criteria

- No regression to deterministic hard risk or broker protection.
- Alpaca remains unmistakably paper-only unless a future explicit live authorization supersedes this boundary.
- Relevant targeted tests/checks pass; the governed full suite passes at external checkpoint handoff.
- Trading remains independent of Mission Control/API/journal/search/UI availability.
- Mission Control cannot directly place, cancel, modify, or bypass broker orders during the current read-only phases.
- No mock/demo data masquerades as production state.
- No secrets are exposed through API responses, client bundles, logs, or committed configuration.
- Actual provider/model attribution remains analyzable for LLM invocations relevant to the experiment.
- New UI/search/journal persistence is derived and rebuildable unless explicitly accepted as canonical.
- No new infrastructure without a demonstrated and accepted need.
- Current implementation state/authorization is updated in `docs/STATE.md` only after external acceptance; do not duplicate live status across governance documents.

## Internal tranche gates

When `docs/STATE.md` authorizes a coordinated multi-stage tranche, internal gates are engineering quality boundaries rather than operator acceptance events.

At an internal gate:
- verify that stage's outcome against the actual implementation;
- run appropriate targeted tests/build/type checks;
- perform runtime/visual verification for UI work where tools allow;
- obtain fresh-context independent review;
- fix verified blockers/important findings;
- leave a clear commit boundary;
- continue only if the live authorization permits it.

Do not mark an internal gate as externally accepted and do not create new governance documents merely to narrate it.

## External checkpoint

At an external STOP:
- verify the complete authorized tranche/outcome;
- run the governed full backend suite and complete frontend checks that exist;
- re-verify paper-only, hard-risk, read-only API, secret, and trading-isolation boundaries;
- perform fresh independent review and resolve verified blockers/important findings;
- push an auditable branch and STOP without merging or starting unauthorized work.

ChatGPT/operator independently review the actual GitHub work before acceptance/merge.

## Engineering stop rule

Stop and escalate instead of silently widening scope when success would require deterministic trading/risk changes outside authorization, a write-capable Mission Control API, a broad safety-sensitive canonical-schema redesign, a new distributed service, or optional integration effort disproportionate to experimental value. Deferral is an acceptable outcome.
