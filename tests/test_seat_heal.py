"""Mechanical heal first, then at most one paid retry. Never invent text."""
from src.seat_heal import (
    HEAL_CAP_BLOCKED,
    HEAL_FAILED,
    HEAL_MECHANICAL,
    HEAL_SKIPPED_GOOD,
    HealResult,
    can_paid_retry,
    coerce_macro_shape,
    coerce_sector_guidance,
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


def test_macro_trim_without_reasoning_chain_is_usable_regime_not_invented_chain():
    """The on-disk MacroStore trim drops reasoning_chain. Coerce the shape;
    do not fill the chain from summary (that would invent macro text)."""
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
    assert heal.usable
    assert heal.outcome in (HEAL_MECHANICAL, HEAL_SKIPPED_GOOD)
    assert heal.payload["regime"] == "risk-on"
    assert "reasoning_chain" not in (heal.payload or {}) or not isinstance(
        (heal.payload or {}).get("reasoning_chain"), str
    )
    # Still must not parse as a full MacroAnalysis — missing chain is a
    # ValidationError, not a loosened schema.
    import pytest
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
    assert heal.usable
    assert heal.paid_retry is False
    assert heal.mechanical is True


def test_pipeline_does_not_repay_remembered_good_or_empty_store():
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
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
    heal = mechanical_heal_macro({
        "regime": "risk-on", "equity_outlook": "bullish",
        "sector_guidance": [],
    })
    assert heal.usable
    assert heal.paid_retry is False
