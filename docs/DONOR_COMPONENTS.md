# Approved Donor Inventory

This file is the gate against unnecessary custom UI work. Stage 0 must replace broad descriptions with exact files/components/commits after inspection.

## OpenTradex — primary trading UI donor
Repository: `deonmenezes/opentradex` (MIT).

Prefer adapting presentation-only assets such as:
- resizable panel/layout primitives;
- cockpit navigation/sidebar concepts;
- cards, tables, status badges, confirm modals;
- run/audit/status presentation;
- responsive terminal visual language.

Known component examples to inspect: `Resizable`, `AgentConsole`, `FlowVisualizer`, `HarnessStatusBadges`, `RunsAuditPanel`, sidebars, confirmation/command presentation.

Explicitly discard:
- `useHarness` and OpenTradex-specific API/data normalization;
- gateway/trading engine;
- Kalshi/Polymarket/scraper assumptions;
- OpenTradex risk/execution models;
- any backend contract that forces quant-agent to pretend to be OpenTradex.

## Orallexa — multi-agent UI donor
Use/adapt where implementation is genuinely reusable:
- individual analyst cards;
- recommendation/confidence presentation;
- disagreement/debate/signal-fusion visualization;
- Portfolio Manager and risk decision cards;
- model scoreboard, token/cost budget concepts;
- daily intelligence/watchlist patterns.

Do not import its trading logic or mock/demo data paths.

## TradingView Lightweight Charts — charting foundation
Use for candlesticks, volume, indicators and trade markers. Preserve required attribution/license obligations.

## QuantDinger
Visual/design inspiration only by default.

## Trading journal donor
Information architecture only; no unlicensed code copy. Do not import Firebase/auth/storage architecture.

## Rule
Before building a component from scratch, check this inventory. Reuse only when adapting the component is simpler than reproducing it natively and does not import unwanted architecture.
