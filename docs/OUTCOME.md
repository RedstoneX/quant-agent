# QAMC Product Outcome

This file states the result QAMC is trying to achieve. It is intentionally less prescriptive than the architecture and roadmap: Claude Code should use it to challenge whether the current plan is actually the best way to reach the outcome.

## Outcome

Build a small, understandable autonomous AI-assisted **Alpaca trading system** that can determine whether inexpensive modern AI models add measurable out-of-sample trading value beyond deterministic market signals.

QAMC is being validated first in Alpaca Paper. Paper is the current execution environment and safety authorization, not the product identity. If the system earns progression to live capital, the same decision, risk, execution, position-management, journaling and observability architecture should carry forward without a paper-to-live redesign.

The system should run largely unattended while giving the operator a browser/iPad Dashboard that makes the trading process understandable rather than opaque.

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

Paper and live operation share one trading architecture. No agent, portfolio-construction, risk, position-management, reflection or Dashboard semantics should become easier, looser, or materially different merely because the current broker account is Paper.

Environment-specific differences belong at the broker/configuration boundary: credentials, endpoint selection, account identity and genuine execution-mechanics differences such as simulated versus real fills/slippage. Live activation, if later authorized, should therefore be a focused operational/risk authorization change rather than a rewrite of the trading system.

## Terminology

- **QAMC / Mission Control** refers to the whole product/system.
- **Dashboard** refers specifically to the browser/iPad read-side UI and the parallel frontend/UX workstream.
- **Core recovery** refers to trading/backend deployment and natural-validation work.

This distinction is intentional so future sessions do not mistake the Dashboard for the whole Mission Control project.

## Dashboard product direction

The QAMC Dashboard is intended to feel like a **real trading cockpit**, not a vertically stacked database/log viewer.

`docs/visual/MISSION_CONTROL_VISION_BOARD.png` is the durable product reference for layout, information hierarchy and donor direction. It is not merely historical inspiration. Product work should actively preserve the strongest ideas already captured there while correcting any semantic or data-truth problems discovered later.

The cockpit should be dense but legible, desktop-first and strong on iPad, with:
- a compact account/status strip;
- watchlist/candidate context;
- chart-led market context where authoritative data supports it;
- a visually prominent **Specialists → Portfolio Manager → AI Risk → deterministic gate → execution** chain;
- positions/orders/trades as supporting state rather than the whole product;
- structured journal, investigation and learning views that explain decisions and missed opportunities.

### Private Research Desk principle

QAMC is built for **one operator**, not customers. The Research/Intelligence experience should therefore optimize for usefulness and willingness to read it every day, not corporate polish.

The voice should feel like a sharp internal trading desk: candid, compact, substantive, occasionally dry or irreverent when the evidence earns it. Avoid generic AI prose, filler, repeated conclusions, forced jokes, fake quotes and performative cleverness. **Say everything useful. Nothing merely decorative.**

Brevity must not become thinness. Each useful research item should give enough evidence, interpretation and consequence to answer: what happened, what changed, why it matters now, what conflicts with it, and what the PM/Risk implication is.

Use visual structure to reduce reading effort where it helps: signal agreement/conflict, what changed, why now, evidence chips, important tension, compact chart context, and clearly separated Read / PM / Risk consequences. Do not mechanically put every device on every card.

The default composition should be deliberately designed, visually balanced and easy to scan. Important stories may dominate while supporting material recedes. Docking/resizing is operator personalization on top of a strong default layout, not a substitute for design.

Raw JSON/logs remain secondary evidence drill-down. They are never the primary reading experience.

### Design-donor decisions to preserve

The vision board intentionally uses donors for proven interaction/design ideas while keeping `quant-agent` as the authoritative trading engine.

**OpenTradex — shell/layout donor**
- Keep/adapt: resizable panes, terminal-style layout, compact top account bar, reusable public UI components, and the run-control visual concept where it remains read-only/safe.
- Do not adopt: its trading engine/gateway, command-chat control path, or internal data models.

