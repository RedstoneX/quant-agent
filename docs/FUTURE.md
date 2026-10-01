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

**The outward snapshot — the one seam that serves everything.** QAMC publishes a signed state snapshot OUTWARD on a schedule: heartbeat and version, trading state, positions it believes it holds, the protection it believes covers each, risk state, last reconciliation, recent trades, and cost spent. It is published to a drop point; nothing reads back into QAMC. No shared database, no inbound port on the money host, no QAMC credential held anywhere else. The Sentinel consumes the whole snapshot; the owner-facing dashboard consumes a subset. One artefact, two readers, neither able to reach in. (Owner, 2026-10-01.)

**The only inbound path is a flag, never a call.** The Sentinel's authority must not route through QAMC, because a hung desk cannot answer a request. Actions that need no cooperation — cancel resting entries, restore a missing stop, flatten — the Sentinel performs DIRECTLY at the broker with its own narrowly scoped credentials. Actions that only QAMC can take — freeze new trades, exits-only — are expressed as a flag QAMC READS before it acts. A hung process cannot serve an API, but neither can it ignore a flag on the path to placing an order.

**Erratic-behaviour breaker.** Beyond protection and reconciliation, the Sentinel watches RATE and SPEND, because a compromised or malfunctioning desk looks like a healthy one placing far too many orders: trades per hour, orders per minute, cost per day, and unexpected symbols or sides. Owner 2026-10-01: "it could get breached or hacked and start buying or selling everything — it should be watching the desk for any erratic actions and shut it down like a circuit breaker, with alerts." The comparison needs a recorded normal rate, which is why the queryable event record is a rebuild prerequisite and not future work. The breaker trips at the BROKER, not through the desk; a compromised desk will not honour its own brakes.

**Owner-facing dashboard, read-only and off the money host.** A small front page — profit and loss, holdings, trades, charting — for people interested in how the desk is doing, with no journal, no controls and no internals. It is NOT served from the QAMC host and NOT from the Sentinel host. Not QAMC, because the money machine must keep no inbound path; not the Sentinel, because the Sentinel holds the authority to cancel and flatten, and a public web surface on that machine hands an attacker a route to the one service that can touch the book. It needs no VPS of its own either: it has no database, no credentials and no running logic, so the snapshot is rendered as a static page on ordinary static hosting, behind credentials (owner ruling 2026-10-01: not fully public, since live holdings are disclosure beyond the already-public historical data). It is a one-way sink: a breach of that host reaches a copy of yesterday's and today's numbers and nothing else. Credentials for it are provisioned by the owner and never held in the repository.

**Where the owner-facing page actually lives (owner, 2026-10-01).** On the owner's existing shared hosting — the managed account that already serves his mail and WordPress sites — behind an ordinary site password. No new VPS is bought and none is needed.

Shared hosting is the least trustworthy machine in the picture: other tenants, someone else's patching, and a WordPress install next door, which is one of the most routinely compromised things on the internet. It is nonetheless the correct host, precisely BECAUSE the page is worth nothing to an attacker. It holds no broker credentials, no QAMC credentials, no database and no logic — only a rendered snapshot. Whoever takes that host gets a copy of some numbers and no path to anything. That is the entire design: put the weakest host where a breach buys the least.

Three rules make that true, and the build must honour all three:
- **The snapshot is scrubbed before it leaves.** No account identifier, no key or token, no internal hostname or filesystem path, no provider credentials. It carries profit and loss, holdings, trades and chart data, and nothing else. A committed test should assert this, because a scrubber nobody checks is a scrubber that rots.
- **The push is outbound only, and the credential is one-way.** QAMC uploads to one directory on the shared host. That upload credential is scoped to that directory, is useless for reaching QAMC, and is not the owner's main hosting login. A compromise of the shared host must not yield anything that can be turned around and pointed at the money machine.
- **The page is static.** No database connection, no API, no server-side code reaching anywhere. If rendering needs logic, the logic runs on QAMC before the push, not on the shared host.

The owner's mail living on the same host is a pre-existing consideration and unrelated to the desk, but it is a reason to keep the upload credential separate from the main hosting account rather than reusing it.

**What the rebuild must leave behind for all of this.** Three seams, and all three are needed by QAMC for its own reasons:
- a queryable record of what the desk decided and did, not log lines;
- a published state snapshot it can produce honestly, which requires the desk to know what it believes;
- a freeze flag read on the path to every order, verified to work when the desk is unresponsive.
Deliberately left open until there is real data: how long a heartbeat may be missing before escalating, and whether the Sentinel may restart the desk — the current answer is no, because a watcher that can restart things can loop.

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

**This list is no longer the source of truth — the gate is (board item 150).**
Every condition above is enumerated in `src/live_capital_preflight.py` and the
list here is a human-readable mirror of it. The gate sits at the point where the
paper lock would be lifted: `AlpacaConfig._enforce_paper_only` in
`src/config.py` refuses a non-paper account unless BOTH the reviewed code
constant `config.LIVE_TRADING_AUTHORIZED` is flipped AND the gate reports every
activation-scope condition satisfied, and a refusal names the conditions that
failed. Run it with `.venv/bin/python scripts/live_capital_preflight.py`
(add `--scope activation` to see only what the live switch itself evaluates).

Conditions the desk cannot prove in code are NOT assumed satisfied: each one is
an explicit blocker requiring a signed entry in
`config/live_capital_preflight_attestations.yaml` (who attested, on what date).
Nothing is signed today, so the gate blocks. Adding a condition here means
adding it to the gate; the roster is pinned by a test, so it cannot be shortened
silently.

## 3. Mission Control security panel

A read-only panel showing host and network security next to trading state, so posture is visible from the cockpit instead of over SSH. Background: `ops/security/vps-hardening-plan.md`.

**Could show:** SSH attack attempts (failure rate, source IPs), fail2ban bans, firewall events, active connections, listening ports and interfaces, service health, unusual resource or network activity.

**Non-goals**
- No Grafana, Prometheus, Loki or other monitoring stack. First evaluate a small read-only pull into the existing Mission Control API.
- Display only: no ban/unban, firewall edits or restarts from the UI.
- Not part of current work; dashboard work follows deployed-MVP acceptance (`docs/OUTCOME.md`, MVP lifecycle principle).
