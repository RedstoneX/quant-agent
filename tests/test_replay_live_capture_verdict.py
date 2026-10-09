"""A private replay succeeds only when the whole session result agrees."""

from types import SimpleNamespace

import pytest

from ops.rehearsal.replay_live_capture import (
    CapturedReplayError,
    _assert_capture_reproduced,
)


def test_same_status_does_not_hide_failed_verdict_or_different_order():
    original = {"status": "executed", "run_id": "run-captured", "orders": [{"symbol": "AAA", "qty": 2}]}
    same = {"status": "executed", "run_id": "rehearsal-morning-20261007", "orders": [{"symbol": "AAA", "qty": 2}]}
    report = SimpleNamespace(status="executed", verdict="FAIL", run_id="rehearsal-morning-20261007")
    with pytest.raises(CapturedReplayError, match="verdict"):
        _assert_capture_reproduced(report, original, same, "run-captured")

    report.verdict = "PASS"
    changed = {**same, "orders": [{"symbol": "AAA", "qty": 3}]}
    with pytest.raises(CapturedReplayError, match="result differs"):
        _assert_capture_reproduced(report, original, changed, "run-captured")

    _assert_capture_reproduced(report, original, same, "run-captured")