**Oralexa — agent/decision visualization donor**
- Keep/adapt as first-class QAMC concepts: **agent cards**, debate/signal-fusion presentation, Portfolio Manager decision cards, scoreboards where backed by real metrics, and bias/self-correction presentation for the Learning Center.
- The agent cards should make each specialist's stance, important evidence, disagreements and confidence/calibration visible without requiring raw-log reading.
- The chain should visually show how specialist views combine into PM intent, how Risk modifies/rejects it, what deterministic Python does next, and what actually reaches the broker.
- Do not adopt Oralexa mock-data layers or its trading logic.

**TradingView Lightweight Charts — charting donor**
- Use/adapt for chart-led context: candlesticks, indicators, volume, trade markers and streaming updates where the existing QAMC data contracts can support them truthfully.
- Charts are context for the decision process, not a replacement trading engine or source of fabricated signals.

### Agent-card principle

Agent cards are a core interaction pattern, not decorative status tiles.

A useful card should answer, at a glance:
- what this agent currently believes;
- the strongest evidence behind the view;
- what would invalidate/change the view where available;
- whether it agrees or conflicts with other agents;
- any genuine confidence/calibration signal that exists in authoritative data.

Do not invent pseudo-confidence percentages or arbitrary gauges merely to make a card look complete. If confidence is not authoritative, show the actual qualitative state/evidence rather than manufacturing precision.

### Decision-chain principle

The operator should be able to follow one compact graphical story:

**candidate/opportunity → specialist cards → disagreements/signal fusion → PM proposal → Risk approval/modification/rejection → deterministic gate → execution → position/exit result.**

The cockpit should emphasize changes and deltas: what PM proposed, what Risk changed, what deterministic Python blocked, and what actually executed.

### Journal Day page

The structured Journal Day mockup on the vision board is an accepted product direction and should not be replaced by an endless chronological event dump.

A day page should compress the session into a useful operator narrative such as:
1. **Market Thesis**
2. **Watchlist / Candidates**
3. **Agent Analysis** — compact specialist cards/table
4. **Disagreements**
5. **Portfolio Manager Proposal**
6. **Risk Review**
7. **Proposed → Executed Difference**
8. **Trades**
9. **Daily Result**
10. **Lesson Learned**
11. **Tomorrow / what to monitor**

The detailed event/log stream can remain available for forensics, but it is not the primary journal experience.

### Required screen states

The Dashboard should deliberately handle the major workflow states shown in the vision board rather than treating them as incidental variations:
- normal/no-trade;
- proposed trade;
- rejected/modified trade;
- executed trade;
- learning/reflection.

Each state must remain truthful when data is absent, partial or degraded; an empty or no-trade state should look intentional rather than broken.

### UX principles

- **Truth before decoration** — candidate/run attribution, capital semantics, execution state and agent provenance must be correct.
- **Clarity first** — the important trading state is immediately visible.
- **Transparency always** — reasoning, disagreement, vetoes and deterministic blocks are inspectable.
- **Explanation before action** — especially for no-trade and rejected-trade states.
- **Graphical synthesis before text dumping** — use cards, chains, charts, funnels and deltas to summarize; preserve drill-down for detail.
- **Human in control** — without making the Dashboard part of the trading-critical path.
- **Maximum useful reuse, minimum custom infrastructure** — adapt the donor ideas and existing product before inventing new durable systems.
- **Desktop + iPad first** — phone is secondary to a strong cockpit experience on the operator's primary surfaces.

### Professional visual-composition standard

Functional correctness is necessary but not sufficient. The Dashboard must also satisfy basic professional interface-design fundamentals in the rendered product.

