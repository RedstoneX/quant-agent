"""Board item 219 - the pruning pass's outcome, read off the rendered text.

Drives a stored run through the real Telegram session formatter and through
the dashboard's run-detail reader, and asserts the owner-facing WORDS carry
what was reviewed, what was kept and what was culled with its reason. A name
below the entry bar that was NOT cut must never be told to the owner as one
that "still clears the bar".
"""

from src import trader_feed
from src.api.routes_history import _rotation_lines
from tests.test_trader_feed import (
    _QUIET_TICK_TIME,
    _evidence,
    _make_db,
    _pin_clock,
)

_ROW = dict(
    tier="ineligible_hold",
    held_symbol="BBB",
    held_reasons="technical rule failed",
    held_examined="AAA,BBB,CCC",
    held_examined_count=3,
    held_below_entry_bar="BBB,CCC",
    held_below_entry_bar_count=2,
)


def _seed(db, run):
    _evidence(
        db,
        run,
        "pipeline",
        "pipeline_event",
        {
            "stage": "rotation",
            "outcome": "precheck",
            "reason": "full_nothing_outranked_a_holding",
            "headroom_pct": 0.09,
            "ceiling_pct": 25.0,
            "floor_pct": 0.5,
            "execute_enabled": True,
            "ranked_margin_enabled": False,
            **_ROW,
        },
    )


def _kept_line(lines):
    return [ln for ln in lines if "Considered and kept" in ln][0]


def test_telegram_message_states_reviewed_kept_and_culled(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    _seed(db, "run-219")
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    msg = trader_feed.format_session_result(
        "morning",
        {"status": "no_trades", "run_id": "run-219", "orders": []},
        1.0,
    )
    assert "examined all 3 holdings" in msg
    assert "Put up to be cut: BBB" in msg and "technical rule failed" in msg
    kept = _kept_line(msg.split("\n"))
    assert "AAA" in kept
    # CCC is below the bar and was not cut: it is not "kept because it clears".
    assert "CCC" not in kept and "BBB" not in kept
    assert "CCC" in msg.split("Below the desk's own entry bar today")[1]


def test_dashboard_run_detail_carries_the_same_sentences(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    _seed(db, "run-219")
    lines = _rotation_lines("run-219")
    text = "\n".join(lines)
    assert "examined all 3 holdings" in text
    assert "technical rule failed" in text
    assert "CCC" not in _kept_line(lines)
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    msg = trader_feed.format_session_result(
        "morning",
        {"status": "no_trades", "run_id": "run-219", "orders": []},
        1.0,
    )
    for line in lines:
        assert line.strip() in msg
