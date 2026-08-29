"""Regression: the rehearsal verdict must not call a healthy session a FAIL.

Found running the rig against real production history (2026-08-29): plain,
uneventful `midday`, `close` and `intra_check` rehearsals — no crash, no
missing recording, no blocked agent, nothing stopped — all came back
`VERDICT: FAIL` because `report.py`'s status vocabulary had not kept up with
`src/pipeline.py`'s. `run_position_review` (shared by midday/close) returns
"reviewed" for both "no positions to review" and "reviewed them successfully"
— only "position_review_parse_error" is the real failure. `run_intra_check`
returns "ok" when there is no loss violation or no positions — a normal,
majority-of-the-time outcome for a session that runs every 30 minutes.
`run_evening` returns "analyzed" on success. Production's own
`src/trader_feed.py` (groups "reviewed", "intraday_no_trades", "no_trades",
"ok") and `src/notifier.py` (groups "executed", "analyzed", "reviewed",
"preprocessed", "reflected") already treat these as healthy completions, not
failures — the rig disagreeing with production about what counts as "the
session worked" is exactly the kind of dishonest output this harness exists
to avoid producing itself.
"""

from __future__ import annotations

from ops.rehearsal.report import RehearsalReport, STATUS_PLAIN, _verdict


def _report(status: str) -> RehearsalReport:
    return RehearsalReport(
        session="midday", rehearsed_date="2026-08-31", run_id="r",
        source_run_id=None, status=status, completed=True,
    )


def test_reviewed_with_no_positions_is_not_a_failure():
    """run_midday / run_close's normal outcome when there is nothing to
    review — not a crash, not a degraded result."""
    assert _verdict(_report("reviewed")) == "PASS"


def test_intra_check_ok_is_not_a_failure():
    """run_intra_check's normal outcome absent a loss-limit breach — the
    common case on a 30-minute cadence, not an error."""
    assert _verdict(_report("ok")) == "PASS"


def test_evening_analyzed_is_not_a_failure():
    assert _verdict(_report("analyzed")) == "PASS"


def test_intraday_no_trades_and_intraday_executed_are_not_failures():
    assert _verdict(_report("intraday_no_trades")) == "PASS"
    assert _verdict(_report("intraday_executed")) == "PASS"


def test_genuine_failure_statuses_are_still_fail():
    """The fix must not swallow real failures alongside the healthy ones."""
    for status in (
        "position_review_parse_error", "evening_analysis_error",
        "evening_parse_error", "broker_error", "pm_agent_failure",
    ):
        assert _verdict(_report(status)) == "FAIL", status


def test_every_newly_recognized_status_has_a_plain_english_entry():
    for status in ("reviewed", "ok", "analyzed", "intraday_no_trades", "intraday_executed"):
        assert status in STATUS_PLAIN
        assert STATUS_PLAIN[status]  # non-empty
