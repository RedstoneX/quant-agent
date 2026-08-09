"""Pydantic response models for the Mission Control API.

Every route returns one of these models (never a raw `dict(row)` passthrough)
so the response shape is structurally frozen and, critically, structurally
incapable of carrying a secret: these models simply have no field that could
hold an API key, because none of the read paths that populate them ever touch
`ApiKeysConfig`. Stage 3 (a future consumer) gets a stable, typed contract for
free via FastAPI's generated OpenAPI schema.

Unknown/unavailable values are always `None`, never fabricated — see
CLAUDE.md/AGENTS.md "Health" and "Global criteria" sections.
"""

from __future__ import annotations

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str  # "ok" — the API process itself responded
    db_reachable: bool
    broker_reachable: bool | None = None  # None = not checked (e.g. no keys)
    paper: bool | None = None  # honest echo of config.alpaca.paper; never fabricated
    sessions_logged_today: list[str] = []  # run_id prefixes, e.g. ["run", "midday"]
    last_run_files: dict[str, str | None] = {}  # mode -> ISO mtime or None
    session_lock_active: bool | None = None  # best-effort process hint, not authoritative
    timestamp: str


# ---------------------------------------------------------------------------
# /account
# ---------------------------------------------------------------------------

class DailyPnlPoint(BaseModel):
    date: str
    total_value: float | None = None
    daily_pnl: float | None = None
    daily_return_pct: float | None = None
    equity_close: float | None = None


class AccountResponse(BaseModel):
    cash: float | None = None
    portfolio_value: float | None = None
    last_equity: float | None = None
    daily_pnl: float | None = None       # portfolio_value - last_equity (computed here)
    daily_pnl_pct: float | None = None
    paper: bool | None = None
    source: str = "alpaca_live"
    history: list[DailyPnlPoint] = []    # recent daily_pnl table rows, newest first
    error: str | None = None             # set (fields above null) when the broker read failed


# ---------------------------------------------------------------------------
# /positions
# ---------------------------------------------------------------------------

class PositionItem(BaseModel):
    symbol: str
    qty: float
    avg_entry: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_intraday_pnl: float | None = None
    sector: str | None = None


class PositionsResponse(BaseModel):
    positions: list[PositionItem] = []
    source: str = "alpaca_live"
    error: str | None = None


# ---------------------------------------------------------------------------
# /orders
# ---------------------------------------------------------------------------

class OrderItem(BaseModel):
    id: str
    symbol: str
    side: str | None = None
    qty: float | None = None
    order_type: str | None = None
    status: str | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    filled_qty: float | None = None
    filled_avg_price: float | None = None
    submitted_at: str | None = None
    filled_at: str | None = None


class OrdersResponse(BaseModel):
    orders: list[OrderItem] = []
    source: str = "alpaca_live"
    error: str | None = None


# ---------------------------------------------------------------------------
# /trades
# ---------------------------------------------------------------------------

class TradeItem(BaseModel):
    id: int
    symbol: str
    action: str
    qty: float | None = None
    price: float | None = None
    reasoning: str | None = None
    run_id: str | None = None
    decision_id: str | None = None
    broker_order_id: str | None = None
    fill_status: str | None = None
    fill_qty: float | None = None
    fill_price: float | None = None
    fill_reconciled_at: str | None = None
    timestamp: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None


class TradesResponse(BaseModel):
    trades: list[TradeItem] = []
    count: int = 0


# ---------------------------------------------------------------------------
# /runs, /runs/{run_id}, /decisions/{decision_id}
# ---------------------------------------------------------------------------

class AgentLogItem(BaseModel):
    id: int
    agent_name: str
    run_id: str | None = None
    decision_id: str | None = None
    timestamp: str | None = None
    input_summary: str | None = None
    input_message: str | None = None
    output_summary: str | None = None
    full_response: str | None = None
    model: str | None = None
    requested_provider: str | None = None
    requested_model: str | None = None
    actual_provider: str | None = None
    prompt_version: str | None = None
    tokens_used: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_s: float | None = None
    status: str | None = None
    finish_reason: str | None = None
    truncated: bool | None = None


