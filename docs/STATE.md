# QAMC Current State

Updated: 2026-08-14

This file says what is accepted and authorized **now**. Git history preserves prior detail.

## Accepted

- Stages 0, 0.5, 1, 2, 3, 4 and 5 are accepted.
- Mission Control/API and browser cockpit are deployed under `qamc`, private, read-only and non-critical to trading.
- The OVH account boundary remains: `ubuntu` = administration/recovery, `qamc` = runtime, `dev` = development/Claude Code.
- VPS baseline hardening and upstream OneCLI credential-gateway deployment are complete.
- QAMC can reach Alpaca Paper through the approved OneCLI path; runtime checks report `broker_reachable: true`, `db_reachable: true`, `paper: true`.
- Alpaca Paper-only is enforced in code.
- Cost-optimized model routing and the decision-chain audit are accepted on `main`.
- PR #33 fixed the commissioning timer-state false positive and merged to `main` as `aa52f5f9fd5912914a1640f74bdab84d1e30cd51`.
- The seven trading timers were directly inspected on the runtime account and are currently `disabled`; the prior verifier failure came from confusing systemd's PRESET column with the STATE column.

### Accepted model policy

- OpenRouter transport remains the model-provider path.
- Eight seats run `google/gemini-2.5-flash-lite`.
- `risk_manager` runs `qwen/qwen3-235b-a22b-2507`, held apart from the Portfolio Manager after a current-branch RM benchmark found equal measured seat quality and independence became the tie-breaker.
- Three seats (`earnings_analyst`, `evening_analyst`, `meta_reflector`) remain assigned by analogy rather than direct seat measurement; this is a known limitation, not hidden evidence.
- Projected LLM spend is approximately **$72.10 → $1.14/month** under the measured workload assumptions.
- Full contract and evidence: `docs/architecture/MODEL_ROUTING_POLICY.md`.

### Accepted decision-chain audit

- F6: the AI Risk Manager receives the position-age and drawdown evidence needed to audit the rules it already owned.
- F5: RM reads primary evidence before PM's narrative, PM's chain is explicitly treated as claims rather than evidence, the calibration loop is disclosed, and PM/RM use different models.
- F4: missing mandatory PM audit steps are observable through explicit rendering and a non-blocking advisory.
- F7b: price-derived valuation data is not routed into the cached earnings artifact; unsourced valuation claims are detected and logged instead.
- F8: inherited Apr–Jul 2026 behavioural priors retain provenance and lose precedence to this account's own evidence once available.
- **No deterministic risk or execution semantics changed.** Alpaca remains Paper-only.
- Full record: `docs/architecture/DECISION_CHAIN_AUDIT.md`.

## Current product priority: start the Alpaca Paper soak

The operator has explicitly re-oriented QAMC around one immediate outcome: **begin scheduled Alpaca Paper trading as soon as commissioning is green, observe what the system actually does, then improve agents, reasoning, code and Mission Control from real soak evidence.**

Therefore:

- no additional agent-intelligence, prompt, model-routing or dashboard-polish tranche is a prerequisite to paper trading;
- the existing basic Mission Control is sufficient for soak start because the accepted stages already provide account/position/order visibility, decision-chain evidence, model attribution, health and journal/forensics;
- post-start improvements should be prioritized by observed trading behaviour, failures, usability gaps and measured decision quality rather than by speculative pre-soak polish.

## Immediate remaining work

Only the final runtime acceptance rerun remains before activation.

The most recent `qamc` live verifier run produced **36 PASS / 1 FAIL / 1 SKIP**. Every functional, credential, broker, model and Mission Control check passed. The sole FAIL was the now-fixed timer parser. Direct systemd inspection confirmed all seven timers are disabled.

Next:

1. synchronize `/home/qamc/quant-agent` to current `main` including PR #33;
2. rerun `ops/commissioning/verify_commissioning.py --live` from the real `qamc` environment;
3. require exit `0` and zero FAIL results; the dev-only isolation SKIP / partial single-account coverage remains expected;
4. combine that green `qamc` evidence with the already-green `dev` half and treat commissioning as accepted;
5. because the operator has already authorized the Alpaca Paper soak contingent on green commissioning, enable the existing trading timers and verify the schedule/health without reopening architecture or asking for another product decision.

## ChatGPT GitHub integration role — reconstitution rule

When QAMC is being managed from ChatGPT, **ChatGPT owns GitHub review/integration and should use the connected GitHub plugin directly** for repository reads/writes, PR creation/review, merges, and routine GitHub administration whenever that connector supports the action.

Claude does not merge its own work. The operator should not be asked to perform routine GitHub housekeeping that ChatGPT can perform through the connector.

## Not authorized without a new contract

- Any live-broker trading.
- Any second model provider or silent fallback model.
- Promoting `qwen/qwen3-235b-a22b-2507` to another safety-relevant seat without evidence.
- Deterministic risk/execution semantic redesign.
- Broker-write Mission Control controls.
- Public exposure of QAMC or OneCLI.
- Collapsing `dev` / `qamc` / `ubuntu` account boundaries.
- Replacing upstream OneCLI or adding a new durable routing platform without a new architectural decision.

## Timer activation rule

Timer activation is **not a separate architecture or product-design problem**. The timers start QAMC's scheduled autonomous Alpaca Paper runs.

The operator authorization to begin the paper soak is now recorded. Activation should occur immediately after the final runtime verifier passes; no additional agent/dashboard polish or approval gate is required.

## Handoff

Claude Code works from `/home/dev/projects/quant-agent`. Runtime changes under `/home/qamc` must preserve account isolation.

Proceed through `docs/WORK.md` to green commissioning and paper-soak activation. After the soak begins, use observed evidence to drive the next improvement tranche.
