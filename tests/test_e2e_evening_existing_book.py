"""The hermetic EVENING session on an existing book.

Evening is the end-of-day AUDIT, not a trading session
(`src/sessions/evening_session.py`): "last check before carrying positions
overnight" — it reconciles fills, audits broker-truth stop coverage, records
the day's P&L, asks the evening analyst for the report, and writes the whole
result to the durable `evening_reports` table. It must place no order. This
file asserts that, on a book that already holds one long:

  covered       one resting stop behind the position: no coverage gap is
                reported, the day's P&L row and the report are persisted,
                the analyst was asked once, and nothing was submitted;
  naked         NO resting stop: the audit names the position as a gap
                instead of reporting a clean book;
  market shut   on a non-trading day the session says so, asks no model and
                places nothing.

Every seat is scripted; every expectation derives from the inputs.
"""
from __future__ import annotations

import json

from src.models.evening import EveningReasoningChain, EveningReport
from tests.e2e_held_book_support import NO_STOP, run_held_book
from tests.test_e2e_close_existing_book import (
    INITIAL_STOP, ONE_R_PRICE, QTY, SYMBOL, _bars, _news_says_nothing,
    _resting_sell_stops,
)

HOUR = 17
BARS = _bars(end=ONE_R_PRICE - 1.0)


def _analyst_says() -> dict:
    step = "x"
    report = EveningReport(
        reasoning_chain=EveningReasoningChain(
            performance_attribution=step, outlook_retrospection=step,
            thesis_health_review=step, decision_quality_review=step,
            calibration_meta=step, market_regime_read=step,
            tomorrow_preparation=step,
        ),
        daily_summary="synthetic day", lessons="none",
        tomorrow_outlook="synthetic", risk_rating="low",
    )
    return json.loads(report.model_dump_json())


def _evening(tmp_path, monkeypatch, **kw):
    answers = {"evening": _analyst_says(), "news": _news_says_nothing()}
    return run_held_book(tmp_path, monkeypatch, session="evening", hour=HOUR,
                         bars=BARS, answers=answers, **kw)


def _llm(trace, key):
    return [n for k, n in trace if k == "llm" and n == key]


def test_evening_on_a_covered_book_audits_records_and_places_nothing(
    tmp_path, monkeypatch,
):
    result, trace, trading, attempts, pipeline = _evening(tmp_path, monkeypatch)
    assert attempts == [], attempts
    assert result["status"] == "analyzed", {
        k: result.get(k) for k in ("status", "error")}
    assert result["stop_coverage_gaps"] == [], result["stop_coverage_gaps"]
    assert len(_llm(trace, "evening")) == 1, trace
    assert trading.submitted == [] and trading.cancelled == [], (
        [o.as_plain() for o in trading.submitted], trading.cancelled)
    assert [(s.stop_price, s.qty) for s in _resting_sell_stops(trading)] == [
        (INITIAL_STOP, QTY)]
    stored = pipeline.db.get_evening_report()
    assert stored is not None, "the evening result was not persisted"
    assert stored["run_id"] == result["run_id"], stored
    assert pipeline.db.get_daily_pnl(limit=5), "no daily P&L row was written"


def test_evening_names_a_position_that_has_no_resting_stop(
    tmp_path, monkeypatch,
):
    result, trace, trading, attempts, pipeline = _evening(
        tmp_path, monkeypatch, standing_stop=NO_STOP)
    assert attempts == [], attempts
    gaps = result["stop_coverage_gaps"]
    assert gaps and SYMBOL in json.dumps(gaps), (
        f"an unprotected overnight position was not reported: {gaps}")
    assert trading.submitted == [] or all(
        "stop" in o.order_type for o in trading.submitted), (
        [o.as_plain() for o in trading.submitted])


def test_evening_on_a_non_trading_day_reports_it_and_does_nothing(
    tmp_path, monkeypatch,
):
    result, trace, trading, attempts, pipeline = _evening(
        tmp_path, monkeypatch, trading_day=False)
    assert attempts == [], attempts
    assert result["status"] == "market_holiday", result
    assert result["analysis"] is None
    assert [k for k, _ in trace if k == "llm"] == [], (
        f"a model seat was asked on a shut market: {trace}")
    assert trading.submitted == [] and trading.cancelled == []
    assert [(s.stop_price, s.qty) for s in _resting_sell_stops(trading)] == [
        (INITIAL_STOP, QTY)]
