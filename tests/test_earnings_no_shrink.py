"""Owner ruling 2026-10-09: earnings never resize or block a trade."""

from pathlib import Path

from src.data.event_calendar.earnings import EarningsProximity

ROOT = Path(__file__).resolve().parent.parent
RM = (ROOT / "config/prompts/risk_manager.md").read_text()
PM = (ROOT / "config/prompts/portfolio_manager.md").read_text()


def test_risk_manager_has_no_earnings_cut_instruction():
    for bad in (
        "Reduce size due to upcoming earnings",
        "cut NVDA from 15 to 10",
        "Minor NVDA size cut pre-earnings",
        "(upcoming earnings, stretched stop)",
        "earnings/FOMC ≤ 3 days",
        "earnings ≤ 3 sessions",
        "fetched earnings date inside the window",
    ):
        assert bad not in RM, bad
    assert "earnings never resize or block a trade" in RM


def test_pm_prompt_has_no_earnings_window_sizing():
    assert "Check earnings / FOMC windows before sizing up" not in PM


def test_event_banner_is_gone():
    out = EarningsProximity("NVDA", 2, "measured").describe()
    assert "EVENT WINDOW" not in out
    assert out == "NVDA: next earnings ~2 sessions away (fetched)"
