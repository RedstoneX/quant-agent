# QAMC Product Outcome

This file states the result QAMC is trying to achieve. It is intentionally less prescriptive than the architecture and roadmap: Claude Code should use it to challenge whether the current plan is actually the best way to reach the outcome.

## Outcome

Build a small, understandable autonomous AI-assisted **Alpaca trading system** that can determine whether inexpensive modern AI models add measurable out-of-sample trading value beyond deterministic market signals.

QAMC is being validated first in Alpaca Paper. Paper is the current execution environment and safety authorization, not the product identity. If the system earns progression to live capital, the same decision, risk, execution, position-management, journaling and observability architecture should carry forward without a paper-to-live redesign.

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

## Execution-environment principle

Paper and live operation share one trading architecture. No agent, portfolio-construction, risk, position-management, reflection or dashboard semantics should become easier, looser, or materially different merely because the current broker account is Paper.

Environment-specific differences belong at the broker/configuration boundary: credentials, endpoint selection, account identity and genuine execution-mechanics differences such as simulated versus real fills/slippage. Live activation, if later authorized, should therefore be a focused operational/risk authorization change rather than a rewrite of the trading system.

## Mission Control product direction

Mission Control is intended to feel like a **real trading cockpit**, not a vertically stacked database/log viewer.

`docs/visual/MISSION_CONTROL_VISION_BOARD.png` is the durable product reference for layout and information hierarchy: a dense desktop-first cockpit, responsive on iPad, with a compact account/status strip, candidate/watchlist context, chart-led market context where authoritative data supports it, a visually prominent Specialist → PM → Risk → deterministic gate → execution chain, positions/orders/trades as supporting state, and journal/investigation/learning views that explain decisions and missed opportunities.

The governing UX principles are:

- **Clarity first** — the important trading state is immediately visible.
- **Transparency always** — reasoning, disagreement, vetoes and deterministic blocks are inspectable.
- **Explanation before action** — especially for no-trade and rejected-trade states.
- **Human in control** — without making Mission Control part of the trading-critical path.
- **Maximum useful reuse, minimum custom infrastructure** — improve the existing product before inventing new durable systems.

The visual reference is directional, not blanket feature authorization. Mockup concepts that conflict with current safety boundaries — including broker-write PAUSE/KILL controls, direct trade controls, or other write paths — remain unimplemented unless separately authorized. Do not fabricate unsupported data merely to match a mockup.

## MVP lifecycle principle

QAMC should reach a safe, observable deployed baseline and then **start real-market validation in Alpaca Paper promptly**. Paper evidence is not the reward after polish; it is the evidence needed to decide what should be improved next and whether the system could eventually justify live-capital authorization.

The expected sequence is:

**functional foundation → integrated verification → VPS deployment → runtime commissioning → Paper validation → observe/evaluate natural sessions → iterative agent/code/dashboard improvement → separate live-capital authorization if earned**.

Before Paper validation starts, the product needs enough observability to understand account state, decisions, execution, health and history. It does **not** need every desirable reasoning refinement, benchmark, chart or UX improvement.

After validation starts, engineering should use observed trading behaviour and operator experience to prioritize work: weak evidence, poor decisions, excessive vetoes, execution problems, missing telemetry, confusing Mission Control views, missed opportunities in either direction, model cost/latency and measurable out-of-sample performance.

Now that the validation run is active, visual/product convergence is valid engineering work when the running cockpit materially fails the intended operator experience. Functional correctness alone is not sufficient acceptance for a Mission Control redesign.

## Hard outcome constraints

These are not implementation suggestions; they define the currently authorized safe system:
- Alpaca **Paper is the only currently authorized execution environment** until a separate future live-capital authorization;
- the trading architecture must remain environment-neutral: no paper-only shortcuts or separate paper-specific decision/risk path;
- `yebof/quant-agent` remains the authoritative trading engine unless the operator explicitly changes that project premise;
- deterministic Python and broker protections remain final safety/execution authority and fail closed;
- Mission Control/read-side failure must not stop trading or weaken broker protection;
- UI/search/journal state must not become a second authoritative trading-memory system;
- no secrets or fake production trading state exposed to the UI;
- directional capability must remain inside the supported instrument/risk contracts and must not bypass deterministic safety;
- keep the system small enough to understand, operate and evaluate rather than turning it into a bespoke platform.

## Design freedom

Everything else is challengeable during discovery and post-validation iteration.

Existing architecture, donor choices, stage boundaries, data presentation, component structure, sequencing and implementation techniques are prior proposals—not instructions to preserve merely because they already exist in Git.

Claude Code is expected to inspect the actual repository and challenge whether those choices still provide the simplest, safest and most effective path to the outcome. Material changes to accepted safety/product boundaries still require reconciliation and approval before implementation.
