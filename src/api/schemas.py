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

# Stage 4: reused directly as typed sub-fields of CandidateDetailResponse
# below. `src/api` importing `src.models` is safe under the Stage 2
# isolation invariant (tests/test_api_safety.py only forbids
# src.pipeline / src.pipeline_stages / src.risk / the write-capable
# Database class) — these are pure Pydantic value objects with no
# execution/trading behavior. Reusing them (instead of hand-duplicating
# near-identical shapes) means CandidateDetailResponse can re-hydrate
# `specialist_evidence.evidence_json` — itself always the
# `model_dump_json()` of one of these same validated objects, never raw
# LLM prose — with real type/field validation instead of an untyped dict.
from src.models import (
    EarningsAnalysis,
    MacroAnalysis,
    NewsIntelligenceReport,
    ReasoningChain,
    RiskModification,
    RiskVerdict,
    TargetPosition,
    TechAnalysisResult,
    TradeDecision,
)


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


class LiquidityBreakdown(BaseModel):
    """Honest split of `AccountResponse.cash` so raw cash, cash parked in
    the sweep vehicle, and the configured reserve floor are never conflated
    into one ambiguous number (docs/STATE.md 2026-08-18 soak finding: SGOV
    must never present like an ordinary position or invented risk posture).
    Any field is None when the underlying account/positions read failed —
    never fabricated from a partial read."""
    sweep_enabled: bool = False
    sweep_symbol: str | None = None
    raw_cash: float | None = None            # broker cash, includes the reserve
    sweep_parked_value: float | None = None  # market value of the held sweep vehicle, 0 if none
    reserve_usd: float | None = None         # config reserve_pct% of portfolio_value
    deployable_cash: float | None = None     # max(raw_cash - reserve_usd, 0)
    total_liquidity: float | None = None     # raw_cash + sweep_parked_value


class AccountResponse(BaseModel):
    cash: float | None = None
    portfolio_value: float | None = None
    last_equity: float | None = None
    daily_pnl: float | None = None       # portfolio_value - last_equity (computed here)
    daily_pnl_pct: float | None = None
    paper: bool | None = None
    source: str = "alpaca_live"
    history: list[DailyPnlPoint] = []    # recent daily_pnl table rows, newest first
    liquidity: LiquidityBreakdown | None = None
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
    # True only for the configured cash-sweep vehicle (e.g. SGOV) — parked
    # idle cash, never a Portfolio Manager thesis. See LiquidityBreakdown.
    is_cash_equivalent: bool = False
    # "long" (ordinary equity/ETF) | "bearish_hedge" (an inverse ETF already
    # in the trading universe — SH/SDS/PSQ/SQQQ) | "cash_equivalent" (the
    # sweep vehicle). Display labeling only; computes no exposure/risk math.
    direction: str = "long"


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
# /prices/{symbol}
# ---------------------------------------------------------------------------

