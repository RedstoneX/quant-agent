"""Evidence-kind reuse: pay again only when that kind of fact expired or was never good.

Owner 2026-09-16. PR #430 same-session GOOD reuse must not regress.
No refresh timer. No item-20 seat-count. Blank/LOST is never research.
"""
    from src.evidence_kind import (
        DECISION_LOST,
        DECISION_REFETCH,
        DECISION_REREAD_LIVE,
        DECISION_REUSE,
        QUALITY_GOOD,
        QUALITY_LOST,
        STATUS_CARRIED_FROM_MORNING,
        STATUS_CHOSE_NOT_TO_REFETCH,
        STATUS_REMEMBERED,
        chart_reuse,
        covered_news_headlines,
        earnings_reuse,
        headline_mentions_symbols,
        insider_reuse,
        macro_reuse,
        newer_material_wire,
        news_reuse,
        payload_quality,
        same_session_from_date,
    )
from src import evidence_gate


def test_blank_and_none_are_lost_not_research():
    assert payload_quality(None) == QUALITY_LOST
    assert payload_quality({}) == QUALITY_LOST
    assert news_reuse(None, same_session=True, newer_material_wire=False).decision == DECISION_LOST
    assert macro_reuse(None, same_session=True, regime_or_print_changed=False).decision == DECISION_LOST
    assert macro_reuse({"summary": "no regime"}, same_session=True, regime_or_print_changed=False).decision == DECISION_LOST


def test_same_session_good_news_is_reused_without_a_clock():
    """PR #430: same-session GOOD news is usable. No second fitted timer."""
    payload = {"pm_briefing": "ok", "stock_news": {"AAPL": [{"headline": "beat"}]}}
    v = news_reuse(payload, same_session=True, newer_material_wire=False)
    assert v.decision == DECISION_REUSE
    assert v.status == STATUS_CARRIED_FROM_MORNING
    assert v.usable
    assert not v.pay_again
    assert not evidence_gate.counts_as_degraded(v.status)


def test_newer_material_wire_expires_news_without_inventing_minutes():
    payload = {"pm_briefing": "ok", "stock_news": {"AAPL": [{"headline": "beat"}]}}
    v = news_reuse(payload, same_session=True, newer_material_wire=True)
    assert v.decision == DECISION_REFETCH
    assert v.pay_again
    assert newer_material_wire(frozenset({"beat"}), ["beat"]) is False
    assert newer_material_wire(frozenset({"beat"}), ["breaking: guidance cut"]) is True
    assert newer_material_wire(frozenset({"beat"}), []) is False


def test_cross_session_news_is_expired():
    payload = {"pm_briefing": "ok"}
    v = news_reuse(payload, same_session=False, newer_material_wire=False)
    assert v.decision == DECISION_REFETCH


def test_macro_same_session_good_reuse_is_integrity_clean():
    v = macro_reuse(
        {"regime": "risk-on", "equity_outlook": "bullish"},
        same_session=True, regime_or_print_changed=False,
    )
    assert v.status == STATUS_CARRIED_FROM_MORNING
    assert not evidence_gate.counts_as_degraded(v.status)
    assert evidence_gate.evaluate({"macro": v.status, "news": "carried_from_morning"}).skip is False


def test_macro_is_remembered_across_days_until_regime_change():
    payload = {"regime": "risk-on", "equity_outlook": "bullish"}
    kept = macro_reuse(payload, same_session=False, regime_or_print_changed=False)
    assert kept.decision == DECISION_REUSE
    assert kept.status == STATUS_REMEMBERED
    assert kept.same_session is False
    assert not evidence_gate.counts_as_degraded(kept.status)
    expired = macro_reuse(payload, same_session=False, regime_or_print_changed=True)
    assert expired.decision == DECISION_REFETCH


def test_failed_macro_parse_is_not_regime_ok():
    v = macro_reuse({"equity_outlook": "bullish"}, same_session=True, regime_or_print_changed=False)
    assert v.decision == DECISION_LOST
    assert v.quality == QUALITY_LOST


def test_earnings_remembered_until_next_report():
    writeup = [{"symbol": "AAPL", "analysis": {"summary": "beat"}}]
    kept = earnings_reuse(writeup, same_session=True, new_report_or_8k=False)
    assert kept.decision == DECISION_REUSE
    expired = earnings_reuse(writeup, same_session=True, new_report_or_8k=True)
    assert expired.decision == DECISION_REFETCH
    quiet = earnings_reuse([], same_session=True, new_report_or_8k=False)
    assert quiet.status == STATUS_CHOSE_NOT_TO_REFETCH
    assert not evidence_gate.counts_as_degraded(quiet.status)


def test_insider_remembered_until_new_form4():
    findings = [{"symbol": "FTK", "accession": "0001"}]
    kept = insider_reuse(findings, same_session=True, new_form4=False)
    assert kept.decision == DECISION_REUSE
    expired = insider_reuse(findings, same_session=True, new_form4=True)
    assert expired.decision == DECISION_REFETCH


def test_chart_always_rereads_live_price():
    v = chart_reuse({"symbol": "AAPL", "rating": "buy"}, same_session=True)
    assert v.decision == DECISION_REREAD_LIVE
    assert v.usable
    assert not v.pay_again


def test_no_seat_count_lives_in_the_kind_module():
    import inspect
    import src.evidence_kind as mod
    src = inspect.getsource(mod)
    assert "min_seats" not in src
    assert "seat_count" not in src


def test_covered_headlines_read_model_or_dict():
    class _Item:
        headline = "Apple beats"

    class _Report:
        stock_news = {"AAPL": [_Item()]}

    assert "Apple beats" in covered_news_headlines(_Report())
    assert "Apple beats" in covered_news_headlines(
        {"stock_news": {"AAPL": [{"headline": "Apple beats"}]}}
    )
    assert headline_mentions_symbols("AAPL guidance cut after close", ["AAPL"]) is True
    assert headline_mentions_symbols("Fed holds rates after the close", ["AAPL"]) is False
    assert headline_mentions_symbols("Apple beats", []) is False


def test_undated_is_not_same_session():
    from src.trading_calendar import et_today
    assert same_session_from_date(str(et_today())) is True
    assert same_session_from_date(None) is False
    assert same_session_from_date("") is False
    assert same_session_from_date("yesterday") is False
