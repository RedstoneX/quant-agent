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

Stage 4 additive endpoints: `/runs/{run_id}/candidates` (distinct symbols
considered in a run — union of symbol-scoped `specialist_evidence` and
`trades`), `/runs/{run_id}/candidates/{symbol}` (per-candidate fidelity:
symbol-specific tech/earnings/news evidence, clearly labeled broader
macro/news context, PM target/proposed order, RM verdict/modification, the
resulting trade(s), typed pipeline lifecycle events, and a computed
disagreement/consensus summary). Distinct
from `/candidates` (an unrelated Stage 2 universe-expansion watchlist
concept over `insights`, unchanged).

Stage 5 additive endpoints: `/journal/dates` (ET trading days with journal
data, newest first), `/journal/{date}` (day-level read-only aggregation:
equity snapshot, evening reflection, that day's runs, trades, and
candidate symbols — 404 when the date has no data), `/search?q=...`
(parameterized `LIKE` search over `trades`/`agent_logs`; the search term
is always a bound parameter, never interpolated into SQL text — the
endpoint cannot accept or generate arbitrary SQL). All three are read-only
aggregations over existing/Stage-4 tables; none introduce new
authoritative state.

When the deterministic gate blocks every candidate, the pipeline writes one additive forensic `agent_logs` row with `agent_name="risk_gate"`. It is not an LLM agent and does not alter risk calculations, eligibility, execution or broker behavior. Run/decision reads use it to reconstruct full hard-risk blocks; old databases simply lack pre-change rows.

Limits:
- no API writes;
- no authoritative UI/journal/search storage;
- `/reflections` does not parse Meta Reflector free text;
- `/candidates` exposes the existing persisted watch/universe-expansion concept, not a new intraday candidate engine.

Accepted full suite at Checkpoint C: **1530 passed, 0 failed**.
Detailed acceptance evidence remains in Git history. The last pre-ultra-lean working-tree snapshot is commit `02e20e6ac1c5c7e65b7f512f76c568328c990e3c`.

Stage 4–5 (specialist evidence, journal, forensic search) are externally
accepted — full suite **1558 passed, 0 failed**. Per
`.claude/rules/frontend-verification.md`, every cockpit UI acceptance pass
must be browser/runtime verified and ships a representative screenshot
set under `docs/verification/<stage>/`; see `docs/verification/stage-4-5/`
for this tranche's evidence.

## Stage 6 — liquidity breakdown, position direction, decision funnel

Additive, read-only aggregation over existing data — no new authoritative
state, no broker-write surface, no new external dependency.

- `/account`'s `liquidity` field (`LiquidityBreakdown`) separates raw
  broker cash from the cash-sweep vehicle's parked market value, the
  configured reserve floor, and deployable cash — so the sweep vehicle
  (e.g. SGOV) can never present like an ordinary position or an invented
  risk posture (2026-08-18 soak finding). Computed from `read_account()` +
  `read_positions()` + narrow `cash_sweep` config accessors
  (`src.api.deps.get_cash_sweep_*`); `None` whenever the underlying
  account/config read fails, never fabricated.
- `/positions` items gain `is_cash_equivalent` (true only for the
  configured sweep symbol) and `direction`
  (`"long" | "bearish_hedge" | "cash_equivalent"`). `direction` labels an
  inverse ETF already in the trading universe (`SH`/`SDS`/`PSQ`/`SQQQ`,
  `src.api.deps.INVERSE_ETF_SYMBOLS`) — display labeling only, computes no
  exposure/sizing/risk math, and is a hand-kept display-only duplicate of
  `src/risk/rules.py::_ETF_LEVERAGE`'s negative-multiplier entries since
  `src/api` may never import `src.risk`.
- `GET /runs/{run_id}/funnel` (`RunFunnelResponse`): the structural
  decision funnel for one run — how many candidates were considered,
  reached a PM target, reached a PM proposed order, and executed; whether
  a bearish-hedge candidate was considered; the deterministic
  hard-risk-block flag; and the run-scoped PM reasoning / AI Risk verdict
  / macro regime context (quoted verbatim from what those agents actually
  wrote, never a Mission-Control-authored guess about "why"). Answers
  "why did it trade, or why not?" from one call instead of the client
  opening every candidate individually. 404 only when the run itself
  doesn't exist (no `agent_logs`/`specialist_evidence`/`trades` row at
  all) — a run that legitimately considered zero candidates (e.g. a
  hard-risk-block before `risk_manager` ever ran) still returns 200 with
  `decision_state="hard_risk_block"` and an empty `candidates` list.

