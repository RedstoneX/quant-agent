# Mission Control API — Accepted Stage 2 Contract

Status: **ACCEPTED / Checkpoint C — 2026-08-09**.

## Purpose / isolation

`src/api/` is a separate-process, read-only adapter over existing quant-agent state. It is not a trading engine, memory system or trading dependency.

- Trading code does not depend on `src.api`; API failure has zero trading impact.
- Historical reads use independent SQLite `mode=ro` connections.
- Broker-live reads remain separate from SQLite history reads.
- Routes are GET-only with app-level rejection of non-read methods.
- No broker order placement/cancel/modify/close/stop-management path is exposed.
- Typed responses must not expose credential-bearing configuration.
- Mission Control cannot bypass deterministic risk/execution.

## Accepted endpoints

| Route | Meaning |
|---|---|
| `/health` | API/DB/broker/session health |
| `/account` | broker-live account + historical daily P&L |
| `/positions` | broker-live positions |
| `/orders` | broker-live read-only orders |
| `/trades` | canonical trades |
| `/runs` | run summaries from `agent_logs` |
| `/runs/{run_id}` | run reconstruction |
| `/decisions/{decision_id}` | PM → AI Risk/hard-risk → trade reconstruction |
| `/agents` | configured agent roster/model/provider |
| `/agents/{agent_name}` | configuration + recent canonical call history |
| `/reflections` | recent insights + Meta artifact presence |
| `/candidates` | rebuildable watchlist/universe-expansion candidates from persisted insights |

## Hard-risk forensics

When the deterministic gate blocks every candidate, the pipeline writes one additive forensic `agent_logs` row with `agent_name="risk_gate"`. It is not an LLM agent and does not alter risk calculations, eligibility, execution or broker behavior.

`/runs/{run_id}` and `/decisions/{decision_id}` use that record to reconstruct full hard-risk blocks. Old databases remain compatible and simply lack pre-change rows.

## Limits

- No API writes.
- No authoritative UI/journal/search storage.
- `/reflections` does not parse Meta Reflector free text.
- `/candidates` exposes the existing persisted watch/universe-expansion concept, not a new intraday candidate engine.

Accepted full suite at Checkpoint C: **1530 passed, 0 failed**.
Historical evidence: `docs/history/CHECKPOINT_C_ACCEPTANCE.md`, `docs/history/STAGE0_BASELINE_AUDIT.md`, and Git history.
