# QAMC Product Outcome

This file states the result QAMC is trying to achieve. It is intentionally less prescriptive than the architecture and roadmap: Claude Code should use it to challenge whether the current plan is actually the best way to reach the outcome.

## Outcome

Build a small, understandable autonomous AI-assisted **Alpaca paper-trading experiment** that can determine whether inexpensive modern AI models add measurable out-of-sample trading value beyond deterministic market signals.

The system should run largely unattended while giving the operator a browser/iPad Mission Control that makes the trading process understandable rather than opaque.

**Directional neutrality is a product requirement, not a promise of constant activity.** QAMC should not structurally depend on rising equity markets to have an opportunity set. Within the instruments and risk architecture actually supported by the project, it should be able to express bullish, bearish or neutral/cash views and evaluate missed opportunities in both directions. The currently supported bearish expression is through the approved inverse ETFs already in the universe; this statement does not authorize direct stock shorting, options, margin or a deterministic risk/execution redesign.

The operator should be able to understand, without reading raw logs:
- current account/equity/P&L/positions/orders/trades and system health;
- current directional posture and whether apparent cash is raw cash, sweep-parked liquidity or risk exposure;
- what candidates the system considered;
- what specialist agents concluded and where they disagreed;
- what the Portfolio Manager proposed;
- what the AI Risk Manager changed or rejected;
- what deterministic Python ultimately allowed or blocked;
- why an active session produced no trade when candidates existed;
- what actually executed versus what was proposed;
- which model/provider actually answered, its cost/latency/tokens, and whether fallback occurred;
- what happened on prior days through a useful journal and forensic search;
- what meaningful bullish or bearish opportunities the system missed;
- whether model/prompt choices appear to add measurable value over time.

## MVP lifecycle principle

QAMC should reach a safe, observable deployed baseline and then **start the Alpaca Paper experiment promptly**. Paper-soak evidence is not the reward after polish; it is the evidence needed to decide what should be improved next.

The expected sequence is:

**functional foundation → integrated verification → VPS deployment → runtime commissioning → paper-soak start → observe/evaluate real sessions → iterative agent/code/dashboard improvement**.

Before soak start, the product needs enough observability to understand account state, decisions, execution, health and history. It does **not** need every desirable reasoning refinement, benchmark, chart or UX improvement.

After soak start, engineering should use observed trading behaviour and operator experience to prioritize work: weak evidence, poor decisions, excessive vetoes, execution problems, missing telemetry, confusing Mission Control views, missed opportunities in either direction, model cost/latency and measurable out-of-sample performance.

Dedicated visual polish remains valuable, but it must not delay a safe paper experiment once the minimum useful cockpit and commissioning gates are satisfied. Likewise, agent/prompt/model improvements can continue during the soak rather than being treated as prerequisites to collecting real evidence.

## Hard outcome constraints

These are not implementation suggestions; they define the safe experiment:
- Alpaca **Paper only** until a separate future authorization;
- `yebof/quant-agent` remains the authoritative trading engine unless the operator explicitly changes that project premise;
- deterministic Python and broker protections remain final safety/execution authority and fail closed;
- Mission Control/read-side failure must not stop trading or weaken broker protection;
- UI/search/journal state must not become a second authoritative trading-memory system;
- no secrets or fake production trading state exposed to the UI;
- directional capability must remain inside the supported instrument/risk contracts and must not bypass deterministic safety;
- keep the experiment small enough to understand, operate and evaluate rather than turning it into a bespoke platform.

## Design freedom

Everything else is challengeable during discovery and post-soak iteration.

Existing architecture, donor choices, stage boundaries, data presentation, component structure, sequencing and implementation techniques are prior proposals—not instructions to preserve merely because they already exist in Git.

Claude Code is expected to inspect the actual repository and challenge whether those choices still provide the simplest, safest and most effective path to the outcome. Material changes to accepted safety/product boundaries still require reconciliation and approval before implementation.
