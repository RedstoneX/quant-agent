"""Mechanical heal first, then at most one paid retry. Never invent text."""
import pytest
from src.seat_heal import (
    HEAL_CAP_BLOCKED,
    HEAL_FAILED,
    HEAL_MECHANICAL,
    HEAL_SKIPPED_GOOD,
    HealResult,
    can_paid_retry,
    coerce_macro_shape,
    coerce_sector_guidance,
    describe_macro_parse_failure,
    heal_failure_alert_text,
    mechanical_heal_macro,
    record_paid_retry,
    restore_stated_soft_exits,
)
from src.models import MacroAnalysis, MacroPositionGuidance, MacroReasoningChain


def _chain(**overrides):
    base = dict(
        volatility_analysis="vix ok",
        yield_curve_analysis="curve ok",
        monetary_policy_analysis="fed ok",
        inflation_labor_credit="cpi ok",
        cross_signal_synthesis="together ok",
        sector_implications="tech ow",
    )
    base.update(overrides)
    return MacroReasoningChain(**base)


def test_stated_soft_exit_is_restored_not_invented():
    values = {"thesis_invalid_if": "", "catalyst": None, "symbol": "AAPL"}
    raw = {"thesis_invalid_if": "daily close below 191.5", "catalyst": "8-K"}
    out, restored = restore_stated_soft_exits(values, raw)
    assert out["thesis_invalid_if"] == "daily close below 191.5"
    assert out["catalyst"] == "8-K"
    assert set(restored) == {"catalyst", "thesis_invalid_if"}


def test_omitted_soft_exit_is_not_filled_with_placeholder_text():
    values = {"thesis_invalid_if": "", "catalyst": ""}
    out, restored = restore_stated_soft_exits(values, {"thesis_invalid_if": None})
    assert out["thesis_invalid_if"] == ""
    assert restored == []
    out2, restored2 = restore_stated_soft_exits(values, {"thesis_invalid_if": "  "})
    assert restored2 == []


def test_restore_overwrites_unknown_with_the_stated_string_not_an_invention():
    values = {"thesis_invalid_if": "unknown", "catalyst": "unknown"}
    raw = {"thesis_invalid_if": "daily close below 191.5", "catalyst": "8-K"}
    out, restored = restore_stated_soft_exits(values, raw)
    assert out["thesis_invalid_if"] == "daily close below 191.5"
    assert out["catalyst"] == "8-K"
    assert set(restored) == {"catalyst", "thesis_invalid_if"}


def test_merge_retry_falsifiers_copies_stated_only_never_invents():
    from src.seat_heal import merge_retry_falsifiers

    original = [
        {"symbol": "AAPL", "risk_allocation_pct": 1.0, "thesis_invalid_if": ""},
        {"symbol": "MSFT", "risk_allocation_pct": 1.0,
         "thesis_invalid_if": "closes below 400"},
        {"symbol": "NVDA", "risk_allocation_pct": 0.0, "thesis_invalid_if": ""},
    ]
    retry = [
        {"symbol": "AAPL", "risk_allocation_pct": 9.0,
         "thesis_invalid_if": "daily close below 191.5",
         "catalyst": "invented 8-K"},
        {"symbol": "MSFT", "thesis_invalid_if": "retry must not overwrite"},
        {"symbol": "NVDA", "thesis_invalid_if": "close needs no fill"},
    ]
    merged, filled = merge_retry_falsifiers(original, retry)
    by_sym = {t["symbol"]: t for t in merged}
    assert by_sym["AAPL"]["thesis_invalid_if"] == "daily close below 191.5"
    assert by_sym["AAPL"]["risk_allocation_pct"] == 1.0
    assert "catalyst" not in by_sym["AAPL"] or by_sym["AAPL"].get("catalyst") != "invented 8-K"
    assert by_sym["MSFT"]["thesis_invalid_if"] == "closes below 400"
    assert by_sym["NVDA"]["thesis_invalid_if"] == ""
    assert filled == ["AAPL"]


def test_merge_retry_falsifiers_does_not_invent_when_retry_also_blank():
    from src.seat_heal import merge_retry_falsifiers

    original = [{"symbol": "MRVL", "risk_allocation_pct": 1.0, "thesis_invalid_if": "unknown"}]
    merged, filled = merge_retry_falsifiers(
        original,
        [{"symbol": "MRVL", "thesis_invalid_if": ""},
         {"symbol": "MRVL", "thesis_invalid_if": "unknown"}],
    )
    assert merged[0]["thesis_invalid_if"] == "unknown"
    assert filled == []


