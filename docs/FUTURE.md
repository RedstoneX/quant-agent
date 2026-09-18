# Future ideas

**Status: CONCEPTUAL / NOT AUTHORIZED.** Owner ideas parked for later. Nothing here is work, authorizes work, or describes something that exists. Nothing moves to WORK.md until the owner schedules it.

## 1. Options desk cloned from QAMC

*Recorded 2026-09-14.*

**A new desk, not a setting.** Plumbing is bounded; the trading logic is most of the work.

**Broker facts (Alpaca docs, fetched 2026-09-14)**
- Paper accounts have options enabled by default.
- Levels: 1 = covered calls / cash-secured puts; 2 = + buy calls and puts; 3 = + spreads.
- Orders: market, limit, stop, stop-limit. Stops single-leg only. Day or gtc only.
- Whole contracts only. No extended hours.
- In-the-money contracts auto-exercise at expiry. Assignment is visible by REST polling only.
- Data: free "indicative" feed (quotes modified, trades 15 min delayed); real OPRA feed is paid. Both return Greeks and implied volatility.
- Historical options data starts February 2024.

**Plumbing**
- Options data client (chains, snapshots, contract symbols). The code uses stock data clients only today.
- Orders in contracts: x100 multiplier, no fractional, no overnight session.
- Positions, P&L, journal: premium, expiry, exercise and assignment.
- 47 source files touch share quantity, fractional or stop logic (measured 2026-09-14).

**Hard part**
- Every agent, gate and grader reasons in stock price levels. Options add strike, expiry, implied volatility, time decay.
- Sizing and R/R change shape: a bought option's maximum loss is the premium, not the stop distance.
- Time decay works against the horizon; broker stops on option prices are unproven here.
- Rig and benchmark need options fixtures; history is short.
- Real quotes need paid OPRA — an owner decision.

**Cheapest first step (unratified)**
- Keep stock analysis unchanged. Add one layer: stock thesis -> buy a call or put (level 2).
- No spreads, no selling options, paper only, until that layer has its own evidence.

## 2. Live trading safety architecture

Applies only if QAMC earns real capital. Changes nothing about the paper-only boundary.

**Core principle.** The AI is an untrusted strategy generator inside a trusted control system. It may propose trades; it is never the final authority. Deterministic controls, an isolated executor, broker-side limits and an independent Sentinel must each be able to stop trading without the AI's cooperation.

```text
Research / AI zone
        | trade proposal
        v
Deterministic Risk Engine A
        | approved intent
        v
========== HARD SECURITY BOUNDARY ==========
Live Execution Governor
        +-- independent broker state read
        +-- Risk Engine B
        +-- duplicate-order protection
        +-- order-rate and exposure limits
        +-- circuit breakers / kill switch
        | live broker credential exists only here
        v
      Alpaca  <---  Independent Sentinel VPS
```

**Execution Governor.** One small service holds the live broker key — not agents, the PM, Mission Control or general tooling. It accepts only structured approved intents, re-reads broker state, revalidates right before submitting, and exposes a narrow interface, so a compromised AI or dashboard cannot send arbitrary orders.

**Two independent risk checks.** Engine A is today's risk layer. Engine B inside the governor re-checks the safety-critical subset: sizing, cash/leverage, gross and net exposure, concentration, loss limits, stale prices, liquidity/spread, duplicates, order frequency, state consistency. They must not share enough code for one bug to defeat both. Uncertainty fails closed for new exposure.

**Circuit-breaker states** (thresholds designed before live):

```text
GREEN   normal
YELLOW  degraded / reduced risk
ORANGE  exits only
RED     cancel pending entries; halted
BLACK   emergency flatten under predefined conditions
```

Inputs could include loss and drawdown, unexpected leverage or exposure, abnormal turnover, slippage, spread or liquidity deterioration, stale data, broker/local disagreement, rejected-order bursts, partial fills, protection failure, provider anomalies, heartbeat loss, clock integrity. An infrastructure blip must not liquidate a healthy protected book: losing the control plane is not the same as capital danger.

