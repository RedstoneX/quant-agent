# Mission Control API — Stage 2 (Thin Read-Only)

Status: **implemented 2026-08-09** on branch
`claude/stage-2-mission-control-api-4zpx7j`, awaiting Checkpoint C operator
acceptance (see `docs/MILESTONES.md` Stage 2 and `docs/CHECKPOINT_C_ACCEPTANCE.md`
once recorded).

## What this is

A small, separate, read-only HTTP API (`src/api/`, FastAPI + uvicorn) that
exposes quant-agent's existing canonical state — SQLite history and
broker-live account/positions/orders — for a future Mission Control UI
(Stage 3+). It is a thin adapter, not a second trading engine, memory store,
or operational dependency.

```text
Browser / future Mission Control UI
    |
    | GET only
    v
src/api/  (FastAPI, separate process — python -m src.api)
    |                                  |
    | own mode=ro SQLite connection    | AlpacaBroker.get_account() /
    v                                  | .get_positions() / client.get_orders()
data/quant_agent.db (WAL)              v
    ^                              Alpaca Paper (read endpoints only)
    | writer (unaffected)
    |
main.py / TradingPipeline (unchanged, unaware src/api/ exists)
```

## Why FastAPI + uvicorn

Recommended by a Wave-0 inspection subagent and adopted: `pydantic>=2.7.0`
is already a direct dependency (reused for response models — no extra
serialization layer), the async-only-in-a-separate-process constraint costs
nothing because the trading process is a synchronous `BlockingScheduler`
with no asyncio anywhere, and FastAPI's generated OpenAPI schema gives Stage
3's React/Vite frontend a stable, typed, auto-documented contract at zero
extra authoring cost. Two new dependencies (`fastapi`, `uvicorn`), added as
an **optional** `pyproject.toml` extra (`pip install -e '.[api]'`) — a
trading-only install (`pip install -e .`) does not pull them in.

## Isolation architecture

Two structurally separate read paths, matching the brief's explicit
instruction to keep broker-live reads apart from canonical historical SQLite
reads:

- **`src/api/broker_reads.py`** — the only file that constructs
  `AlpacaBroker` (from `src.execution.broker`, reused for its two pre-existing
  read-only methods, `get_account()`/`get_positions()`) and the only file
  that calls the Alpaca SDK's `client.get_orders(...)` directly for order
  listing (no such general-purpose read existed before Stage 2; it was added
  here rather than in `broker.py`, keeping 100% of Stage 2's new code inside
  `src/api/`). Every function here is designed to never raise — broker
  failures surface as an `error` field on the response.