- **Coherent hierarchy:** typography, scale, weight and placement must make the most important trading state visually dominant. Brand/header, primary metrics, section titles, labels, body text and metadata should form a deliberate readable type hierarchy rather than unrelated font sizes.
- **Readable typography:** routine labels and explanatory text must remain comfortably legible on desktop and iPad; small text must not be used merely to make dense panels fit.
- **Proportion:** a component must visually justify the space allocated to it. Important graphics such as risk deployment, decision flow and agent analysis should scale to their containers rather than appearing as tiny islands inside large empty rectangles.
- **Intentional whitespace:** empty space should create hierarchy and breathing room, not dominate the page because fixed containers remain large when content is absent.
- **Adaptive sparse states:** no-candidate, no-position and low-information states should collapse, rebalance or repurpose space so the cockpit remains composed and useful instead of becoming a sea of empty panels.
- **Grid and rhythm:** columns, card edges, baselines, gaps and repeated structures should align to a coherent layout/spacing system. Unequal proportions should be intentional and reflect information importance.
- **Information density:** the cockpit should use its canvas efficiently. Avoid both extremes: cramped micro-text and oversized containers with very little content.
- **Designed surfaces, not generic breakpoints:** desktop and iPad should each look deliberately composed, not simply like the same grid squeezed or stretched.
- **Visual acceptance is empirical:** implementation is not complete merely because it builds, tests pass or nothing overlaps. Rendered screenshots must be inspected at target desktop and iPad sizes against the vision board and professional trading-dashboard standards; obvious hierarchy, typography, proportion, balance or empty-state defects remain product bugs.

The target is a credible professional trading cockpit, not a technically correct dashboard shell containing miniature widgets.

The visual reference is directional, not blanket feature authorization. Mockup concepts that conflict with current safety boundaries — including broker-write PAUSE/KILL controls, direct trade controls, or other write paths — remain unimplemented unless separately authorized. Do not fabricate unsupported data merely to match a mockup.

## MVP lifecycle principle

QAMC should reach a safe, observable deployed baseline and then **start real-market validation in Alpaca Paper promptly**. Paper evidence is not the reward after polish; it is the evidence needed to decide what should be improved next and whether the system could eventually justify live-capital authorization.

The expected sequence is:

**functional foundation → integrated verification → VPS deployment → runtime commissioning → Paper validation → observe/evaluate natural sessions → iterative agent/code/dashboard improvement → separate live-capital authorization if earned**.

Before Paper validation starts, the product needs enough observability to understand account state, decisions, execution, health and history. It does **not** need every desirable reasoning refinement, benchmark, chart or UX improvement.

After validation starts, engineering should use observed trading behaviour and operator experience to prioritize work: weak evidence, poor decisions, excessive vetoes, execution problems, missing telemetry, confusing Dashboard views, missed opportunities in either direction, model cost/latency and measurable out-of-sample performance.

Now that the validation run is active, visual/product convergence is valid engineering work when the running Dashboard materially fails the intended operator experience. Functional correctness alone is not sufficient acceptance for a Dashboard redesign.

## Hard outcome constraints

These are not implementation suggestions; they define the currently authorized safe system:
- Alpaca **Paper is the only currently authorized execution environment** until a separate future live-capital authorization;
- the trading architecture must remain environment-neutral: no paper-only shortcuts or separate paper-specific decision/risk path;
- `yebof/quant-agent` remains the authoritative trading engine unless the operator explicitly changes that project premise;
- deterministic Python and broker protections remain final safety/execution authority and fail closed;
- Dashboard/read-side failure must not stop trading or weaken broker protection;
- UI/search/journal state must not become a second authoritative trading-memory system;
- no secrets or fake production trading state exposed to the UI;
- directional capability must remain inside the supported instrument/risk contracts and must not bypass deterministic safety;
- keep the system small enough to understand, operate and evaluate rather than turning it into a bespoke platform.

## Design freedom

Everything else is challengeable during discovery and post-validation iteration.

Existing architecture, donor choices, stage boundaries, data presentation, component structure, sequencing and implementation techniques are prior proposals—not instructions to preserve merely because they already exist in Git. The explicit donor/product principles above, however, represent desired product outcomes and should not be silently discarded merely because later work focused on semantic correctness.

Claude Code is expected to inspect the actual repository and challenge whether implementation choices still provide the simplest, safest and most effective path to the outcome. Material changes to accepted safety/product boundaries still require reconciliation and approval before implementation.