**Broker-side protection.** Protection that must survive a QAMC crash lives at the broker. A position needing a stop is never naked beyond a short, bounded window; failing to verify it escalates (exits-only, repair, or flatten by policy). Restrict the live account as far as the broker allows — no leverage, shorting, options or overnight exposure until each is validated. Paper and live credentials stay strictly separate; going live must never be a casual config toggle.

**Sentinel.** A tiny deterministic service on a separate VPS, ideally a different provider. Not a trading brain: no selection, optimisation or model calls. It monitors heartbeats, reads Alpaca directly, verifies protection, compares broker state to what QAMC claims, detects unexpected positions, orders, losses or dead control paths, alerts, and can take only these actions:

```text
OBSERVE  WARN  FREEZE_NEW_TRADES  CANCEL_OPEN_ENTRIES
EXITS_ONLY  RESTORE_PROTECTION  EMERGENCY_FLATTEN
```

- **Heartbeat:** QAMC sends a signed health message (version, trading state, expected positions and protections, risk state, last reconciliation). Heartbeat loss alone escalates by policy, never an automatic flatten: if the book is protected, hold and alert; loss plus missing protection justifies stronger action.
- **Access:** control traffic over a private network (Tailscale). Any emergency broker authority is as narrow as the broker allows, exposed only as the predefined actions.

**Hardened live host.** Separate from dev and research. Minimal: no AI coding environment, dev tooling, public dashboard, broad GitHub write keys or unrelated services. Default-deny networking, private admin access, key auth, MFA on infrastructure and broker, encrypted storage where it fits, controlled updates, reviewed deployments, runtime secret injection, durable audit logs.

**Mission Control** sits beside the execution path, never in it; its failure cannot touch protection. It shows Sentinel health, reconciliation, protection checks, governor health, breaker state and live/paper identity.

**Kill switch.** An unmistakable button, but the backend is the boundary, not the UI: block new exposure, cancel pending entries, exits-only, and flatten only when explicitly requested or required by policy. Broker-level suspension, where it exists, is an extra layer, not a replacement.

**Credential isolation.** Research and AI workers never hold live broker keys. The live Alpaca key stays in the governor, not behind a general credential proxy such as OneCLI.

**Change control.** Model, prompt, provider, strategy or risk changes never flow straight to meaningful capital. They pass replay, out-of-sample, paper/shadow, review and a small live canary. A candidate can shadow production for continuous comparison. Risk ceilings and emergency controls stay outside autonomous evolution.

**Graduated capital.** Paper success does not justify equal real capital. Start small; promote through explicit gates (observation period, trade count, reliability, realised behaviour, no open anomalies). Levels are set from the validated strategy at the time, not here. Capital allocation is itself a safety boundary.

**Before any live work is authorized**, a live-readiness review covering:
- evidence the strategy has earned live capital;
- broker capabilities at that time;
- threat model and credential design;
- governor and Sentinel specs, with failure modes;
- breaker thresholds and escalation matrix;
- broker/local reconciliation;
- stop-lifecycle, network, VPS, provider, split-brain and stale-state failure tests;
- deployment, change-control, incident and recovery procedures;
- capital promotion criteria;
- explicit owner approval.

Until then: **paper trading only; live trading is not authorized.**

## 3. Mission Control security panel

A read-only panel showing host and network security next to trading state, so posture is visible from the cockpit instead of over SSH. Background: `ops/security/vps-hardening-plan.md`.

**Could show:** SSH attack attempts (failure rate, source IPs), fail2ban bans, firewall events, active connections, listening ports and interfaces, service health, unusual resource or network activity.

**Non-goals**
- No Grafana, Prometheus, Loki or other monitoring stack. First evaluate a small read-only pull into the existing Mission Control API.
- Display only: no ban/unban, firewall edits or restarts from the UI.
- Not part of current work; dashboard work follows deployed-MVP acceptance (`docs/OUTCOME.md`, MVP lifecycle principle).