- **`src/api/db_reads.py`** — never imports or instantiates
  `src.storage.db.Database` (the trading process's read/WRITE class).
  Opens its own independent `sqlite3.connect(f"file:{path}?mode=ro",
  uri=True)` connection per call — a structural, OS-enforced guarantee that
  a write attempt fails at the SQLite layer, not just by code-review
  convention. Safe under concurrent access because the trading process
  already runs the DB in WAL mode (`src/storage/db.py:initialize()`),
  designed for exactly this: a second reader while the writer holds the
  file.

`src/api/server.py` adds two app-level, belt-and-suspenders guarantees on
top of both routers only ever registering `@router.get(...)`: a
`GetOnlyMiddleware` that rejects any non-GET/HEAD/OPTIONS request with 405
before it reaches any handler, and a global exception handler that turns any
unhandled exception into a plain `{"detail": "internal error"}` 500 rather
than a leaking traceback (`HTTPException(404, ...)` responses are
unaffected — Starlette routes the generic-`Exception` handler through
`ServerErrorMiddleware`'s separate catch-all, not through
`ExceptionMiddleware`'s more specific `HTTPException` handling).

`src/api/deps.py` deliberately never hands a full `AppConfig`/`ApiKeysConfig`
object to a route handler — only narrow accessor functions
(`get_db_path()`, `get_alpaca_paper()`, `get_alpaca_credentials()`,
`get_agent_model()`, `get_agent_provider()`). Every response is a typed
Pydantic model (`src/api/schemas.py`), never a raw `dict(row)` passthrough,
so the response *shape* structurally has nowhere to carry a secret.

Verified by `tests/test_api_safety.py` (30 static/AST-level tests),
`tests/test_api_no_secrets.py` (schema field-name blocklist + live sentinel
sweep), `tests/test_api_db_concurrency.py` (concurrent read/write load +
`PRAGMA integrity_check`), and `tests/test_api_isolation.py` (starts the API
as a real separate OS process, kills it, proves an ordinary trading DB write
succeeds identically before/after).

## Endpoint contract

| Route | Source | Notes |
|---|---|---|
| `GET /health` | derived | `db_reachable`, `broker_reachable` (`None` = not configured, distinct from `False` = down), `paper`, `sessions_logged_today`, best-effort `last_run_files`/`session_lock_active` from `~/.cache/quant-agent/` (read-only stat, never fabricated — `None` on anything unknown) |
| `GET /account` | broker-live + `daily_pnl` history | `cash`/`portfolio_value`/`last_equity` from `AlpacaBroker.get_account()`; `daily_pnl`/`daily_pnl_pct` computed here (never divides by zero); `history` from the `daily_pnl` table |
| `GET /positions` | broker-live | `AlpacaBroker.get_positions()` |
| `GET /orders?status=open\|closed\|all&limit=` | broker-live | new read-only `client.get_orders(...)` wrapper; per-field-defensive parsing so one malformed order can't 500 the whole response |
| `GET /trades?symbol=&run_id=&decision_id=&today_only=&executed_only=&limit=` | SQLite `trades` | |
| `GET /runs?limit=` | SQLite `agent_logs` (derived) | per-`run_id` summary: agent count, decision_id, total cost (`None` if any call's cost is unknown — matches `Database.sum_session_cost`'s convention) |
| `GET /runs/{run_id}` | SQLite `agent_logs` + `trades` | full research→PM→RM→trade reconstruction for one run; `hard_risk_block_recorded` always `False` — see Known limitation below |
| `GET /decisions/{decision_id}` | SQLite `agent_logs` + `trades` | PM proposal + RM verdict + resulting trade(s) for one `decision_id` |
| `GET /agents` | config (static) | roster: name, role, configured model/provider |
| `GET /agents/{agent_name}` | config + SQLite `agent_logs` | roster entry + recent call history (full prompt/response, tokens, cost, latency, status) |
| `GET /reflections?limit=` | SQLite `insights` + filesystem | recent evening insights; `meta_periods` is existence-only (`data/evolution/{period}/{digest,reflection,proposed_edits}.json` presence, contents never parsed at this layer) |

Names are drawn directly from Wave-0 canonical-source inspection, not
invented — every route maps to a pre-existing table/broker method, plus one
new but narrowly-scoped read helper (`client.get_orders(...)` for
open/recent orders — no general "list all orders" read existed before).

## Known limitation — deterministic hard-risk gate rejections are not persisted

Verified during Wave-0 inspection (not assumed): when
`TradingPipeline._filter_hard_risk_decisions(...)` blocks **every** candidate
in a run before it ever reaches `risk_manager`, `RiskStage.run()` returns
early with an in-memory `{"status": "hard_risk_block", "reason": ...}` dict.
That reason reaches a log line and (via the noise policy) a Telegram push —
**no row of any kind, in any table, records which hard-risk rule fired.**
This is a genuine, structural gap in "deterministic accepted/rejected
decision" reconstruction, not a formatting inconvenience — there is nothing
to parse from a JSON blob because nothing was ever written.

Per the Stage 2 brief's schema-discipline instruction ("If you discover a
remaining schema gap, STOP and explain it before introducing broad
persistence changes"), Stage 2 does **not** add new persistence to close
this. `RunDetailResponse.hard_risk_block_recorded` is hardcoded `False`
rather than guessed from an empty trades list. Closing this (if ever wanted)
would mean persisting `RiskViolation` objects somewhere queryable — a
schema change squarely outside Stage 2's bounded scope, left for a future
stage to propose explicitly.

A **partial** hard-risk block (some candidates blocked, RM still reached for
the remainder) is fully reconstructable — the RM's `agent_logs.full_response`
carries its own reasoning, and blocked-vs-allowed is inferable from which
symbols made it to `trades`.

## Deliberately out of Stage 2 scope

- No `/candidates` or `/watchlist` endpoint. The only existing read
  (`TradingPipeline._build_watchlist_candidates`) lives on the pipeline
  object itself (`src/pipeline.py`, 7000+ lines) — importing it would pull
  `src/api/` into the trading-execution import graph, violating the
  isolation boundary this stage exists to establish. Not in the Stage 2
  "Data contract requirements" list either. Left for a future stage to
  decide how to expose without that coupling.
- Meta Reflector `digest.json`/`reflection.json`/`proposed_edits.json`
  contents are not parsed or summarized — `/reflections` reports only
  which files exist per period. These are LLM-generated free text meant for
  human reading, not this layer's concern (Stage 7 — Learning Center —
  remains BLOCKED and out of scope here).
- No journal UI, no full-text search index, no NL→SQL. Stage 2 exposes the
  canonical records Stage 5 would derive a journal/search index from; it
  does not build that index.

## Running it

```bash
pip install -e '.[api]'          # adds fastapi + uvicorn on top of the base install
python -m src.api                # binds 127.0.0.1:8800 by default
# or: uvicorn src.api.server:app --host 127.0.0.1 --port 8800
```

Binds to loopback by default (`QUANT_AGENT_API_HOST`/`QUANT_AGENT_API_PORT`
env vars override) — `DECISIONS.md` #29 says initial remote access should
prefer Tailscale/private networking over a public bind. `main.py` and
`src/scheduler.py` have zero awareness this package exists; killing this
process, or never starting it, has no effect on any trading session.