class RunSummary(BaseModel):
    run_id: str
    session_prefix: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    agent_count: int = 0
    decision_id: str | None = None
    total_cost_usd: float | None = None


class RunsResponse(BaseModel):
    runs: list[RunSummary] = []


class RunDetailResponse(BaseModel):
    run_id: str
    agent_logs: list[AgentLogItem] = []
    trades: list[TradeItem] = []
    decision_id: str | None = None
    total_cost_usd: float | None = None
    # Stage 2 Checkpoint C: True when this run's `agent_logs` includes the
    # `agent_name="risk_gate"` forensic row `TradingPipeline._persist_hard_risk_block`
    # writes when the deterministic hard-risk gate blocked EVERY candidate
    # before risk_manager was ever called (see docs/architecture/
    # MISSION_CONTROL_API.md). Computed from `agent_logs`, never fabricated —
    # a run with no such row (the ordinary case) reports False.
    hard_risk_block_recorded: bool = False


class DecisionDetailResponse(BaseModel):
    decision_id: str
    portfolio_manager: AgentLogItem | None = None
    risk_manager: AgentLogItem | None = None
    # Stage 2 Checkpoint C: populated instead of `risk_manager` when the
    # deterministic hard-risk gate blocked every candidate for this decision
    # before risk_manager was ever called — the forensic `agent_name="risk_gate"`
    # `agent_logs` row (see `RunDetailResponse.hard_risk_block_recorded`).
    # `None` for every ordinary (RM-reached) decision.
    hard_risk_block: AgentLogItem | None = None
    trades: list[TradeItem] = []


# ---------------------------------------------------------------------------
# /candidates
# ---------------------------------------------------------------------------

class CandidateItem(BaseModel):
    symbol: str
    add_count: int = 0
    watch_count: int = 0
    total_flags: int = 0
    dates: list[str] = []          # ISO dates flagged, newest first
    themes: list[str] = []
    latest_reason: str = ""
    latest_miss_category: str = ""


class CandidatesResponse(BaseModel):
    candidates: list[CandidateItem] = []
    lookback_days: int = 30


# ---------------------------------------------------------------------------
# /agents, /agents/{agent_name}
# ---------------------------------------------------------------------------

class AgentRosterItem(BaseModel):
    agent_name: str
    role: str
    configured_model: str | None = None
    configured_provider: str | None = None  # resolved via resolve_provider(); None = prefix-inferred


class AgentsResponse(BaseModel):
    agents: list[AgentRosterItem] = []


class AgentDetailResponse(BaseModel):
    agent_name: str
    role: str
    configured_model: str | None = None
    configured_provider: str | None = None
    recent_calls: list[AgentLogItem] = []


# ---------------------------------------------------------------------------
# /reflections
# ---------------------------------------------------------------------------

class ReflectionItem(BaseModel):
    date: str
    tomorrow_outlook: str | None = None
    lessons: str | None = None
    suggested_actions: str | None = None
    risk_rating: str | None = None
    tomorrow_bias: str | None = None
    tomorrow_conviction: str | None = None
    tomorrow_key_risks: str | None = None
    sell_decisions_assessment: str | None = None
    sell_grades_json: str | None = None
    buy_grades_json: str | None = None
    missed_opportunities_json: str | None = None
    timestamp: str | None = None


class MetaPeriodSummary(BaseModel):
    period: str
    has_digest: bool = False
    has_reflection: bool = False
    has_proposed_edits: bool = False


class ReflectionsResponse(BaseModel):
    insights: list[ReflectionItem] = []
    meta_periods: list[MetaPeriodSummary] = []


# ---------------------------------------------------------------------------
# Generic error envelope (used by exception handlers, not returned inline)
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    detail: str
