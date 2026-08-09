"""Stage 4 — per-candidate specialist evidence routes.

Reads exclusively through `src.api.db_reads` (independent read-only SQLite
connection — see that module's docstring for the safety invariant). Never
imports `src.pipeline` / `src.pipeline_stages` / `src.risk` or the
write-capable `Database` class — same isolation invariant enforced for
`routes_history.py` by `tests/test_api_safety.py`.

Every `specialist_evidence.evidence_json` value re-hydrated here was
written as the `model_dump_json()` (or `json.dumps(validated.model_dump())`
for earnings) of an ALREADY-VALIDATED Pydantic object at write time (see
`src/pipeline_stages.py::_persist_evidence` call sites) — never raw LLM
prose. Re-parsing it back into the same Pydantic type below is re-hydration
of trusted structured data, not "reconstructing canonical structured
evidence by parsing raw LLM blobs" (the boundary docs/WORK.md's Stage 4
section forbids doing in the CLIENT). A malformed/legacy row degrades that
one field to `None` (`_validate`) rather than ever raising a 500 — "unknown
stays unknown", never fabricated.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from src.api import db_reads
from src.api.schemas import (
    CandidateDetailResponse,
    ConsensusSignal,
    ConsensusSummary,
    MacroBroaderContext,
    NewsBroaderContext,
    PmReasoning,
    RiskManagerVerdict,
    RunCandidatesResponse,
    TradeItem,
)
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

router = APIRouter()

_TECH_DIRECTION = {
    "strong_buy": "bullish", "buy": "bullish",
    "neutral": "neutral",
    "sell": "bearish", "strong_sell": "bearish",
}


def _parse_evidence(row: dict):
    try:
        return json.loads(row["evidence_json"])
    except (KeyError, TypeError, ValueError):
        return None


def _validate(model_cls, data):
    """Re-hydrate already-validated JSON back into its Pydantic type.
    Degrades to None on any failure — never raises, never fabricates."""
    if data is None:
        return None
    try:
        return model_cls.model_validate(data)
    except Exception:  # noqa: BLE001 — a malformed/legacy row is a display gap, not a 500
        return None


def _find(rows: list[dict], agent_name: str, kind: str) -> dict | None:
    for row in rows:
        if row["agent_name"] == agent_name and row["kind"] == kind:
            return row
    return None


@router.get("/runs/{run_id}/candidates", response_model=RunCandidatesResponse)
def get_run_candidates(run_id: str) -> RunCandidatesResponse:
    """Symbols considered in this run — union of symbol-scoped specialist
    evidence and executed/attempted trades. See
    `src.api.db_reads.get_run_candidates`."""
    return RunCandidatesResponse(run_id=run_id, candidates=db_reads.get_run_candidates(run_id))


@router.get("/runs/{run_id}/candidates/{symbol}", response_model=CandidateDetailResponse)
def get_candidate_detail(run_id: str, symbol: str) -> CandidateDetailResponse:
    """Per-candidate fidelity: everything Mission Control has about one
    symbol within one run, preserving natural scope. Follows PM proposal ->
    AI Risk response/modification -> deterministic outcome without the
    client re-parsing raw agent output."""
    symbol = symbol.strip().upper()
    symbol_rows = db_reads.get_specialist_evidence(run_id=run_id, symbol=symbol)
    trades = db_reads.get_trades(run_id=run_id, symbol=symbol, limit=1)

    # 404 when THIS symbol wasn't actually a candidate in this run (no
    # symbol-scoped evidence, no trade) — run-scoped context (macro/news/PM
    # reasoning/RM verdict) existing for the run does not by itself make an
    # arbitrary symbol a valid candidate. Also covers "run doesn't exist".
    if not symbol_rows and not trades:
        raise HTTPException(404, "symbol was not a candidate in this run")

    run_rows = db_reads.get_specialist_evidence(run_id=run_id, scope="run")

    decision_id = None
    for row in symbol_rows + run_rows:
        if row.get("decision_id"):
            decision_id = row["decision_id"]
            break

    tech_row = _find(symbol_rows, "tech_analyst", "analysis")
    tech = _validate(TechAnalysisResult, _parse_evidence(tech_row)) if tech_row else None

    earnings_row = _find(symbol_rows, "earnings_analyst", "analysis")
    earnings = _validate(EarningsAnalysis, _parse_evidence(earnings_row)) if earnings_row else None

    target_row = _find(symbol_rows, "portfolio_manager", "target")
    pm_target = _validate(TargetPosition, _parse_evidence(target_row)) if target_row else None

    proposed_row = _find(symbol_rows, "portfolio_manager", "proposed_order")
    pm_proposed_order = (
        _validate(TradeDecision, _parse_evidence(proposed_row)) if proposed_row else None
    )

    mod_row = _find(symbol_rows, "risk_manager", "modification")
    risk_modification = (
        _validate(RiskModification, _parse_evidence(mod_row)) if mod_row else None
    )

    pm_reasoning = None
    reasoning_row = _find(run_rows, "portfolio_manager", "reasoning")
    if reasoning_row:
        data = _parse_evidence(reasoning_row) or {}
        pm_reasoning = PmReasoning(
            portfolio_view=data.get("portfolio_view"),
            reasoning_chain=_validate(ReasoningChain, data.get("reasoning_chain")),
            timestamp=reasoning_row.get("timestamp"),
        )

    risk_verdict = None
    verdict_row = _find(run_rows, "risk_manager", "verdict")
    if verdict_row:
        risk_verdict = RiskManagerVerdict(
            verdict=_validate(RiskVerdict, _parse_evidence(verdict_row)),
            timestamp=verdict_row.get("timestamp"),
        )

    macro_context = None
    macro_row = _find(run_rows, "macro_analyst", "analysis")
    if macro_row:
        ma = _validate(MacroAnalysis, _parse_evidence(macro_row))
        if ma is not None:
            macro_context = MacroBroaderContext(
                regime=ma.regime, equity_outlook=ma.equity_outlook,
                confidence=ma.confidence, summary=ma.summary,
                sector_guidance=[g.model_dump() for g in ma.sector_guidance],
                timestamp=macro_row.get("timestamp"),
            )

    news_symbol: list[dict] = []
    news_context = None
    news_row = _find(run_rows, "news_analyst", "analysis")
    if news_row:
        ni = _validate(NewsIntelligenceReport, _parse_evidence(news_row))
        if ni is not None:
            news_symbol = [item.model_dump() for item in ni.stock_news.get(symbol, [])]
            relevant_changes = [
                sc.model_dump() for sc in ni.state_changes
                if symbol in (sc.affected_symbols or [])
            ]
            news_context = NewsBroaderContext(
                market_sentiment=ni.market_sentiment, confidence=ni.confidence,
                pm_briefing=ni.pm_briefing,
                era_themes=list(ni.macro_narrative.era_themes),
                current_regime=ni.macro_narrative.current_regime,
                relevant_state_changes=relevant_changes,
                timestamp=news_row.get("timestamp"),
            )

    trade = TradeItem(**trades[0]) if trades else None

    signals: list[ConsensusSignal] = []
    if tech is not None:
        signals.append(ConsensusSignal(
            source="tech_analyst",
            direction=_TECH_DIRECTION.get(tech.rating, "neutral"),
            detail=tech.reasoning,
        ))
    if earnings is not None:
        signals.append(ConsensusSignal(
            source="earnings_analyst",
            direction=earnings.investment_implications.sentiment,
            detail=earnings.investment_implications.key_thesis,
        ))
    for item in news_symbol:
        signals.append(ConsensusSignal(
            source="news_analyst",
            direction=item.get("sentiment", "neutral"),
            detail=item.get("headline", ""),
        ))

    if len(signals) < 2:
        agreement = "insufficient_data"
    else:
        directions = {s.direction for s in signals if s.direction != "neutral"}
        agreement = "aligned" if len(directions) <= 1 else "mixed"

    return CandidateDetailResponse(
        run_id=run_id, symbol=symbol, decision_id=decision_id,
        tech=tech, earnings=earnings, news_symbol=news_symbol,
        macro_context=macro_context, news_context=news_context,
        pm_reasoning=pm_reasoning, pm_target=pm_target,
        pm_proposed_order=pm_proposed_order, risk_verdict=risk_verdict,
        risk_modification=risk_modification, trade=trade,
        consensus=ConsensusSummary(signals=signals, agreement=agreement),
    )