## Backend recovery — canonical lifecycle evidence (2026-08-25)

The existing `specialist_evidence` stream now also accepts validated
`kind="pipeline_event"` rows. These are additive lifecycle facts, not a new
memory or trading dependency. Symbol events carry `stage`, `outcome`, `reason`
and structured details for opportunity discovery, specialist success/failure,
PM proposal/omission/failure, Risk outcome, deterministic gate, funding, order
submission and protection. Existing evidence kinds remain the canonical agent
payloads; `trades` remains the canonical broker lifecycle row.

`GET /runs/{run_id}/funnel` and the per-candidate endpoint expose those events,
all matching trades, terminal `fill_status`/fill facts, protection outcome and
`realized_pnl`. Realized P&L is written only for confirmed exit fills with a
complete confirmed average-cost basis; an incomplete legacy basis remains
`null`. Thus submitted/filled/partially-filled/canceled/expired/rejected state,
position-management exits and measured outcomes are queryable without parsing
logs or model prose. API behavior remains read-only and non-authoritative.

## Stage 6 — price bars (chart panel)

`GET /prices/{symbol}?lookback_days=120&timeframe=1d`
(`PriceBarsResponse`): OHLCV bars for one symbol at `5m`, `15m`, `1h`,
or `1d`. Daily bars wrap `AlpacaBroker.get_bars`; intraday bars use the
timestamp-preserving, read-only
`AlpacaBroker.get_intraday_chart_bars` — Alpaca's market-data client
(`StockHistoricalDataClient.get_stock_bars`), a
distinct read-only client from the trading client `broker_reads.py`
already uses for account/positions/orders. Never places, cancels, or
references an order; degrades to `{"bars": [], "error": "..."}` on any
failure rather than raising. `5m` is scoped to today's ET session; the
other controls use bounded chart lookbacks. Powers the cockpit's price
chart panel and timestamp-aligned execution markers. During market hours
daily bars can run one session behind — see `/quotes` below for the true
current price this chart is deliberately never mistaken for.

## Mission Control data-truth / run-history / decision-explainability tranche (2026-08-21)

Operator-authorized, bounded read-side correctness tranche. Additive
only — no new authoritative state, no broker-write surface, no change to
`src/execution/broker.py` / `src/pipeline*.py` / `src/risk/*`.

`GET /quotes?symbols=AAPL,MSFT` (`LiveQuotesResponse`): current-session
quote facts (last trade price, previous session close, today's
still-forming session open/high/low) for up to 25 symbols in one call.
Wraps `AlpacaBroker.get_intraday_snapshots` — the SAME read-only bulk
Alpaca snapshot call (`StockHistoricalDataClient.get_stock_snapshot`)
the already-accepted, already-enabled intraday opportunity scanner uses;
no new broker capability. `as_of` is a Mission-Control fetch-time
timestamp (not a per-trade exchange timestamp), so a consumer can label
a quote "as of HH:MM:SS" rather than presenting it as unqualified live.
Distinct from `/positions`' broker-marked `current_price` (held
positions only) and `/prices`' historical daily bars — exists so the
cockpit chart/candidate context can show a genuinely current price
without ever implying a historical bar is "now" (the 2026-08-21 pricing-
reconciliation finding this tranche addresses).

`GET /journal/dates` now additionally unions in every ET calendar date
that has at least one recorded run (`agent_logs`), not only dates with an
evening reflection (`insights`) or an equity snapshot (`daily_pnl`) — a
day with real runs/candidates but neither of those was previously
invisible in the date list even though `/journal/{date}` already
rendered it correctly once selected directly. See `src/api/db_reads.py`
`get_journal_dates`.

`CandidateFunnelItem`'s `execution_skip_reason`/`execution_skip_detail`
(already returned by `GET /runs/{run_id}/funnel` since Stage 6) are now
also exposed in the frontend TypeScript contract
(`frontend/src/api/client.ts`) and surfaced in the Cockpit, the Candidate
Detail modal's outcome summary, and the Journal's per-candidate ledger
line — closing a frontend-only contract gap where a specific persisted
skip reason existed but the UI fell back to a generic "proposed but not
executed."

Frontend-only, no backend change: the professional Today Sessions strip
can pin the primary view (Candidates / Decision Room / chart) to an
explicit earlier run from the current ET day. Automatic mode truthfully
follows `bestPrimaryRunId` (shown as `AUTO / PRIMARY`, not mislabeled as
literal latest), and the operator can return to it explicitly. Poll
responses are generation-checked, so a new run or stale in-flight response
cannot silently replace a pinned inspection or mix context across runs.
