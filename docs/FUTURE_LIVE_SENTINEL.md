# Live Trading Architecture

**Status: CONCEPTUAL / NOT AUTHORIZED FOR IMPLEMENTATION OR LIVE TRADING.**

This document records the intended security architecture if QAMC eventually demonstrates sufficient out-of-sample and paper-trading evidence to justify risking real capital. It does not authorize live trading, alter the current paper-only safety boundary, or add work to the current milestone roadmap. Activation requires explicit operator approval.

## Core principle

A live QAMC deployment must treat the strategy/AI system as an **untrusted strategy generator enclosed inside a trusted financial-control system**.

AI may propose trades. AI must never be the final authority over live capital. Deterministic controls, an isolated execution authority, broker-side constraints, and an independent Sentinel must be capable of rejecting or stopping trading without cooperation from the AI system.

```text
Research / AI zone
        |
        | trade proposal
        v
Deterministic Risk Engine A
        |
        | approved intent
        v
================ HARD SECURITY BOUNDARY ================
        |
        v
Live Execution Governor
        |
        +-- independent account/broker state read
        +-- deterministic Risk Engine B
        +-- idempotency / duplicate-order protection
        +-- order-rate and exposure limits
        +-- circuit breakers / kill switch
        |
        | live broker credential exists only here
        v
      Alpaca
        ^
        |
 Independent Sentinel VPS
```

## 1. Live Execution Governor

The live broker credential should belong to one deliberately small execution service, not to research agents, the Portfolio Manager, Mission Control, or general-purpose tooling.

The governor accepts only structured, previously approved trade intents. It independently obtains current broker/account state and revalidates the proposed action immediately before execution.

The governor should expose a narrow domain-specific interface rather than arbitrary broker API access. A compromised AI or dashboard therefore cannot manufacture unrestricted live broker requests.

## 2. Independent deterministic validation

Critical live controls should be intentionally redundant.

Risk Engine A remains the main QAMC deterministic risk layer. The Execution Governor contains a smaller independent Risk Engine B that validates the safety-critical subset again immediately before broker submission.

At minimum, live validation should cover position sizing, available cash/leverage, gross and net exposure, concentration, daily/rolling loss limits, stale prices, liquidity/spread constraints, duplicate orders, abnormal order frequency, and state consistency.

The two layers should not share so much implementation that one defect automatically defeats both.

Uncertainty in a safety-critical check must fail closed for new exposure.

## 3. Circuit-breaker states

Live operation should use explicit deterministic safety states rather than a single on/off flag. Exact thresholds are to be designed and validated before live authorization.

```text
GREEN   normal operation
YELLOW  degraded / reduced-risk operation
ORANGE  exits only; no new exposure
RED     cancel pending entries; trading halted
BLACK   emergency flatten under predefined conditions
```

Potential breaker inputs include daily realized/unrealized loss, rolling drawdown, unexpected leverage, concentration/exposure violations, abnormal turnover/order rate, excessive slippage, spread/liquidity deterioration, stale market data, broker/local-state disagreement, rejected-order bursts, repeated partial fills, protection failure, provider/model anomalies, heartbeat loss, and clock/time-integrity failure.

A transient infrastructure failure should not automatically liquidate a healthy protected portfolio. Escalation policy must distinguish loss of control-plane availability from actual capital danger.

## 4. Broker-side protection and constraints

Safety that can survive a QAMC crash should live at the broker where practical.

Protective orders should be broker-resident whenever the strategy requires them. A position that requires protection must not remain unintentionally naked beyond a tightly bounded transition window. Failure to verify required protection should trigger deterministic escalation such as exits-only, protection repair, or flattening according to predefined policy.

Live account capabilities should be constrained as aggressively as the broker supports. Initial live operation should avoid unnecessary leverage, shorting, options, overnight exposure, or other capabilities until explicitly validated and authorized.

Paper and live credentials/configuration must remain strongly separated. Live activation must not be reducible to an accidental casual configuration toggle.

## 5. Sentinel

A live deployment should include a separate, very small **Sentinel** service on an independent VPS, preferably using a different infrastructure provider and failure domain from the main QAMC host.

Sentinel is not a second trading engine and must never become a competing trading brain. It performs no stock selection, portfolio optimization, model inference, or discretionary trade reasoning.

Sentinel's responsibilities are deterministic:

- monitor heartbeats from the main QAMC host and Execution Governor;
- query Alpaca independently for actual account, position, order, and equity state;
- independently verify required broker-side protection;
- compare actual broker state with expected QAMC state;
- detect unexpected positions/orders, exposure, loss, or order activity;
- detect stale/dead main-system control paths;
- alert on anomalies;
- invoke a deliberately tiny set of predefined emergency actions when policy requires it.

