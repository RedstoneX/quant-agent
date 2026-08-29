"""Read-only history routes: trades / runs / decisions / agents / reflections.

Every handler here reads through `src.api.db_reads` (its own read-only
SQLite connection — see that module's docstring for the safety invariant)
and returns one of the typed Pydantic models from `src.api.schemas`, never
a raw `dict(row)` passthrough.

`HTTPException(404, ...)` is used deliberately for "no matching data"
cases and always propagates untouched — it is never caught by the
defensive `except Exception` blocks below, which exist only to turn a
genuine unexpected read failure into a plain 500 (a global handler is
added later by the orchestrator) rather than swallowing it into a
misleading 200.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.api import db_reads, deps
from src.api.schemas import (
    AgentDetailResponse,
    AgentLogItem,
    AgentRosterItem,
    AgentsResponse,
    CandidateItem,
    CandidatesResponse,
    DecisionDetailResponse,
    MetaPeriodSummary,
    PositionHistoryResponse,
    ReflectionItem,
    ReflectionsResponse,
    RunDetailResponse,
    RunsResponse,
    RunSummary,
    TradeItem,
    TradesResponse,
)
from src.config import AGENT_NAMES

router = APIRouter()


@router.get("/trades", response_model=TradesResponse)
def get_trades(
    symbol: str | None = None,
    run_id: str | None = None,
    decision_id: str | None = None,
    today_only: bool = False,
    executed_only: bool = False,
    limit: int = 100,
) -> TradesResponse:
    rows = db_reads.get_trades(
        symbol=symbol,
        run_id=run_id,
        decision_id=decision_id,
        today_only=today_only,
        executed_only=executed_only,
        limit=limit,
    )
    trades = [TradeItem(**row) for row in rows]
    return TradesResponse(trades=trades, count=len(trades))


@router.get("/positions/{position_id}/history", response_model=PositionHistoryResponse)
def get_position_history(position_id: str) -> PositionHistoryResponse:
    """The full chain for one position (Phase 6, §6.2b) — the entry thesis
    and stop, each interim review decision, the exit reason, realized P&L,
    and hold duration. See `db_reads.get_position_history` for how the
    chain is assembled; distinct from the broker-live `/positions` in
    routes_live.py, which lists currently-held positions, not history."""
    detail = db_reads.get_position_history(position_id)
    if detail is None:
        raise HTTPException(404, "position not found")
    return PositionHistoryResponse(
        position_id=detail["position_id"],
        symbol=detail["symbol"],
        status=detail["status"],
        entry=TradeItem(**detail["entry"]),
        interim=[TradeItem(**row) for row in detail["interim"]],
        exit=TradeItem(**detail["exit"]) if detail["exit"] is not None else None,
        realized_pnl_total=detail["realized_pnl_total"],
        realized_pnl_partial=detail["realized_pnl_partial"],
        hold_days=detail["hold_days"],
        trade_count=detail["trade_count"],
    )


@router.get("/runs", response_model=RunsResponse)
def get_runs(limit: int = 20) -> RunsResponse:
    rows = db_reads.get_recent_runs(limit=limit)
    return RunsResponse(runs=[RunSummary(**row) for row in rows])


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
def get_run_detail(run_id: str) -> RunDetailResponse:
    detail = db_reads.get_run_detail(run_id)
    if not detail["agent_logs"] and not detail["trades"]:
        raise HTTPException(404, "run not found")
    # Stage 2 Checkpoint C: True iff the forensic "risk_gate" sentinel row
    # is present — see RunDetailResponse.hard_risk_block_recorded docstring
    # in src/api/schemas.py. Computed from the already-fetched agent_logs,
    # never fabricated.
    hard_risk_block_recorded = any(
        row.get("agent_name") == "risk_gate" for row in detail["agent_logs"]
    )
    return RunDetailResponse(
        run_id=run_id,
        agent_logs=[AgentLogItem(**row) for row in detail["agent_logs"]],
        trades=[TradeItem(**row) for row in detail["trades"]],
        decision_id=detail["decision_id"],
        total_cost_usd=detail["total_cost_usd"],
        hard_risk_block_recorded=hard_risk_block_recorded,
    )


@router.get("/decisions/{decision_id}", response_model=DecisionDetailResponse)
def get_decision_detail(decision_id: str) -> DecisionDetailResponse:
    detail = db_reads.get_decision_detail(decision_id)
    if (
        detail["portfolio_manager"] is None
        and detail["risk_manager"] is None
        and detail["hard_risk_block"] is None
        and not detail["trades"]
    ):
        raise HTTPException(404, "decision not found")
    return DecisionDetailResponse(
        decision_id=decision_id,
        portfolio_manager=(
            AgentLogItem(**detail["portfolio_manager"])
            if detail["portfolio_manager"] is not None
            else None
        ),
        risk_manager=(
            AgentLogItem(**detail["risk_manager"])
            if detail["risk_manager"] is not None
            else None
        ),
        hard_risk_block=(
            AgentLogItem(**detail["hard_risk_block"])
            if detail["hard_risk_block"] is not None
            else None
        ),
        trades=[TradeItem(**row) for row in detail["trades"]],
    )


@router.get("/candidates", response_model=CandidatesResponse)
def get_candidates(lookback_days: int = 30) -> CandidatesResponse:
    """Symbols the evening analyst has repeatedly flagged as `add`/`watch`
    for universe expansion — see `src.api.db_reads.get_watchlist_candidates`
    and `src.watchlist_candidates` (Stage 2 Checkpoint C candidates gap)."""
    rows = db_reads.get_watchlist_candidates(lookback_days=lookback_days)
    return CandidatesResponse(
        candidates=[CandidateItem(**row) for row in rows],
        lookback_days=lookback_days,
    )


@router.get("/agents", response_model=AgentsResponse)
def get_agents() -> AgentsResponse:
    roster = deps.agent_roster()
    return AgentsResponse(agents=[AgentRosterItem(**row) for row in roster])


@router.get("/agents/{agent_name}", response_model=AgentDetailResponse)
def get_agent_detail(agent_name: str) -> AgentDetailResponse:
    if agent_name not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    recent_calls = db_reads.get_recent_agent_logs(agent_name, limit=20)
    return AgentDetailResponse(
        agent_name=agent_name,
        role=deps.AGENT_ROLES.get(agent_name, ""),
        configured_model=deps.get_agent_model(agent_name),
        configured_provider=deps.get_agent_provider(agent_name),
        recent_calls=[AgentLogItem(**row) for row in recent_calls],
    )


@router.get("/reflections", response_model=ReflectionsResponse)
def get_reflections(limit: int = 7) -> ReflectionsResponse:
    insights = db_reads.get_recent_insights(limit=limit)
    meta_periods = db_reads.list_meta_periods()
    return ReflectionsResponse(
        insights=[ReflectionItem(**row) for row in insights],
        meta_periods=[MetaPeriodSummary(**row) for row in meta_periods],
    )
