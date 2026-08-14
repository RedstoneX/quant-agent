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
- trading timers disabled PASS before activation
- no committed secrets PASS
- Mission Control read-only PASS

The remaining `qamc` SKIP is the intentionally off-account isolation check. The already-green `dev` commissioning run proves that boundary. **Full commissioning acceptance is the union of those two green account runs and is complete.**

### Alpaca Paper soak activated

On 2026-08-14 the operator activated all seven existing `qamc` user timers after commissioning acceptance:

- `quant-agent-morning.timer`
- `quant-agent-midday.timer`
- `quant-agent-intra_check.timer`
- `quant-agent-close.timer`
- `quant-agent-evening.timer`
- `quant-agent-earnings_preprocess.timer`
- `quant-agent-daily.timer`

Systemd reported all seven as `enabled`. The six trading-stage timers are scheduled every 30 minutes and self-gate to their intended ET windows. Their first scheduled post-activation tick was **2026-08-14 18:30 UTC / 14:30 ET**. The daily P&L CSV export is scheduled for **Mon–Fri 09:00 America/New_York**.

This marks the start of the authorized **Alpaca Paper soak**. It does **not** authorize live-money trading.

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

## Current product priority: observe the paper soak

The immediate product goal is now to observe what QAMC actually does under scheduled Alpaca Paper operation and use real evidence to drive the next tranche.

Prioritize:

- positions selected and rejected;
- specialist disagreement and evidence quality;
- PM/RM reasoning and vetoes;
- deterministic blocks;
- order/fill behaviour;
- cost/latency/model attribution;
- missed opportunities and performance;
- Mission Control usability during real sessions.

No additional agent/prompt/model/dashboard polish is a prerequisite to the running soak.

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

Paper soak is active. Use observed soak evidence to choose and scope the next improvement tranche.