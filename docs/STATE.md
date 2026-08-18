# QAMC Current State

Updated: 2026-08-18

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
- PR #37 recorded the verified private Tailscale/Orca access path and merged to `main` on 2026-08-18.

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

## Private operator access

The OVH host's private operator path was completed and verified on 2026-08-14:

- Tailscale is authenticated, enabled and healthy.
- The **tailnet DNS name** is `wallaby-bowfin.ts.net`.
- The OVH VPS **machine name** remains `redstone-vps`.
- Therefore the VPS's canonical MagicDNS FQDN is **`redstone-vps.wallaby-bowfin.ts.net`**. Use that FQDN for explicit SSH/HTTPS configuration; the shorter `redstone-vps` may also resolve on clients where MagicDNS search domains are active.
- `wallaby-bowfin.ts.net` by itself names the tailnet DNS namespace, not the VPS node, and must not be used as the SSH host.
- Tailscale Serve provides tailnet-only HTTPS to Mission Control while the API remains bound to `127.0.0.1:8800`; ports 443 and 8800 are not publicly reachable.
- Existing OpenSSH key authentication for `ubuntu` and `dev` was verified over the tailnet. Tailscale SSH remains disabled so the existing key-based OpenSSH path stays authoritative.
- Public port 22 remains temporarily available as the recovery path until the operator validates SSH from the other regular Tailscale clients; it must not be removed before that validation.
- Orca development tooling runs under `dev` from `/home/dev/projects`; host-administration work is isolated under `ubuntu` in `/home/ubuntu/orca-admin`; `qamc` remains runtime-only.
- Only `ubuntu` has sudo administration. `dev` and `qamc` have neither sudo nor Docker-group membership, and no foreign-owned files were found in the three account homes.
- OneCLI remains healthy and loopback-only on ports 10254/10255.

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

## Paper-soak findings now accepted as work-driving evidence

The first operator review of real soak behaviour and Mission Control on 2026-08-18 exposed issues that are now legitimate post-start work rather than speculative polish:

- **SGOV is deterministic cash parking, not a Portfolio Manager investment thesis.** The cash-sweep subsystem parks excess raw cash in SGOV, hides it from LLM-facing portfolio views, treats it as cash-equivalent for risk, and releases it before real BUYs. Mission Control currently presents SGOV like an ordinary position, which can materially mislead the operator about risk posture and deployable liquidity.
- **QAMC has an existing bearish path but is not currently a direct-short system.** `SH`, `SDS`, `PSQ` and `SQQQ` are already in the trading universe and deterministic risk code handles their signed inverse exposure and leverage. The Portfolio Manager cannot emit negative target weights; SELLs reduce/close owned positions. Direct stock shorting, options/theta strategies and margin are therefore outside the current implementation and remain unauthorized.
- **The product must not be structurally long-only.** Within the currently supported instrument set, QAMC is expected to consider and exploit credible bearish opportunities as well as bullish ones. This is now explicit in `docs/OUTCOME.md`. It does not require a trade every down day and does not authorize hindsight-driven chasing.
- **A possible long/caution bias requires evidence-based audit.** The PM and Evening prompts are intentionally swing/position oriented and still contain inherited Apr–Jul 2026 priors aimed at correcting predecessor-account under-investment and missed leaders. Those priors are provenance-tagged, but the running account has little history. The Aug 17–18 decline therefore warrants a forensic check of whether bearish/inverse-ETF evidence reached Tech → PM → RM correctly before changing intelligence.
- **Mission Control buries the useful explanation.** Existing API/UI code already records per-candidate specialist evidence, PM reasoning/targets, RM verdict/modifications, deterministic gate records and execution. The current top-level dashboard still makes the operator drill into run/candidate modals to answer the basic question “why did QAMC do nothing?” A 2026-08-18 morning run visibly considered candidates while producing no decision, making this an observed usability gap.
- **Missed-opportunity data exists but is not prominent enough.** The Evening Analyst can review notable UP or DOWN moves and the journal can render `missed_opportunities`; the operator should not need to infer from an empty trade table whether the system recognized a significant move.

## Current product priority: directionality + explainability during the live paper soak

The paper soak continues uninterrupted. The next authorized tranche is to use actual Aug 17–18 evidence to determine whether the system's lack of risk deployment was intentional, a candidate-generation/agent bias, a risk veto, or simply no qualified setup, while simultaneously fixing the dashboard presentation defects that make that distinction difficult.

Priority order:

1. forensic reconstruction of bearish opportunities through specialist → PM → RM → deterministic gate → execution;
2. make raw cash, SGOV sweep liquidity, real risk exposure and deployable liquidity visually distinct;
3. put a latest-run decision funnel / “why no trade?” explanation on the main Mission Control view;
4. surface directional posture, inverse-ETF consideration and missed opportunities using existing read-only evidence where practical;
5. change prompt/agent behaviour only if the forensic evidence demonstrates a real directional blind spot.

## ChatGPT GitHub integration role

ChatGPT owns GitHub review/integration and should use the connected GitHub plugin directly for supported repository reads/writes, PR creation/review, merges and routine administration. Claude does not merge its own work. The operator should not be asked to perform routine GitHub housekeeping that ChatGPT can perform.

## Not authorized without a new contract

- Any live-broker trading.
- Direct stock shorting, options trading/theta strategies, or enabling margin.
- Any second model provider or silent fallback model.
- Deterministic risk/execution semantic redesign.
- Broker-write Mission Control controls.
- Public exposure of QAMC or OneCLI.
- Collapsing `dev` / `qamc` / `ubuntu` account boundaries.
- Replacing upstream OneCLI or adding a new durable routing platform without a new architectural decision.

## Handoff

Paper soak remains active. Execute the authorized directionality/forensics + Mission Control explainability tranche in `docs/WORK.md`, preserving existing deterministic safety semantics and requiring evidence before any intelligence correction.
