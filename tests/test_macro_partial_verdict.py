"""Board item 119, second criterion — a macro verdict formed on an
incomplete FRED set must stay VISIBLY incomplete everywhere it travels.

Why this file exists (measured, not assumed)
--------------------------------------------
Production's `agent_logs` table holds 19 paid `macro_analyst` calls whose
prompt carried a coverage section. Two of them were formed on a partial set:
2026-09-17 at 13:33:56 UTC (8/15) and 2026-09-22 at 13:34:56 UTC (7/15), both
within five minutes of the 09:30 ET open, and both missing the same tail of
`CONFIGURED_SERIES`. On the first of the two the regime came back
`transitional`/`low` where the mornings either side of it said `risk-on`, so a
partial set is not a cosmetic difference — it moved the answer.

What the desk already had, and what it did not
----------------------------------------------
It already had the INPUT side: `MacroCoverage.describe()` names the holes in
the economist's own prompt. It already had a run-scoped operator signal:
`data_status["macro"] = "partial"`, which raises the degraded banner. And it
already refused to pay twice — "partial" maps to `CATEGORY_REPORTED` in
`src.evidence_gate`, never to a healable category, so the seat heal cannot buy
a second opinion on the same holes.

What it did NOT have is the verdict itself carrying the fact. The verdict
outlives the run: `MacroStore.save_last_state` persists it, midday/close/intra
read it back as `carried_from_morning`, later days read it back as
`remembered`, the portfolio manager renders it, and the owner gets it as the
"📊 Market:" line. Every one of those surfaces showed a 7/15 read exactly as it
showed a 15/15 one. These tests pin the stamp at each of them.

No coverage threshold appears anywhere below, deliberately. Nothing here
compares a count against a cutoff; the stamp is a restatement of the
deterministic fetch record.
"""

from __future__ import annotations

import json

import pytest

from src.data.macro import MacroCoverage, SeriesFailure
from src.data.macro_store import MacroStore
from src.models import (
    MacroAnalysis, MacroPositionGuidance, MacroReasoningChain,
)


# --------------------------------------------------------------------------
# Fixtures


def _analysis(**overrides) -> MacroAnalysis:
    kwargs = dict(
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="VIX compressing.",
            yield_curve_analysis="Curve steepening.",
            monetary_policy_analysis="Fed on hold.",
            inflation_labor_credit="Core CPI sticky.",
            cross_signal_synthesis="Risk-on lean.",
            sector_implications="Tech overweight.",
        ),
        regime="risk-on",
        confidence="medium",
        equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=75.0, cash_recommendation_pct=25.0,
            reasoning="Hold buffer.",
        ),
        summary="Constructive.",
    )
    kwargs.update(overrides)
    return MacroAnalysis(**kwargs)


def _partial_coverage() -> MacroCoverage:
    """The 2026-09-22 production shape: 7 of 15, the same eight series that
    sit at the tail of `CONFIGURED_SERIES` never attempted."""
    missing = [
        "PCEPI", "UNRATE", "BAMLH0A0HYM2", "DFII10",
        "T10YIE", "DTWEXBGS", "BAMLC0A0CM", "ICSA",
    ]
    return MacroCoverage(
        configured=15, succeeded=7,
        failed=[SeriesFailure(series_id=s, reason="fetch_deadline_exceeded")
                for s in missing],
    )


# --------------------------------------------------------------------------
# The stamp itself


def test_complete_coverage_stamps_complete_with_no_note():
    cov = MacroCoverage(configured=15, succeeded=15, failed=[])
    assert cov.verdict_stamp() == ("complete", "")


def test_partial_coverage_stamps_partial_and_names_every_missing_series():
    state, note = _partial_coverage().verdict_stamp()
    assert state == "partial"
    assert "7/15" in note
    for series in ("PCEPI", "UNRATE", "ICSA", "BAMLC0A0CM"):
        assert series in note


