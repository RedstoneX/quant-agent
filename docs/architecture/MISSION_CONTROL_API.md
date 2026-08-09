# Mission Control API — Accepted Stage 2 Contract

Status: **ACCEPTED / Checkpoint C — 2026-08-09**.  
Acceptance evidence: `docs/CHECKPOINT_C_ACCEPTANCE.md`.

## Purpose

`src/api/` is a small **read-only, separate-process adapter** that exposes existing quant-agent state for Mission Control. It is not a trading engine, memory system, or trading dependency.

```text
Mission Control / browser
        |
        | GET only
        v
src/api/  (separate FastAPI process)
   |                         |
   | SQLite mode=ro          | broker read methods only
   v                         v
canonical SQLite         Alpaca Paper
        ^
        |
TradingPipeline writer — unaware of src/api
```

## Accepted isolation / safety properties

- API runs independently from the trading process.
- Trading code does not import or depend on `src.api`; API death has zero trading impact.
- Historical reads use an independent SQLite `mode=ro` connection rather than the trading writer connection.
- Broker-live reads are structurally separate from SQLite history reads.
- Routes are GET-only, with app-level rejection of non-read HTTP methods.
- No broker order placement/cancel/modify/close/stop-management methods are exposed through the API.
- Responses use typed schemas and do not expose credential-bearing configuration.
- Mission Control cannot bypass deterministic risk/execution.

## Accepted endpoint surface

| Route | Source / meaning |
|---|---|
| `GET /health` | API/DB/broker/session health evidence |
| `GET /account` | broker-live account + historical daily P&L |
| `GET /positions` | broker-live positions |
| `GET /orders` | broker-live read-only order listing |
| `GET /trades` | canonical SQLite trades |
| `GET /runs` | run summaries derived from `agent_logs` |
| `GET /runs/{run_id}` | run-level agent/trade reconstruction |
| `GET /decisions/{decision_id}` | PM → AI Risk / hard-risk → resulting trade reconstruction |
| `GET /agents` | configured agent roster/model/provider |
| `GET /agents/{agent_name}` | agent configuration + recent canonical call history |
| `GET /reflections` | recent insights + Meta Reflector artifact presence |
| `GET /candidates` | rebuildable watchlist/universe-expansion candidates derived from persisted insights |

## Deterministic hard-risk forensics

Checkpoint C closed the original “full hard-risk block leaves no canonical reason” gap.

When the deterministic gate blocks every candidate, the pipeline writes one additive forensic `agent_logs` row with `agent_name="risk_gate"` using the existing persistence mechanism. This is **not an LLM agent** and does not alter risk calculations, eligibility, execution, or broker behavior.

- `GET /runs/{run_id}` computes `hard_risk_block_recorded` from the presence of that row.
- `GET /decisions/{decision_id}` can return the corresponding `hard_risk_block` record.
- Old databases remain compatible; they simply contain no historical `risk_gate` rows before this addition.

## Intentional limitations

- No API write operations.
- No journal/search index; those are post-Stage-2 concerns and currently subject to Discovery R1.
- `/reflections` does not parse or summarize Meta Reflector free-text artifacts.
- `/candidates` represents the existing persisted universe-expansion/watch concept, not a newly invented intraday candidate engine.
- The API does not become canonical storage for UI-derived state.

## Verification anchors

The accepted implementation is covered by API contract, safety, no-secret, DB-concurrency, process-isolation, and pipeline forensic tests. The accepted full suite at Checkpoint C was **1530 passed, 0 failed**.

For implementation history and original findings, use Git history plus `docs/CHECKPOINT_C_ACCEPTANCE.md` and `docs/STAGE0_BASELINE_AUDIT.md`; historical narrative is not part of this live contract.

## Run

```bash
pip install -e '.[api]'
python -m src.api
```

Default bind remains loopback/private-oriented; current remote-access policy prefers Tailscale/private networking rather than making this a public trading dependency.
