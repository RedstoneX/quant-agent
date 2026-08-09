# Mission Control API — Stage 2 (Thin Read-Only)

Status: **implemented 2026-08-09** on branch
`claude/stage-2-mission-control-api-4zpx7j`, **Checkpoint C completion slice
implemented 2026-08-09** on branch `claude/stage-2-checkpoint-c-fb1ip0`
(candidates endpoint + hard-risk-block forensic reconstruction — see
"Checkpoint C completion slice" below), awaiting Checkpoint C operator
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
| `GET /candidates?lookback_days=` | SQLite `insights.missed_opportunities_json` | symbols the evening analyst has repeatedly flagged `add`/`watch` for universe expansion — see "Checkpoint C completion slice" below |

Names are drawn directly from Wave-0 canonical-source inspection, not
invented — every route maps to a pre-existing table/broker method, plus one
new but narrowly-scoped read helper (`client.get_orders(...)` for
open/recent orders — no general "list all orders" read existed before).

## Checkpoint C completion slice (2026-08-09, branch `claude/stage-2-checkpoint-c-fb1ip0`)

Independent ChatGPT review of the implemented-but-unaccepted Stage 2 branch
found two Checkpoint C gaps. Both are resolved here, additively, without
reopening the rest of Stage 2's architecture:

**1. Candidates/watchlist API contract gap — resolved via extraction, not a
second engine.** The governed milestone text ("Expose only what is needed
for UI: account, positions, orders, trades, **candidates**, ...",
`docs/MILESTONES.md` Stage 2) did require candidates; the original
implementation's claim that it "was not in the Stage 2 brief's required
data contract" was incorrect and has been corrected here rather than
left standing. Repository inspection found the only existing candidate
read, `TradingPipeline._build_watchlist_candidates`
(symbols the evening analyst has repeatedly flagged `add`/`watch` for
universe expansion, aggregated from `insights.missed_opportunities_json`),
is a **pure function of already-persisted canonical data** — its only
dependency is `self.db.get_recent_insights(...)`, no broker, no execution
state, no other pipeline internals. The aggregation body was extracted
verbatim into a new zero-dependency module,
`src/watchlist_candidates.py:build_watchlist_candidates(rows, lookback_days)`
(stdlib `json` only — no `src.pipeline`, `src.pipeline_stages`, `src.risk`,
or `src.api` imports in either direction). `TradingPipeline._build_watchlist_candidates`
is now a thin fetch-then-aggregate wrapper around it (identical output,
covered by the pre-existing `tests/test_missed_opportunities.py` suite,
unchanged). `src/api/db_reads.py:get_watchlist_candidates()` calls the
existing read-only `get_recent_insights()` query and the same pure
aggregator, then `GET /candidates?lookback_days=` (default 30) serves it.
`TradingPipeline` is still never imported by `src/api/` — the isolation
boundary Stage 2 exists to establish is intact; only a genuinely pure
helper moved to a neutral location both sides can import.
This is a **derived/rebuildable read** over canonical `insights` rows, not
a second candidate-generation engine, and recomputes nothing about trading
decisions.

**2. Deterministic hard-risk rejection reconstruction gap — resolved via
one additive `agent_logs` row, no schema change.** The original finding
stands as accurately described below (now historical): when the
deterministic hard-risk gate blocked every candidate before `risk_manager`
ever ran, nothing was persisted. `TradingPipeline._persist_hard_risk_block`
(`src/pipeline.py`) now writes one forensic `agent_logs` row at both
`RiskStage.run()` early-return sites (`src/pipeline_stages.py`), using the
*existing* `Database.insert_agent_log` mechanism — no new table, no new
column, no change to hard-risk calculations, limits, eligibility,
execution semantics, or broker behavior. The row uses the sentinel
`agent_name="risk_gate"`, deliberately distinct from the real
`"risk_manager"` LLM agent name, so it can never be mistaken for (or
accidentally replayed as) an actual RM call: `scripts/replay_decision.py`
selects rows by exact `agent_name` match, and the fixed 9-name
`AGENT_NAMES` roster (`/agents`, `/agents/{agent_name}`) is unaffected
since it never enumerates `agent_logs` — `"risk_gate"` simply doesn't
match either. `cost_usd`/`tokens_used` are persisted as known-zero (`0`,
`0.0`), not `None` — no LLM call happened, so the cost is exactly zero,
not unknown, which keeps `Database.sum_session_cost`'s any-null-means-
unknown convention from nulling out an otherwise-fully-priced run's total.
The call is wrapped so a persistence failure can never affect the
already-made early-return risk decision.

`RunDetailResponse.hard_risk_block_recorded` is no longer hardcoded
`False` — `GET /runs/{run_id}` now computes it from whether the fetched
`agent_logs` includes a `agent_name="risk_gate"` row. `GET
/decisions/{decision_id}` gained a `hard_risk_block` field (an
`AgentLogItem | None`, populated instead of `risk_manager` in exactly this
case) via `db_reads.get_decision_detail()`'s new `agent_name = 'risk_gate'`
lookup. No existing field, route, or response shape was removed or
renamed — both additions are backward compatible with old SQLite DBs (the
`agent_logs` schema is unchanged; a pre-Checkpoint-C DB simply has zero
`risk_gate` rows and both endpoints behave exactly as before).

Regression tests: `tests/test_pipeline_stages.py` (4 new — the persistence
helper directly, its failure-swallowing behavior, and both `RiskStage.run()`
early-return call sites end-to-end) and `tests/test_api_contract.py` (5 new
— `/candidates` aggregation/empty/lookback-param, and `/runs/{run_id}` +
`/decisions/{decision_id}` reconstructing a fully-hard-risk-blocked run).
Full suite: **1530 passed, 0 failed** (1521 baseline + 9 new).

## Known limitation — deterministic hard-risk gate rejections (historical description, resolved above)

Verified during Wave-0 inspection (not assumed): when
`TradingPipeline._filter_hard_risk_decisions(...)` blocks **every** candidate
in a run before it ever reaches `risk_manager`, `RiskStage.run()` returns
early with an in-memory `{"status": "hard_risk_block", "reason": ...}` dict.
That reason reaches a log line and (via the noise policy) a Telegram push —
at Stage 2 sign-off, **no row of any kind, in any table, recorded which
hard-risk rule fired.** The Checkpoint C completion slice above closes this
via one additive `agent_logs` row (`agent_name="risk_gate"`); this section
is retained as an accurate record of the original finding rather than
deleted.

A **partial** hard-risk block (some candidates blocked, RM still reached for
the remainder) was, and remains, fully reconstructable — the RM's
`agent_logs.full_response` carries its own reasoning, and blocked-vs-allowed
is inferable from which symbols made it to `trades`.

## Stage 2 scope not addressed by the Checkpoint C completion slice

- Meta Reflector `digest.json`/`reflection.json`/`proposed_edits.json`
  contents are not parsed or summarized — `/reflections` reports only
  which files exist per period. These are LLM-generated free text meant for
  human reading, not this layer's concern (Stage 7 — Learning Center —
  remains BLOCKED and out of scope here).
- No journal UI, no full-text search index, no NL→SQL. Stage 2 exposes the
  canonical records Stage 5 would derive a journal/search index from; it
  does not build that index.
- `/candidates` exposes universe-expansion candidates (the only candidate
  concept with a canonical persisted read). It does not expose an
  intraday/pre-trade "buy candidates considered this run" concept — no
  such canonical record exists independent of `agent_logs.full_response`
  (the PM's own reasoning), which `/runs/{run_id}` and
  `/decisions/{decision_id}` already surface.

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