def test_total_failure_stamps_failed_not_partial():
    cov = MacroCoverage(
        configured=15, succeeded=0,
        failed=[SeriesFailure(series_id="VIXCLS", reason="timeout")],
    )
    assert cov.verdict_stamp()[0] == "failed"


def test_misconfigured_empty_set_is_failed_not_complete():
    cov = MacroCoverage(configured=0, succeeded=0, failed=[])
    state, note = cov.verdict_stamp()
    assert state == "failed"
    assert "misconfiguration" in note


def test_stamp_never_compares_coverage_against_a_threshold():
    """14/15 and 1/15 both stamp `partial`. There is no cutoff between them,
    because no sourceable coverage threshold exists and picking one is
    barred. This pins the absence of a number."""
    def cov(succeeded: int) -> MacroCoverage:
        return MacroCoverage(
            configured=15, succeeded=succeeded,
            failed=[SeriesFailure(series_id=f"S{i}", reason="fetch_deadline_exceeded")
                    for i in range(15 - succeeded)],
        )
    assert cov(14).verdict_stamp()[0] == "partial"
    assert cov(1).verdict_stamp()[0] == "partial"


# --------------------------------------------------------------------------
# The model


def test_unstamped_verdict_is_unknown_and_is_not_a_partial_claim():
    """An old snapshot, a test double or any caller that never stamped makes
    no claim in either direction — it must not read as complete, and it must
    not manufacture a caveat the fetch record does not support."""
    a = _analysis()
    assert a.coverage_state == "unknown"
    assert a.partial_read is False


def test_partial_and_failed_states_both_read_as_a_partial_read():
    assert _analysis(coverage_state="partial").partial_read is True
    assert _analysis(coverage_state="failed").partial_read is True
    assert _analysis(coverage_state="complete").partial_read is False


def test_a_verdict_persisted_before_this_field_existed_still_parses():
    """Every macro_store snapshot and replayed decision on disk predates
    these two fields; they must not become a validation error."""
    legacy = _analysis().model_dump()
    legacy.pop("coverage_state")
    legacy.pop("coverage_note")
    assert MacroAnalysis(**legacy).coverage_state == "unknown"


def test_coverage_state_rejects_an_invented_word():
    with pytest.raises(Exception):
        _analysis(coverage_state="mostly_fine")


# --------------------------------------------------------------------------
# The surfaces the verdict travels to


def test_macro_store_round_trip_keeps_the_stamp(tmp_path):
    """`save_last_state` writes an explicit whitelist of keys, so a field the
    model carries is dropped unless it is listed. This is what midday/close
    and later DAYS read back, so losing the stamp here would launder a
    partial read into a complete-looking one a tick later."""
    store = MacroStore(data_dir=str(tmp_path))
    analysis = _analysis(coverage_state="partial", coverage_note="7/15 FRED series; missing: ICSA")
    store.save_last_state(analysis.model_dump())
    loaded = store.load_last_state()
    assert loaded["coverage_state"] == "partial"
    assert "ICSA" in loaded["coverage_note"]


def test_macro_store_round_trip_defaults_an_unstamped_caller_to_unknown(tmp_path):
    store = MacroStore(data_dir=str(tmp_path))
    payload = _analysis().model_dump()
    payload.pop("coverage_state")
    store.save_last_state(payload)
    assert store.load_last_state()["coverage_state"] == "unknown"


def test_pm_sheet_marks_a_partial_read_and_names_the_gap():
    from src.agents.portfolio_manager import PortfolioManagerAgent
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    macro = _analysis(
        coverage_state="partial",
        coverage_note="7/15 FRED series; missing: PCEPI, UNRATE, ICSA",
    ).model_dump()
    msg = _pm_macro_section(agent, macro)
    assert "PARTIAL READ" in msg
    assert "ICSA" in msg
    assert "not calm readings" in msg or "gaps, not calm" in msg


