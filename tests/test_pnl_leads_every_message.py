"""P&L leads EVERY Telegram message, directly under the heading.

Owner, 2026-09-18, verbatim: "all the P&L information has to go at the very
top of every telegram alert, right after the first line, which is really the
heading."

A REPEAT correction — it had been asked for before and drifted, because each
new banner, gate notice or cost line was added "at the top" of a message that
already had a P&L block somewhere below. So these tests assert the POSITION,
not the presence. Presence never regressed; position did.

The rule, mechanically: after the heading line(s), the first thing with any
content in it is the P&L block. Blank separator lines (`_seal_section`) do not
count as content.
"""

from __future__ import annotations

import pytest

from src import trader_feed
from src.notifier import format_session_result as base_format_session_result


PNL_MARKERS = ("Today's P&L", "Daily P&L", "P&L:")


def first_content_after(message: str, heading_lines: int) -> str:
    """The first line carrying content after `heading_lines` heading lines."""
    for line in message.split("\n")[heading_lines:]:
        if line.strip():
            return line
    return ""


def assert_pnl_leads(message: str, heading_lines: int = 1) -> None:
    lead = first_content_after(message, heading_lines)
    assert any(marker in lead for marker in PNL_MARKERS), (
        "the first thing after the heading was not the P&L block:\n"
        f"{message[:600]}"
    )


@pytest.fixture(autouse=True)
def _no_db_or_network(monkeypatch):
    """Every renderer under test reads the runs DB and the company-profile
    cache. Stub both to empty so these tests measure ORDER and nothing else
    — a missing figure is rendered honestly as "not available" and still has
    to appear in the leading position."""
    monkeypatch.setattr(trader_feed, "_read_run", lambda *a, **kw: trader_feed._empty_snapshot())
    monkeypatch.setattr(trader_feed, "_read_hour_trades", lambda *a, **kw: [])
    monkeypatch.setattr(trader_feed, "_read_hour_evidence", lambda *a, **kw: [])
    monkeypatch.setattr(trader_feed, "_lookup_company_profiles", lambda *a, **kw: {})


PNL_RESULT = {
    "status": "ok",
    "run_id": "r",
    "daily_pnl": 123.45,
    "daily_return_pct": 1.23,
    "total_pnl": 678.90,
    "total_return_pct": 6.78,
    "total_pnl_since": "2026-09-02",
}


def test_morning_decision_session_leads_with_pnl():
    msg = trader_feed._format_decision_session("morning", dict(PNL_RESULT), 10.0)
    assert_pnl_leads(msg)


def test_midday_position_review_leads_with_pnl():
    # A stop-coverage gap renders a banner, so the P&L assertion has
    # something to be ahead OF. (This used to use the daily-loss halt
    # status; that mechanism was removed 2026-09-20, retired item 32.)
    msg = trader_feed._format_position_review(
        "midday",
        {**PNL_RESULT, "stop_coverage_gaps": [
            {"symbol": "NVDA", "state": "uncovered", "coverage": "none"},
        ]},
        10.0,
    )
    assert_pnl_leads(msg)


def test_intraday_tick_leads_with_pnl_not_the_status_banner():
    """This is the one the owner was actually looking at: the P&L used to
    sit BELOW `_render_status_banner`."""
    # A nested status that actually RENDERS a banner — with a quiet status
    # the banner block is empty and the assertion passes either way, which
    # would make this test a no-op against the very regression it exists for
    # (checked by swapping the two lines back and watching it fail).
    outer = dict(PNL_RESULT)
    nested = {"status": "intraday_scan_crashed", "run_id": "r", "error": "boom"}
    msg = trader_feed._format_intraday(outer, nested, 10.0)
    assert "CRASHED" in msg
    assert_pnl_leads(msg)


def test_hourly_desk_check_leads_with_pnl():
    """Heading here is two lines — the title and the period line."""
    msg = trader_feed._format_hourly_desk_check(dict(PNL_RESULT), None, 10.0)
    # The hour's activity line always renders, so there is always a block
    # for the P&L to be ahead of here.
    assert "this hour" in msg
    assert_pnl_leads(msg, heading_lines=2)


