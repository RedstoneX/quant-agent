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
from src.api.deps import INVERSE_ETF_SYMBOLS
from src.api.schemas import (
    CandidateDetailResponse,
    CandidateFunnelItem,
    ConsensusSignal,
    ConsensusSummary,
    MacroBroaderContext,
    NewsBroaderContext,
    PmReasoning,
    RiskManagerVerdict,
    RunCandidatesResponse,
    RunFunnelResponse,
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


def _run_scoped_context(run_rows: list[dict]) -> tuple:
    """PM reasoning + AI Risk verdict + macro regime context from a run's
    scope="run" specialist_evidence rows — shared by the per-candidate
    (`get_candidate_detail`) and per-run (`get_run_funnel`) views so the
    same run-wide evidence is interpreted identically in both places."""
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

    return pm_reasoning, risk_verdict, macro_context


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

    pm_reasoning, risk_verdict, macro_context = _run_scoped_context(run_rows)

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
        if not directions:
            # >=2 signals fired but every one was "neutral" — a real,
            # common state (nobody committed to a direction), not
            # agreement. Reporting "aligned" here would claim consensus
            # that doesn't exist (Stage 4 boundary: never pretend every
            # agent emitted the same kind of signal).
            agreement = "no_directional_signal"
        elif len(directions) == 1:
            agreement = "aligned"
        else:
            agreement = "mixed"

    return CandidateDetailResponse(
        run_id=run_id, symbol=symbol, decision_id=decision_id,
        tech=tech, earnings=earnings, news_symbol=news_symbol,
        macro_context=macro_context, news_context=news_context,
        pm_reasoning=pm_reasoning, pm_target=pm_target,
        pm_proposed_order=pm_proposed_order, risk_verdict=risk_verdict,
        risk_modification=risk_modification, trade=trade,
        consensus=ConsensusSummary(signals=signals, agreement=agreement),
    )


@router.get("/runs/{run_id}/funnel", response_model=RunFunnelResponse)
def get_run_funnel(run_id: str) -> RunFunnelResponse:
    """Stage 6 — structural decision funnel for one run: every candidate's
    progress through specialist -> PM target -> PM proposed order -> AI
    Risk -> execution, plus the run-wide PM/RM/macro context, so the
    dashboard can answer "why did it trade, or why not?" without the
    client opening every candidate individually. Pure aggregation over
    the same Stage-4 tables `get_candidate_detail` reads — never a
    Mission-Control-authored guess about intent."""
    raw = db_reads.get_run_funnel(run_id)
    if not raw["run_exists"]:
        raise HTTPException(404, "run not found")

    evidence_rows = raw["specialist_evidence"]
    trades = raw["trades"]

    symbol_rows = [r for r in evidence_rows if r.get("scope") == "symbol" and r.get("symbol")]
    run_rows = [r for r in evidence_rows if r.get("scope") == "run"]

    by_symbol: dict[str, list[dict]] = {}
    for r in symbol_rows:
        by_symbol.setdefault(r["symbol"], []).append(r)
    first_trade_by_symbol: dict[str, dict] = {}
    for t in trades:
        first_trade_by_symbol.setdefault(t["symbol"], t)

    symbols = sorted(set(by_symbol) | set(first_trade_by_symbol))

    candidates: list[CandidateFunnelItem] = []
    bearish_hedge_considered = False
    reached_pm_count = 0
    proposed_order_count = 0
    executed_count = 0

    for sym in symbols:
        rows = by_symbol.get(sym, [])

        tech_row = _find(rows, "tech_analyst", "analysis")
        tech = _validate(TechAnalysisResult, _parse_evidence(tech_row)) if tech_row else None
        direction = _TECH_DIRECTION.get(tech.rating, "neutral") if tech is not None else "unknown"

        is_hedge = sym in INVERSE_ETF_SYMBOLS
        if is_hedge:
            bearish_hedge_considered = True

        target_row = _find(rows, "portfolio_manager", "target")
        pm_target = _validate(TargetPosition, _parse_evidence(target_row)) if target_row else None
        reached_target = pm_target is not None
        if reached_target:
            reached_pm_count += 1

        proposed_row = _find(rows, "portfolio_manager", "proposed_order")
        pm_proposed = _validate(TradeDecision, _parse_evidence(proposed_row)) if proposed_row else None
        reached_proposed = pm_proposed is not None
        if reached_proposed:
            proposed_order_count += 1

        risk_modified = _find(rows, "risk_manager", "modification") is not None

        skip_row = _find(rows, "execution", "execution_skip")
        skip = _parse_evidence(skip_row) if skip_row else None
        skip_reason = skip.get("reason") if isinstance(skip, dict) else None
        skip_detail = skip.get("detail") if isinstance(skip, dict) else None

        trade = first_trade_by_symbol.get(sym)
        executed = trade is not None and db_reads.is_executed_trade(trade)
        if executed:
            executed_count += 1

        candidates.append(CandidateFunnelItem(
            symbol=sym,
            direction=direction,
            is_bearish_hedge=is_hedge,
            reached_pm_target=reached_target,
            pm_target_weight_pct=pm_target.target_weight_pct if pm_target else None,
            reached_proposed_order=reached_proposed,
            proposed_action=pm_proposed.action if pm_proposed else None,
            risk_modified=risk_modified,
            executed=executed,
            trade_action=trade.get("action") if trade else None,
            execution_skip_reason=skip_reason,
            execution_skip_detail=skip_detail,
        ))

    pm_reasoning, risk_verdict, macro_context = _run_scoped_context(run_rows)

    if raw["hard_risk_block"]:
        decision_state = "hard_risk_block"
    elif executed_count > 0:
        decision_state = "executed"
    elif proposed_order_count > 0:
        decision_state = "proposed_not_executed"
    elif symbols:
        decision_state = "no_proposal"
    else:
        decision_state = "no_candidates"

    session_prefix = run_id.rsplit("-", 1)[0] if "-" in run_id else run_id

    return RunFunnelResponse(
        run_id=run_id,
        session_prefix=session_prefix,
        timestamp=raw["first_timestamp"],
        candidates=candidates,
        candidates_considered=len(symbols),
        reached_pm_count=reached_pm_count,
        proposed_order_count=proposed_order_count,
        executed_count=executed_count,
        bearish_hedge_considered=bearish_hedge_considered,
        hard_risk_block=raw["hard_risk_block"],
        pm_reasoning=pm_reasoning,
        risk_verdict=risk_verdict,
        macro_context=macro_context,
        decision_state=decision_state,
    )