Its permitted action vocabulary should remain narrow, conceptually:

```text
OBSERVE
WARN
FREEZE_NEW_TRADES
CANCEL_OPEN_ENTRIES
EXITS_ONLY
RESTORE_PROTECTION
EMERGENCY_FLATTEN
```

Sentinel should independently validate claims sent by QAMC rather than treating the main system as authoritative. For example, QAMC may report expected positions and protective orders, but Sentinel verifies them directly against Alpaca.

### Dead-man / heartbeat protocol

The main system should periodically provide Sentinel with a small signed or authenticated health/state message containing information such as deployment/version identity, trading state, expected positions/protections, risk state, and last successful broker reconciliation.

Heartbeat loss alone should trigger a deterministic escalation policy, not an unconditional flatten. If the main system disappears while all positions remain correctly protected and the broker account is otherwise safe, Sentinel may hold and alert. Heartbeat loss combined with missing protection or other dangerous state can justify stronger action.

### Sentinel connectivity and authority

Main-to-Sentinel administrative/control communication should use a private network such as Tailscale rather than a publicly exposed management interface.

If Sentinel requires emergency broker authority, its credential/capability should be as constrained as technically possible. Its software interface should expose predefined safety operations rather than general trading functionality.

## 6. Hardened live environment

Live execution should run separately from development/research infrastructure. The live host should be deliberately boring and minimal: no interactive AI coding environment, no unnecessary development tooling, no public dashboard endpoint, no broad GitHub write credentials, and no unrelated services.

Security posture should include default-deny networking, private administrative access, key-based authentication, MFA around infrastructure and broker accounts, encrypted storage where appropriate, controlled updates, reproducible/reviewed deployments, runtime secret injection, and durable audit logs.

Mission Control should sit beside the execution path, not become part of the authority chain. Dashboard failure must not compromise broker-side protection or deterministic safety.

Mission Control should prominently expose Sentinel health, broker reconciliation, protection verification, execution-governor health, current circuit-breaker state, and the live/paper environment identity.

## 7. Kill switch

Live Mission Control should eventually expose an unmistakable emergency control, but the UI itself is not the safety boundary.

The privileged backend control path should be capable of deterministic actions such as blocking new exposure, cancelling pending entry orders, switching to exits-only, and—only when explicitly requested or predefined emergency policy requires it—flattening positions.

Where broker-level trading suspension or equivalent controls exist and are appropriate, they should be considered an additional independent layer rather than a replacement for QAMC controls.

## 8. Credential isolation

Research/AI workers should not possess live broker credentials. External research/data/LLM credentials should also be isolated from agents where practical.

A credential gateway/vault technology such as OneCLI may be evaluated later for less-trusted agent/tool credentials. It is a candidate, not an architectural dependency. The Alpaca live credential should preferentially remain confined to the narrow Execution Governor rather than being broadly available through a generic credential proxy.

## 9. Production change control

Autonomous model, prompt, provider, strategy, or risk changes must not flow directly into a meaningful-capital live deployment.

Candidate changes should progress through evidence-producing stages such as historical/replay evaluation, out-of-sample testing, paper/shadow operation, review, and small live canary exposure before promotion.

A candidate strategy/model can shadow the production system without touching capital so that production and next-generation decisions can be compared continuously.

Protected deterministic risk ceilings and emergency controls remain outside autonomous evolution.

## 10. Graduated capital deployment

Successful paper trading does not justify immediately deploying equivalent real capital. Live deployment should begin with deliberately small capital and progress through explicit gates based on observation period, trade count, operational reliability, realized behavior, and absence of unresolved anomalies.

Exact capital levels and promotion criteria are intentionally not fixed in this conceptual document; they must be defined from the validated strategy and account conditions at the time live trading is considered.

Capital allocation itself is a safety boundary.

## 11. Preconditions for live authorization

Before any live implementation or activation is authorized, QAMC should require a dedicated live-readiness design/review covering at least:

- evidence that the strategy has earned consideration for live capital;
- broker API/account capabilities as they exist at that time;
- threat model and credential architecture;
- independent Execution Governor specification;
- independent Sentinel specification and failure-mode analysis;
- deterministic circuit-breaker thresholds and escalation matrix;
- broker/local-state reconciliation behavior;
- stop/protection lifecycle failure testing;
- network/VPS/provider failure testing;
- split-brain and stale-state testing;
- deployment/change-control procedure;
- incident response and recovery procedure;
- staged-capital promotion criteria;
- explicit operator approval.

Until that review occurs, the existing QAMC safety boundary remains unchanged: **paper trading only; live trading is not authorized.**
