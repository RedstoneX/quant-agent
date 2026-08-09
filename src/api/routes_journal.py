"""Stage 5 — journal (day browsing) + forensic search routes.

Reads exclusively through `src.api.db_reads` (independent read-only SQLite
connection). Same isolation invariant as `routes_history.py` /
`routes_evidence.py`: never imports `src.pipeline` / `src.pipeline_stages`
/ `src.risk` or the write-capable `Database` class (tests/test_api_safety.py).

Both routes are read-only aggregations over existing/Stage-4 tables —
`daily_pnl`, `insights`, `agent_logs`, `trades`, `specialist_evidence`.
They introduce no new authoritative state and no second trading-memory
system (docs/OUTCOME.md hard constraint): losing this file or the process
serving it has zero effect on trading, and every field here is derived
from — never a substitute for — the canonical rows those tables already
hold.

Search (`/search`) is parameterized `LIKE` matching only
(`src.api.db_reads.search`) — the search term is always a bound SQL
parameter, never interpolated into query text, so this endpoint can never
accept or generate arbitrary SQL (docs/WORK.md Stage 5 boundary).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.api import db_reads
from src.api.schemas import (
    DailyPnlPoint,
    JournalDatesResponse,
    JournalDayResponse,
    ReflectionItem,
    RunSummary,
    SearchAgentLogHit,
    SearchResponse,
    SearchTradeHit,
    TradeItem,
)

router = APIRouter()


@router.get("/journal/dates", response_model=JournalDatesResponse)
def get_journal_dates(limit: int = 60) -> JournalDatesResponse:
    return JournalDatesResponse(dates=db_reads.get_journal_dates(limit=limit))


@router.get("/journal/{date}", response_model=JournalDayResponse)
def get_journal_day(date: str) -> JournalDayResponse:
    detail = db_reads.get_journal_day(date)
    has_data = bool(
        detail["daily_pnl"] or detail["insights"] or detail["runs"] or detail["trades"]
    )
    if not has_data:
        raise HTTPException(404, "no journal data for this date")
    return JournalDayResponse(
        date=date,
        has_data=True,
        daily_pnl=DailyPnlPoint(**detail["daily_pnl"]) if detail["daily_pnl"] else None,
        reflection=ReflectionItem(**detail["insights"]) if detail["insights"] else None,
        runs=[RunSummary(**r) for r in detail["runs"]],
        trades=[TradeItem(**t) for t in detail["trades"]],
        candidates=detail["candidates"],
    )


@router.get("/search", response_model=SearchResponse)
def search(q: str = "", limit: int = 50) -> SearchResponse:
    result = db_reads.search(q, limit=limit)
    return SearchResponse(
        query=q,
        trades=[
            SearchTradeHit(
                id=t["id"], symbol=t["symbol"], action=t["action"],
                run_id=t.get("run_id"), decision_id=t.get("decision_id"),
                timestamp=t.get("timestamp"), reasoning=t.get("reasoning"),
            )
            for t in result["trades"]
        ],
        agent_logs=[
            SearchAgentLogHit(
                id=a["id"], agent_name=a["agent_name"], run_id=a.get("run_id"),
                decision_id=a.get("decision_id"), timestamp=a.get("timestamp"),
                model=a.get("model"), output_summary=a.get("output_summary"),
            )
            for a in result["agent_logs"]
        ],
    )
