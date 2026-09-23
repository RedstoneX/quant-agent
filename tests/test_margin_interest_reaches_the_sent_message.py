"""Margin interest must reach the morning message trader_feed ACTUALLY
sends, not just the base notifier.py formatter it falls back to.

Orchestrator finding, verified against `notifier_sends`, 2026-09-23: 0 of 78
sent messages contained "margin interest". Root cause — `_format_decision_session`
(the "morning"/"once" renderer `format_session_result` dispatches to; see
`src.trader_feed.format_session_result`) never called
`src.notifier._margin_interest_lines`, even though that function already
existed, was fully built, and IS called from `src.notifier.format_session_result`
itself — a path `_format_decision_session` never falls through to for status
"ok".

These tests pin: the line is present in the actual sent-path renderer, it
survives the message-length budget (it must sit before any section subject
to `_budgeted_sections`, same guarantee `test_pnl_leads_every_message.py`
already pins for the P&L block), and a broker-read failure degrades to a
line rather than being silently dropped.
"""
from __future__ import annotations

import pytest

from src import trader_feed


@pytest.fixture(autouse=True)
def _no_db_or_network(monkeypatch):
    monkeypatch.setattr(trader_feed, "_read_run", lambda *a, **kw: trader_feed._empty_snapshot())
    monkeypatch.setattr(trader_feed, "_read_hour_trades", lambda *a, **kw: [])
    monkeypatch.setattr(trader_feed, "_read_hour_evidence", lambda *a, **kw: [])
    monkeypatch.setattr(trader_feed, "_lookup_company_profiles", lambda *a, **kw: {})


RESULT = {
    "status": "ok",
    "run_id": "r",
    "daily_pnl": 123.45,
    "daily_return_pct": 1.23,
    "total_pnl": 678.90,
    "total_return_pct": 6.78,
    "total_pnl_since": "2026-09-02",
}


def test_morning_message_carries_the_margin_interest_line(monkeypatch):
    monkeypatch.setattr(
        trader_feed, "_margin_interest_lines",
        lambda: ["\U0001f4b3 margin interest: $0.99/day (ESTIMATE) on $5.7k borrowed"],
    )
    msg = trader_feed._format_decision_session("morning", dict(RESULT), 10.0)
    assert "margin interest" in msg


def test_margin_interest_line_precedes_the_budgeted_candidate_list(monkeypatch):
    # Same guarantee `test_pnl_leads_every_message.py` pins for the P&L
    # block: a section placed before `_budgeted_sections` runs is never the
    # one a length clip eats. `_append_rotation` renders the candidate list
    # that block is built to protect.
    monkeypatch.setattr(
        trader_feed, "_margin_interest_lines",
        lambda: ["\U0001f4b3 margin interest: $0.99/day (ESTIMATE) on $5.7k borrowed"],
    )
    msg = trader_feed._format_decision_session("morning", dict(RESULT), 10.0)
    interest_pos = msg.index("margin interest")
    pnl_pos = msg.index("Today's P&L")
    assert pnl_pos < interest_pos, "margin interest must come after the P&L block, not before it"
    # It must also land well before the end of a long message — i.e. inside
    # the guaranteed-to-survive header region, not appended at the tail.
    assert interest_pos < len(msg) // 2


def test_zero_debit_balance_still_shows_an_explicit_line_never_silence(monkeypatch):
    # Owner policy, 2026-09-18: "every day, even if it's zero, that way I
    # know it's still working." `_margin_interest_lines` already enforces
    # this; this test only pins that trader_feed does not swallow it.
    monkeypatch.setattr(
        trader_feed, "_margin_interest_lines",
        lambda: ["\U0001f4b3 margin interest: $0.00/day — nothing borrowed overnight"],
    )
    msg = trader_feed._format_decision_session("morning", dict(RESULT), 10.0)
    assert "$0.00/day" in msg


def test_a_broker_read_failure_degrades_to_a_line_not_to_silence(monkeypatch):
    # `_margin_interest_lines` itself never raises (src/notifier.py
    # docstring); this pins that trader_feed does not add a second failure
    # mode by, say, wrapping the call in something that swallows a
    # non-empty-but-degraded return.
    monkeypatch.setattr(
        trader_feed, "_margin_interest_lines",
        lambda: ["\U0001f4b3 margin interest: not available — no borrowing rate is configured"],
    )
    msg = trader_feed._format_decision_session("morning", dict(RESULT), 10.0)
    assert "margin interest: not available" in msg