class PriceBar(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class PriceBarsResponse(BaseModel):
    symbol: str
    bars: list[PriceBar] = []
    source: str = "alpaca_market_data"
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
# /runs/{run_id}/candidates, /runs/{run_id}/candidates/{symbol}  (Stage 4)
# ---------------------------------------------------------------------------

class RunCandidatesResponse(BaseModel):
    run_id: str
    candidates: list[str] = []


class PmReasoning(BaseModel):
    """Run-scoped PM reasoning_chain + portfolio_view — the "why", separate
    from the per-symbol `target`/`proposed_order` rows (the "what")."""
    portfolio_view: str | None = None
    reasoning_chain: ReasoningChain | None = None
    timestamp: str | None = None


class NewsBroaderContext(BaseModel):
    """Run/theme-scoped news context — deliberately NOT attributed to the
    candidate symbol (see ConsensusSummary for the symbol-specific slice
    of the same NewsIntelligenceReport, extracted separately below)."""
    market_sentiment: str | None = None
    confidence: str | None = None
    pm_briefing: str | None = None
    era_themes: list[str] = []
    current_regime: str | None = None
    # state_changes whose affected_symbols includes this candidate — a real
    # per-symbol citation the source itself made, not an inference.
    relevant_state_changes: list[dict] = []
    timestamp: str | None = None


class MacroBroaderContext(BaseModel):
    """Run-scoped macro regime context. sector_guidance is the FULL list
    exactly as macro_analyst emitted it — never filtered/attributed to this
    symbol's sector, since specialist_evidence carries no sector mapping
    and inventing one here would violate the Stage 4 boundary against
    manufacturing per-symbol macro conclusions from a run/sector-scoped
    source."""
    regime: str | None = None
    equity_outlook: str | None = None
    confidence: str | None = None
    summary: str | None = None
    sector_guidance: list[dict] = []
    timestamp: str | None = None


class RiskManagerVerdict(BaseModel):
    """Run-scoped RM verdict — approved/rejected + full reasoning, separate
    from any per-symbol `risk_modification` row."""
    verdict: RiskVerdict | None = None
    timestamp: str | None = None


class ConsensusSignal(BaseModel):
    source: str        # "tech_analyst" | "earnings_analyst" | "news_analyst"
    direction: str      # "bullish" | "bearish" | "neutral"
    detail: str = ""


class ConsensusSummary(BaseModel):
    signals: list[ConsensusSignal] = []
    # "aligned" (>=2 signals, exactly one non-neutral direction among them)
    # | "mixed" (non-neutral signals disagree) | "no_directional_signal"
    # (>=2 signals fired but every one was neutral — NOT the same as
    # "aligned"; nobody actually committed to a direction) |
    # "insufficient_data" (0-1 signals available). Never invents a signal
    # source that didn't actually fire this run, and never reports
    # "aligned" for a degenerate all-neutral case.
    agreement: str = "insufficient_data"


class CandidateDetailResponse(BaseModel):
    """Stage 4 per-candidate fidelity: everything Mission Control has about
    one symbol within one run, preserving each source's natural scope
    (symbol-specific vs broader/run-scoped, clearly separated below) —
    follows PM proposal -> AI Risk response -> deterministic gate ->
    executed/rejected result without the client re-parsing raw agent
    output. requested/actual model+provider+cost+latency for any agent
    involved remain available via GET /runs/{run_id} (agent_logs), not
    duplicated here.
    """
    run_id: str
    symbol: str
    decision_id: str | None = None

    # Symbol-specific specialist evidence — None when that specialist
    # never covered this symbol this run (never fabricated).
    tech: TechAnalysisResult | None = None
    earnings: EarningsAnalysis | None = None
    # Only the entries from this run's NewsIntelligenceReport.stock_news
    # that are actually keyed to this symbol.
    news_symbol: list[dict] = []

    # Broader, explicitly-labeled run-scoped context (never symbol-specific).
    macro_context: MacroBroaderContext | None = None
    news_context: NewsBroaderContext | None = None

    # Decision chain: PM intent -> constructed order -> AI Risk modification
    # -> deterministic outcome (trade, if any reached execution).
    pm_reasoning: PmReasoning | None = None
    pm_target: TargetPosition | None = None
    pm_proposed_order: TradeDecision | None = None
    risk_verdict: RiskManagerVerdict | None = None
    risk_modification: RiskModification | None = None
    trade: TradeItem | None = None

    consensus: ConsensusSummary = ConsensusSummary()


# ---------------------------------------------------------------------------
# /runs/{run_id}/funnel — decision-funnel / "why no trade?" aggregation
# ---------------------------------------------------------------------------

class CandidateFunnelItem(BaseModel):
    """One candidate's progress through the decision chain this run —
    the structural facts only (did it reach each stage, what stage it
    stopped at), never a synthesized narrative."""
    symbol: str
    # "bullish" | "bearish" | "neutral" | "unknown" — from tech_analyst's
    # rating when available (see _TECH_DIRECTION), else "unknown". Purely
    # a display label; computes no exposure/risk math.
    direction: str = "unknown"
    is_bearish_hedge: bool = False  # SH/SDS/PSQ/SQQQ — inverse ETF already in the universe
    reached_pm_target: bool = False
    pm_target_weight_pct: float | None = None
    reached_proposed_order: bool = False
    proposed_action: str | None = None       # BUY | SELL | HOLD | None
    risk_modified: bool = False
    executed: bool = False
    trade_action: str | None = None


class RunFunnelResponse(BaseModel):
    """Stage 6 — structural decision funnel for one run, built to answer
    "why did it trade, or why not?" without requiring the operator to open
    every candidate individually. Every field is derived from existing
    specialist_evidence/trades rows already written by Stage 4; this
    introduces no new authoritative state. The PM/RM `reasoning`/`verdict`
    text below is quoted verbatim from what those agents actually wrote —
    never a Mission-Control-authored summary of "why," which could
    misrepresent the decision."""
    run_id: str
    session_prefix: str | None = None
    timestamp: str | None = None

    candidates: list[CandidateFunnelItem] = []
    candidates_considered: int = 0
    reached_pm_count: int = 0
    proposed_order_count: int = 0
    executed_count: int = 0
    bearish_hedge_considered: bool = False

    hard_risk_block: bool = False
    pm_reasoning: PmReasoning | None = None
    risk_verdict: RiskManagerVerdict | None = None
    macro_context: MacroBroaderContext | None = None

    # "executed" | "proposed_not_executed" | "hard_risk_block" |
    # "no_proposal" | "no_candidates" — the single structural fact the
    # dashboard's headline state badge renders; never invents a cause
    # beyond what candidates/pm_reasoning/risk_verdict/hard_risk_block
    # above actually show.
    decision_state: str = "no_candidates"


# ---------------------------------------------------------------------------
# /journal/dates, /journal/{date}, /search  (Stage 5)
# ---------------------------------------------------------------------------

class JournalDatesResponse(BaseModel):
    dates: list[str] = []


class JournalDayResponse(BaseModel):
    date: str
    has_data: bool = False
    daily_pnl: DailyPnlPoint | None = None
    reflection: ReflectionItem | None = None
    runs: list[RunSummary] = []
    trades: list[TradeItem] = []
    # Union of candidate symbols considered that day (specialist_evidence +
    # trades) — same natural-scope union as get_run_candidates, just
    # day-wide instead of per-run.
    candidates: list[str] = []


class SearchTradeHit(BaseModel):
    kind: str = "trade"
    id: int
    symbol: str
    action: str
    run_id: str | None = None
    decision_id: str | None = None
    timestamp: str | None = None
    reasoning: str | None = None


class SearchAgentLogHit(BaseModel):
    kind: str = "agent_log"
    id: int
    agent_name: str
    run_id: str | None = None
    decision_id: str | None = None
    timestamp: str | None = None
    model: str | None = None
    output_summary: str | None = None


class SearchResponse(BaseModel):
    query: str
    trades: list[SearchTradeHit] = []
    agent_logs: list[SearchAgentLogHit] = []


# ---------------------------------------------------------------------------
# Generic error envelope (used by exception handlers, not returned inline)
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    detail: str
