# QAMC Product Outcome

This file states the result QAMC is trying to achieve. It is intentionally less prescriptive than the architecture and roadmap: Claude Code should use it to challenge whether the current plan is actually the best way to reach the outcome.

## Outcome

Build a small, understandable autonomous AI-assisted **Alpaca paper-trading experiment** that can determine whether inexpensive modern AI models add measurable out-of-sample trading value beyond deterministic market signals.

The system should run largely unattended while giving the operator a polished browser/iPad Mission Control that makes the trading process understandable rather than opaque.

The operator should be able to understand, without reading raw logs:
- current account/equity/P&L/positions/orders/trades and system health;
- what candidates the system considered;
- what specialist agents concluded and where they disagreed;
- what the Portfolio Manager proposed;
- what the AI Risk Manager changed or rejected;
- what deterministic Python ultimately allowed or blocked;
- what actually executed versus what was proposed;
- which model/provider actually answered, its cost/latency/tokens, and whether fallback occurred;
- what happened on prior days through a useful journal and forensic search;
- whether model/prompt choices appear to add measurable value over time.

## Hard outcome constraints

These are not implementation suggestions; they define the safe experiment:
- Alpaca **Paper only** until a separate future authorization.
- `yebof/quant-agent` remains the authoritative trading engine unless the operator explicitly changes that project premise.
- deterministic Python and broker protections remain final safety/execution authority and fail closed;
- Mission Control/read-side failure must not stop trading or weaken broker protection;
- UI/search/journal state must not become a second authoritative trading-memory system;
- no secrets or fake production trading state exposed to the UI;
- keep the experiment small enough to understand, operate, and evaluate rather than turning it into a bespoke platform.

## Design freedom

Everything else is challengeable during discovery.

Existing architecture, donor choices, stage boundaries, data presentation, component structure, sequencing, and implementation techniques are prior proposals—not instructions to preserve merely because they already exist in Git.

Claude Code is expected to inspect the actual repository and challenge whether those choices still provide the simplest, safest and most effective path to this outcome. Material changes to accepted safety/product boundaries still require reconciliation and approval before implementation.
