# QAMC Current State

Updated: 2026-08-14

This file says what is accepted and authorized **now**. Git history preserves prior detail.

## Accepted

- Stages 0, 0.5, 1, 2, 3, 4 and 5 are accepted.
- Mission Control/API and browser cockpit are deployed under `qamc`, private, read-only and non-critical to trading.
- The OVH account boundary remains: `ubuntu` = administration/recovery, `qamc` = runtime, `dev` = development/Claude Code.
- VPS baseline hardening and upstream OneCLI credential-gateway deployment are complete.
- QAMC reaches Alpaca Paper through the approved OneCLI path; runtime checks report `broker_reachable: true`, `db_reachable: true`, `paper: true`.
- Alpaca Paper-only is enforced in code.
- Cost-optimized model routing and the decision-chain audit are accepted on `main`.
- PR #33 fixed the commissioning timer-state false positive and merged to `main` as `aa52f5f9fd5912914a1640f74bdab84d1e30cd51`.
- The seven trading timers were directly inspected on the runtime account and confirmed disabled before activation.

### Runtime commissioning accepted

The final privileged `qamc` live verifier run against current `main` passed on 2026-08-14:

- **37 PASS / 0 FAIL / 0 WARN / 1 SKIP**
- `COMMISSIONING ACCEPTANCE: PASS`
- process exit `0`
- config/routing validation PASS
- OneCLI gateway/wiring/provider injection PASS
- Mission Control DB/paper/broker health PASS
- OpenRouter completions for both accepted policy models PASS
- Alpaca Paper account/data/quote/calendar PASS
- FRED PASS
- trading timers disabled PASS
- no committed secrets PASS
- Mission Control read-only PASS

The remaining `qamc` SKIP is the intentionally off-account isolation check. The already-green `dev` commissioning run proves that boundary. **Full commissioning acceptance is the union of those two green account runs and is now complete.**

## Accepted model policy

- OpenRouter remains the model-provider path.
- Eight seats run `google/gemini-2.5-flash-lite`.
- `risk_manager` runs `qwen/qwen3-235b-a22b-2507` for decision-chain independence at measured-equal RM quality.
- Three seats (`earnings_analyst`, `evening_analyst`, `meta_reflector`) remain assigned by analogy rather than direct seat measurement; this is a known limitation.
- Projected LLM spend is approximately **$72.10 → $1.14/month** under the measured workload assumptions.
- Full contract: `docs/architecture/MODEL_ROUTING_POLICY.md`.

## Accepted decision-chain audit

- Risk Manager receives position-age/drawdown evidence needed to audit its rules.
- RM reads primary evidence before PM narrative; PM claims are not treated as primary evidence.
- Missing PM audit steps are observable through explicit rendering and a non-blocking advisory.
- Unsourced valuation claims are detected/logged rather than contaminating cached filing evidence.
- Inherited Apr–Jul 2026 behavioural priors retain provenance and lose precedence to current-account evidence once available.
- **No deterministic risk or execution semantics changed.** Alpaca remains Paper-only.
- Full record: `docs/architecture/DECISION_CHAIN_AUDIT.md`.

## Current product priority: begin the Alpaca Paper soak

The operator explicitly authorized scheduled Alpaca Paper trading once commissioning passed. That condition is now satisfied.

Therefore:

- paper-soak activation is the immediate routine deployment action;
- no additional agent/prompt/model/dashboard work is a prerequisite;
- post-start improvements should be driven by actual positions, reasoning, vetoes, fills, costs, missed opportunities, performance and Mission Control usability.

## ChatGPT GitHub integration role

ChatGPT owns GitHub review/integration and should use the connected GitHub plugin directly for supported repository reads/writes, PR creation/review, merges and routine administration. Claude does not merge its own work. The operator should not be asked to perform routine GitHub housekeeping that ChatGPT can perform.

## Not authorized without a new contract

- Any live-broker trading.
- Any second model provider or silent fallback model.
- Deterministic risk/execution semantic redesign.
- Broker-write Mission Control controls.
- Public exposure of QAMC or OneCLI.
- Collapsing `dev` / `qamc` / `ubuntu` account boundaries.
- Replacing upstream OneCLI or adding a new durable routing platform without a new architectural decision.

## Handoff

Proceed through `docs/WORK.md` to paper-soak activation. After activation, use observed soak evidence to drive the next improvement tranche.