def test_macro_parse_failure_names_missing_chain_and_does_not_invent_one():
    payload = {
        "regime": "risk-on",
        "confidence": "medium",
        "equity_outlook": "bullish",
        "summary": "stay long",
        "position_guidance": {
            "target_invested_pct": 70, "cash_recommendation_pct": 30,
            "reasoning": "stay invested",
        },
        "sector_guidance": {"Technology": "bullish"},
    }
    coerced, _fixes = coerce_macro_shape(payload)
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        MacroAnalysis.model_validate(coerced)
    reason = describe_macro_parse_failure(coerced, exc.value)
    assert "reasoning_chain" in reason
    assert "macro_parse_failed" in reason
    assert "stay long" not in reason or "reasoning_chain" in reason


def test_sector_guidance_dict_coerces_to_list_without_invented_reasons():
    rows = coerce_sector_guidance({"Technology": "bullish", "Energy": "bearish"})
    by_sector = {r["sector"]: r for r in rows}
    assert by_sector["Technology"]["stance"] == "overweight"
    assert by_sector["Energy"]["stance"] == "underweight"
    assert by_sector["Technology"]["reason"] == ""


def test_macro_dict_shape_coerces_then_validates_when_chain_present():
    payload = {
        "reasoning_chain": _chain().model_dump(),
        "regime": "risk-on",
        "confidence": "medium",
        "equity_outlook": "bullish",
        "position_guidance": {
            "target_invested_pct": 70, "cash_recommendation_pct": 30,
            "reasoning": "stay invested",
        },
        "summary": "risk on",
        "sector_guidance": {"Technology": "bullish"},
    }
    coerced, fixes = coerce_macro_shape(payload)
    assert "sector_guidance_dict_to_list" in fixes
    analysis = MacroAnalysis.model_validate(coerced)
    assert analysis.regime == "risk-on"
    assert analysis.sector_guidance[0].sector == "Technology"


def test_macro_trim_without_reasoning_chain_fails_closed_not_invented_chain():
    """The on-disk MacroStore trim drops reasoning_chain. Coerce the shape;
    do not fill the chain from summary (that would invent macro text);
    do not treat the broken trim as usable macro for PM."""
    trim = {
        "date": "2026-09-16",
        "regime": "risk-on",
        "confidence": "medium",
        "equity_outlook": "bullish",
        "summary": "stay long",
        "position_guidance": {"target_invested_pct": 70},
        "sector_guidance": {"Technology": "bullish"},
    }
    heal = mechanical_heal_macro(trim)
    assert heal.usable is False
    assert heal.outcome == HEAL_FAILED
    assert "reasoning_chain" in heal.reason
    assert heal.payload["regime"] == "risk-on"
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        MacroAnalysis.model_validate(heal.payload)


def test_macro_without_regime_is_not_healed_into_ok():
    heal = mechanical_heal_macro({"summary": "garbled"})
    assert heal.usable is False
    assert heal.outcome == HEAL_FAILED


def test_one_paid_retry_max_per_seat():
    used = {}
    assert can_paid_retry(used, "macro") is True
    used = record_paid_retry(used, "macro")
    assert can_paid_retry(used, "macro") is False
    assert can_paid_retry(used, "news") is True
    used = record_paid_retry(used, "macro")
    assert used["macro"] == 2
    assert can_paid_retry(used, "macro") is False


def test_heal_failure_alert_is_own_message_and_names_the_seat():
    failed = HealResult(seat="macro", outcome=HEAL_FAILED, reason="still unreadable")
    text = heal_failure_alert_text(failed)
    assert text.startswith("OWNER ALERT")
    assert "macro" in text
    blocked = HealResult(seat="news", outcome=HEAL_CAP_BLOCKED, reason="day cap")
    cap = heal_failure_alert_text(blocked, cap_blocked=True)
    assert "spend cap" in cap
    assert "news" in cap


def test_unknown_sector_stance_is_dropped_not_invented_neutral():
    rows = coerce_sector_guidance({"Technology": "sideways"})
    assert rows == []
    heal = mechanical_heal_macro({
        "regime": "risk-on",
        "equity_outlook": "bullish",
        "sector_guidance": {"Technology": "bullish"},
    })
    assert heal.usable is False
    assert heal.outcome == HEAL_FAILED
    assert heal.paid_retry is False
    assert "reasoning_chain" in heal.reason


