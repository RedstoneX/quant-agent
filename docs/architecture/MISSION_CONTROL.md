# Mission Control Architecture

## Decision
Build a **QAMC-native React/Vite/Tailwind UI** with one QAMC-native data contract. Do not preserve OpenTradex's backend worldview just to reuse its frontend.

## Core screens
- Cockpit: equity/P&L, positions, orders/trades, watchlist/candidates, charts, system health.
- Agents: per-agent role/model/provider/recommendation/confidence/reasoning summary/cost.
- Decision Chain: PM proposal → AI Risk response → deterministic gate → executed/rejected delta.
- Journal: calendar/list/daily page and search.
- Learning: Meta Reflector reports/diffs/history (later stage).
- Administration: initially read-only; write controls only after explicit milestone.

## Data contract philosophy
Expose simple QAMC resources such as account, positions, orders, trades, agents, decisions, risk, journal, models, learning and health. Final endpoint shapes are frozen only after Stage 0/2 inspection.

## Donor strategy
OpenTradex contributes presentation primitives/patterns. Orallexa contributes multi-agent semantics/presentation. TradingView Lightweight Charts owns financial visualization. Donor backend assumptions are discarded.
