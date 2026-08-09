# Mission Control API — Accepted Stage 2 Contract

Status: **ACCEPTED / Checkpoint C — 2026-08-09**.

`src/api/` is a separate-process, read-only adapter over existing quant-agent state. It is not a trading engine, memory system or trading dependency.

- Trading code does not depend on `src.api`; API failure has zero trading impact.
- Historical reads use independent SQLite `mode=ro` connections.
- Broker-live reads remain separate from SQLite history reads.
- Routes are GET-only with app-level rejection of non-read methods.
- No broker order placement/cancel/modify/close/stop-management path is exposed.
- Typed responses must not expose credential-bearing configuration.
- Mission Control cannot bypass deterministic risk/execution.

Accepted endpoints: `/health`, `/account`, `/positions`, `/orders`, `/trades`, `/runs`, `/runs/{run_id}`, `/decisions/{decision_id}`, `/agents`, `/agents/{agent_name}`, `/reflections`, `/candidates`.

When the deterministic gate blocks every candidate, the pipeline writes one additive forensic `agent_logs` row with `agent_name="risk_gate"`. It is not an LLM agent and does not alter risk calculations, eligibility, execution or broker behavior. Run/decision reads use it to reconstruct full hard-risk blocks; old databases simply lack pre-change rows.

Limits:
- no API writes;
- no authoritative UI/journal/search storage;
- `/reflections` does not parse Meta Reflector free text;
- `/candidates` exposes the existing persisted watch/universe-expansion concept, not a new intraday candidate engine.

Accepted full suite at Checkpoint C: **1530 passed, 0 failed**.
Detailed acceptance evidence remains in Git history. The last pre-ultra-lean working-tree snapshot is commit `02e20e6ac1c5c7e65b7f512f76c568328c990e3c`.