def test_evening_leads_with_pnl_not_the_banners():
    # `missing_sessions` makes the escalation banner render real content —
    # without it the banner block is empty and this test would pass whatever
    # the order is (verified by swapping the two calls back).
    result = {
        "status": "analyzed", "run_id": "r",
        "pnl_4pm": -500.0, "equity_close": 100_500.0,
        "total_pnl": 678.90, "total_return_pct": 6.78,
        "missing_sessions": ["morning"],
        "analysis": {"risk_rating": "low"},
    }
    msg = trader_feed._format_evening(result, 10.0)
    assert "MORNING SESSION DID NOT RUN" in msg
    assert_pnl_leads(msg)


def test_premarket_earnings_leads_with_an_honest_not_available():
    """The pre-open filing reader does no account read, so there is no
    figure. The block still leads — and says why it is empty rather than
    being dropped (an absent block reads as a broken one) or filled with a
    fabricated zero."""
    result = {"status": "preprocessed", "run_id": "r", "filings": []}
    msg = trader_feed._format_earnings(result, 10.0)
    assert_pnl_leads(msg)
    assert "not available" in msg
    assert "without an account read" in msg
    assert "$0.00" not in msg.split("\n")[1]


def test_base_notifier_formatter_leads_with_pnl():
    """The fallback formatter every non-trader-feed mode falls back to."""
    msg = base_format_session_result("meta", dict(PNL_RESULT), 10.0)
    assert_pnl_leads(msg)


def test_base_notifier_formatter_puts_pnl_above_the_cost_lines():
    """Costs are what it takes to RUN the desk; P&L is his money. Costs used
    to be the first block under the heading."""
    msg = base_format_session_result("meta", dict(PNL_RESULT), 10.0)
    lines = msg.split("\n")
    pnl_at = next(i for i, ln in enumerate(lines) if "Today's P&L" in ln)
    cost_at = [i for i, ln in enumerate(lines) if "AI cost" in ln or "OpenRouter" in ln]
    assert all(pnl_at < i for i in cost_at)


def test_evening_base_formatter_still_uses_the_4pm_figure_when_it_leads():
    """Moving the evening block to the top must NOT have swapped it for the
    shared real-time renderer — that would leak the after-hours number the
    4pm path exists to keep out."""
    result = {
        "status": "analyzed", "run_id": "r",
        "daily_pnl": 1200.0, "total_value": 101_200.0,
        "pnl_4pm": -500.0, "equity_close": 100_500.0,
        "analysis": {"risk_rating": "low"},
    }
    msg = base_format_session_result("evening", result, 10.0)
    assert_pnl_leads(msg)
    assert "4pm close" in first_content_after(msg, 1)
    assert "+$1,200" not in msg


def test_standalone_owner_alerts_carry_the_pnl_line_under_the_heading(monkeypatch):
    """A standalone alert fires the instant a problem is found, on paths
    that have done no account read — so it says that in one sentence rather
    than dropping the block or inventing a figure. Enforced in
    `send_owner_alert` itself, the one funnel all eighteen callers share,
    because the rule drifts whenever it depends on the next author."""
    import src.notifier as n

    sent: list[str] = []
    monkeypatch.setattr(
        n, "TelegramNotifier",
        lambda: type("_T", (), {"send": lambda self, text, symbols=None, **kwargs: sent.append(text) or True})(),
    )
    n.send_owner_alert("NAKED POSITION — AAPL has no protective stop\nDetail line.")
    assert len(sent) == 1
    lines = sent[0].split("\n")
    assert lines[0].startswith("NAKED POSITION")
    assert "P&L: not available in this alert" in lines[1]
    assert lines[2] == "Detail line."


def test_owner_alert_pnl_line_is_not_doubled_up(monkeypatch):
    """Guard against a caller that already carries the line (or a retry)
    growing a second copy."""
    import src.notifier as n

    sent: list[str] = []
    monkeypatch.setattr(
        n, "TelegramNotifier",
        lambda: type("_T", (), {"send": lambda self, text, symbols=None, **kwargs: sent.append(text) or True})(),
    )
    once = n._with_pnl_header("HEADING\nbody")
    twice = n._with_pnl_header(once)
    assert once == twice
    assert once.count(n._ALERT_NO_PNL_LINE) == 1
