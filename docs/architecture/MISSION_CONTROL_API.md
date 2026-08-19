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
resulting trade, and a computed disagreement/consensus summary). Distinct
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
