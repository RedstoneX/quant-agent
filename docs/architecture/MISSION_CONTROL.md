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
Expose simple QAMC resources such as account, positions, orders, trades, agents, decisions, risk, journal, models, learning and health. Mission Control consumes the accepted Stage-2 read-only API and must not become a second trading engine or trading-critical dependency.

## Stage 2 accepted result (Checkpoint C, 2026-08-09)
The endpoint contract is implemented and documented in `docs/architecture/MISSION_CONTROL_API.md`:

- `/health`
- `/account`
- `/positions`
- `/orders`
- `/trades`
- `/runs`
- `/runs/{run_id}`
- `/decisions/{decision_id}`
- `/agents`
- `/agents/{agent_name}`
- `/reflections`
- `/candidates`

Checkpoint C also closed the deterministic hard-risk reconstruction gap. A forensic `agent_logs` row with sentinel `agent_name="risk_gate"` is written when the deterministic gate blocks every candidate, including both the pre-RM and post-RM-modification paths. This is additive forensic persistence only; deterministic risk/execution semantics are unchanged. Authoritative acceptance record: `docs/CHECKPOINT_C_ACCEPTANCE.md`.

## Stages 3–5 coordinated build tranche
`docs/MISSION_CONTROL_BUILD_TRANCHE.md` authorizes Stages 3–5 as one coordinated engineering tranche with internal stage gates and a single external STOP after Stage 5 / Checkpoint E. Claude Code owns implementation orchestration within the frozen architecture and safety boundaries.

## Donor strategy
OpenTradex contributes presentation primitives/patterns. Orallexa contributes multi-agent semantics/presentation. TradingView Lightweight Charts owns financial visualization. Donor backend assumptions are discarded.
