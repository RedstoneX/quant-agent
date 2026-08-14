# QAMC Current State

Updated: 2026-08-14

This file says what is accepted and authorized **now**. Git history preserves prior detail.

## Accepted

- Stages 0, 0.5, 1, 2, 3, 4 and 5 are accepted.
- Mission Control/API and browser cockpit are deployed under `qamc`, private, read-only and non-critical to trading.
- The OVH account boundary remains: `ubuntu` = administration/recovery, `qamc` = runtime, `dev` = development/Claude Code.
- VPS baseline hardening and upstream OneCLI credential-gateway deployment are complete.
- QAMC can reach Alpaca Paper through the approved OneCLI path; `/health` reports `broker_reachable: true`, `db_reachable: true`, `paper: true`.
- Alpaca Paper-only is enforced in code.
- Trading timers remain disabled until runtime acceptance is complete and the operator authorizes the paper-soak start.
- PR #30 is externally reviewed and merged into `main` as `7b78f72ecfc900e166af2207a6f2a8473c277131`.

### Accepted model policy

- OpenRouter transport remains the model-provider path.
- The all-agent `openai/gpt-5.5` map was a commissioning baseline and is retired.
- Eight seats run `google/gemini-2.5-flash-lite`.
- `risk_manager` runs `qwen/qwen3-235b-a22b-2507`, held apart from the Portfolio Manager after a current-branch RM benchmark found equal measured seat quality and independence became the tie-breaker.
- Three seats (`earnings_analyst`, `evening_analyst`, `meta_reflector`) remain assigned by analogy rather than direct seat measurement; this is a known limitation, not hidden evidence.
- Cost telemetry now prices OpenRouter-routed `vendor/model` ids from OpenRouter rather than falling through to direct-provider LiteLLM pricing.
- Projected LLM spend is approximately **$72.10 → $1.14/month** under the measured workload assumptions.
- Full contract and evidence: `docs/architecture/MODEL_ROUTING_POLICY.md`.

### Accepted decision-chain audit

- F6: the AI Risk Manager receives the position-age and drawdown evidence needed to audit the rules it already owned.
- F5: RM reads primary evidence before PM's narrative, PM's chain is explicitly treated as claims rather than evidence, the calibration loop is disclosed, and PM/RM now use different models at measured-equal RM quality.
- F4: missing mandatory PM audit steps are made observable through explicit rendering and a non-blocking advisory; backward-compatible schemas are retained.
- F7b: price-derived valuation data is intentionally not routed into the cached earnings artifact; unsourced valuation claims are detected and logged instead.
- F8: inherited Apr–Jul 2026 behavioural priors are retained with provenance and lose precedence to this account's own evidence once available.
- **No deterministic risk or execution semantics changed.** No threshold moved, no deterministic gate was removed, and Alpaca remains Paper-only.
- Full record: `docs/architecture/DECISION_CHAIN_AUDIT.md`.

## Current remaining work

One privileged runtime acceptance step remains. It is operational, not an architecture or PR blocker.

The runtime checkout is now synchronized to accepted `main`. The first `qamc` acceptance attempt proved that Mission Control and the broker credential chain remained live, but the verifier was invoked with system Python, without the interactive shell exporting `.env`, and without the `systemd --user` bus variables that `sudo -u qamc -i` failed to populate. Those are invocation issues; the corrected command is canonical in `docs/WORK.md` and `ops/onecli/README.md` step 4e.

Full commissioning acceptance is explicitly **cross-account evidence**:

- the `dev` half is already green and proves the runtime credentials are unreadable off-account;
- the `qamc` half must exit `0` with zero FAIL results and verify startup config, real runtime wiring, provider preflight, and timer state.

A single-account verifier run may correctly report `ACCOUNT COVERAGE: partial`; that is not itself a failure. No single login can prove both sides of the account boundary. Acceptance is the union of the two green runs.

## ChatGPT GitHub integration role — reconstitution rule

When QAMC is being managed from ChatGPT, **ChatGPT owns GitHub review/integration and should use the connected GitHub plugin directly** for repository reads/writes, PR creation/review, merges, and routine GitHub administration whenever that connector supports the action.

Do **not** send routine GitHub work to the operator, Claude, Codex/Work mode, or another environment merely because a generic file/code handoff is offered. Use another path only when the GitHub connector genuinely lacks the required capability or the operator explicitly requests it.

Claude does not merge its own work. The operator should not be asked to perform routine GitHub housekeeping that ChatGPT can perform through the connector.

## Not authorized without a new contract

- Any live-broker trading.
- Any second model provider or silent fallback model.
- Promoting `qwen/qwen3-235b-a22b-2507` to another seat without seat-specific evidence where the decision is safety-relevant.
- Deterministic risk/execution semantic redesign.
- Broker-write Mission Control controls.
- Public exposure of QAMC or OneCLI.
- Collapsing `dev` / `qamc` / `ubuntu` account boundaries.
- Replacing upstream OneCLI or adding a new durable routing platform without a new architectural decision.

## Timer activation rule

Timer activation is **not a separate architecture or product-design problem**. The timers simply start QAMC's scheduled autonomous Alpaca Paper runs.

Keep them off until the remaining runtime acceptance passes. After that, enabling them requires the operator's explicit authorization to begin the paper soak, but no new architecture review.

## Handoff

Claude Code works from `/home/dev/projects/quant-agent`. Runtime changes under `/home/qamc` must preserve account isolation.

Proceed through `docs/WORK.md` until the verified finish line or a genuine operator-only boundary: required privilege, required secret entry, or a material architecture/product conflict.