def test_pm_sheet_says_nothing_when_coverage_is_complete():
    from src.agents.portfolio_manager import PortfolioManagerAgent
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    msg = _pm_macro_section(agent, _analysis(coverage_state="complete").model_dump())
    assert "PARTIAL READ" not in msg


def test_pm_sheet_says_nothing_when_nobody_stamped():
    from src.agents.portfolio_manager import PortfolioManagerAgent
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    msg = _pm_macro_section(agent, _analysis().model_dump())
    assert "PARTIAL READ" not in msg


def _pm_macro_section(agent, macro_analysis: dict) -> str:
    """Build the PM user message far enough to read its Macro Analysis
    section. Uses the real builder rather than re-implementing the string."""
    msg = agent.build_user_message(
        analyses=[], positions=[], cash_balance=10_000.0, total_value=10_000.0,
        macro_analysis=macro_analysis,
    )
    start = msg.find("## Macro Analysis")
    assert start != -1
    end = msg.find("\n## ", start + 3)
    return msg[start:end if end != -1 else len(msg)]


def test_owner_market_line_flags_a_partial_read():
    from src.trader_feed import _append_market
    lines: list[str] = []
    _append_market(lines, {"macro": {
        "regime": "risk-on", "equity_outlook": "bullish", "confidence": "low",
        "coverage_state": "partial",
        "coverage_note": "7/15 FRED series; missing: ICSA",
    }})
    assert len(lines) == 1
    assert "PARTIAL READ" in lines[0]
    assert "ICSA" in lines[0]


def test_owner_market_line_is_unchanged_on_a_complete_read():
    from src.trader_feed import _append_market
    lines: list[str] = []
    _append_market(lines, {"macro": {
        "regime": "risk-on", "equity_outlook": "bullish", "confidence": "medium",
        "coverage_state": "complete",
    }})
    assert lines == ["📊 Market: risk-on / bullish / medium"]


def test_owner_market_line_says_nothing_on_an_unstamped_verdict():
    from src.trader_feed import _append_market
    lines: list[str] = []
    _append_market(lines, {"macro": {
        "regime": "risk-on", "equity_outlook": "bullish", "confidence": "medium",
    }})
    assert "PARTIAL" not in lines[0]


# --------------------------------------------------------------------------
# The other half of the criterion: never pay a second time on the same holes


def test_a_partial_macro_seat_is_never_eligible_for_a_second_paid_call():
    """`_try_one_paid_research_retry` only runs for a seat whose data_status
    maps into `HEALABLE_CATEGORIES`. "partial" must stay outside that set:
    re-asking the economist over the SAME eight holes buys a second opinion
    on identical inputs and spends real money for it. This pins the existing
    behaviour mechanically so a future edit to STATUS_CATEGORY cannot switch
    the pay-twice path on by accident."""
    from src import evidence_gate as gate
    assert gate.STATUS_CATEGORY["partial"] == gate.CATEGORY_REPORTED
    assert gate.CATEGORY_REPORTED not in gate.HEALABLE_CATEGORIES


def test_a_lost_macro_seat_is_still_eligible_for_its_one_paid_retry():
    """The counterpart: a seat with NO answer is a different case from a seat
    with a partial one, and this fix must not have narrowed it."""
    from src import evidence_gate as gate
    assert gate.STATUS_CATEGORY["failed"] in (gate.CATEGORY_LOST,)
    assert gate.CATEGORY_LOST in gate.HEALABLE_CATEGORIES


# --------------------------------------------------------------------------
# End-to-end shape: the stamp reaches the evidence row the operator reads


def test_evidence_json_of_a_stamped_verdict_carries_the_stamp():
    """`trader_feed` builds the owner snapshot from the
    `specialist_evidence` row, which is `MacroAnalysis.model_dump_json()`."""
    payload = json.loads(_analysis(
        coverage_state="partial", coverage_note="7/15 FRED series; missing: ICSA",
    ).model_dump_json())
    assert payload["coverage_state"] == "partial"
    assert "ICSA" in payload["coverage_note"]