def test_pipeline_does_not_repay_remembered_good_or_empty_store():
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    # The paid retry now also consults a DURABLE per-ET-day count, because
    # RunContext.heal_paid_retries only spans one tick and intra_check runs
    # every 30 minutes (production shows 8 paid news heals on 2026-09-18).
    # A bare MagicMock int()s to 1, i.e. "already spent today", which is not
    # what this test is about.
    p.db.count_paid_seat_heals_today.return_value = 0
    p.macro_analyst = MagicMock()
    p._require_paid_analysis = MagicMock()
    p._record_heal = MagicMock()
    ctx = RunContext.start("intra_check")
    ctx.data_status = {
        "macro": "remembered",
        "news": "chose_not_to_refetch",
        "earnings": "carry_forward_empty",
    }
    ctx.macro_summary = {"vix": {"current": 18}}
    TradingPipeline._heal_lost_research_seats(p, ctx)
    p.macro_analyst.analyze.assert_not_called()
    p._require_paid_analysis.assert_not_called()


def test_pipeline_paid_retry_is_one_shot_and_requires_inputs():
    from unittest.mock import MagicMock
    from src.models import MacroAnalysis, MacroPositionGuidance, MacroReasoningChain
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext

    chain = MacroReasoningChain(
        volatility_analysis="vix ok", yield_curve_analysis="curve ok",
        monetary_policy_analysis="fed ok", inflation_labor_credit="cpi ok",
        cross_signal_synthesis="together ok", sector_implications="tech ow",
    )
    analysis = MacroAnalysis(
        reasoning_chain=chain, regime="risk-on", confidence="medium",
        equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=70, cash_recommendation_pct=30,
            reasoning="stay invested",
        ),
        summary="risk on",
    )
    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    # The paid retry now also consults a DURABLE per-ET-day count, because
    # RunContext.heal_paid_retries only spans one tick and intra_check runs
    # every 30 minutes (production shows 8 paid news heals on 2026-09-18).
    # A bare MagicMock int()s to 1, i.e. "already spent today", which is not
    # what this test is about.
    p.db.count_paid_seat_heals_today.return_value = 0
    p.macro_analyst = MagicMock()
    p.macro_analyst.analyze.return_value = (analysis, MagicMock())
    p.news_analyst = MagicMock()
    p._require_paid_analysis = MagicMock()
    p._record_heal = MagicMock()

    ctx = RunContext.start("morning")
    ctx.data_status = {"macro": "parse_error", "news": "carry_forward_failed"}
    ctx.macro_summary = {"vix": {"current": 18}}
    TradingPipeline._heal_lost_research_seats(p, ctx)
    assert p.macro_analyst.analyze.call_count == 1
    assert ctx.data_status["macro"] == "ok"
    assert ctx.heal_paid_retries["macro"] == 1
    # Second pass must not pay again.
    ctx.data_status["macro"] = "parse_error"
    TradingPipeline._heal_lost_research_seats(p, ctx)
    assert p.macro_analyst.analyze.call_count == 1
    # News without wire text must not invent a paid call.
    p.news_analyst.analyze.assert_not_called()


def test_remembered_good_is_not_a_heal_target():
    """A validating snapshot is usable without a paid call. A chain-less
    trim is not 'remembered good' — that is the fail-closed test above."""
    payload = {
        "reasoning_chain": _chain().model_dump(),
        "regime": "risk-on",
        "confidence": "medium",
        "equity_outlook": "bullish",
        "position_guidance": {
            "target_invested_pct": 70, "cash_recommendation_pct": 30,
            "reasoning": "stay invested",
        },
        "summary": "risk on",
        "sector_guidance": {"Technology": "bullish"},
    }
    heal = mechanical_heal_macro(payload)
    assert heal.usable
    assert heal.paid_retry is False
    assert heal.outcome in (HEAL_MECHANICAL, HEAL_SKIPPED_GOOD)


def test_broken_macro_dict_is_not_passed_to_pm():
    """Silent broken macro into PM is the Phase 13 afternoon failure.
    Coerce, then fail closed — do not return the garbage dict."""
    from src.pipeline_stages import _macro_analysis_as_dict
    from src.agents.portfolio_manager import PortfolioManagerAgent

    PortfolioManagerAgent._macro_parse_failures = []
    trim = {
        "regime": "risk-on",
        "confidence": "medium",
        "equity_outlook": "bullish",
        "summary": "stay long",
        "position_guidance": {
            "target_invested_pct": 70, "cash_recommendation_pct": 30,
            "reasoning": "stay invested",
        },
        "sector_guidance": {"Technology": "bullish"},
    }
    assert _macro_analysis_as_dict(trim) is None
    assert PortfolioManagerAgent._macro_parse_failures
    assert "reasoning_chain" in PortfolioManagerAgent._macro_parse_failures[0]
    PortfolioManagerAgent._macro_parse_failures = []
