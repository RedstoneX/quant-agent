"""Every name the intraday scan drops leaves a recorded reason (pipeline event)."""

import json
from unittest.mock import patch

from src.pipeline_context import RunContext
from tests.test_intraday_scan import _intraday_pipeline, _snapshot


def test_dropped_names_each_record_their_reason():
    p = _intraday_pipeline(universe=["QUIET", "HOT", "A", "B"], max_candidates=1, cooldown_hours=3.0)
    p.broker.get_intraday_snapshots.return_value = {
        "QUIET": _snapshot(last=101.0, prev=100.0),
        "HOT": _snapshot(last=110.0, prev=100.0),
        "A": _snapshot(last=108.0, prev=100.0),
        "B": _snapshot(last=105.0, prev=100.0),
    }
    p.db.get_recent_intraday_evaluations.side_effect = lambda sym, cooldown_hours: (
        [{"timestamp": "2000-01-01 00:00:00"}] if sym == "HOT" else []
    )
    ctx = RunContext.start("intra_check")
    events = []
    with patch("src.pipeline_candidate_records._persist_evidence", side_effect=lambda db, **kw: events.append(kw)):
        with (
            patch.object(type(p), "_await_paid_scan_slot", return_value=True),
            patch.object(type(p), "_intraday_paid_scan_skip", return_value={}),
        ):
            p._run_intraday_opportunity_scan(ctx)
    got = {e["symbol"]: json.loads(e["evidence_json"]) for e in events if e["kind"] == "pipeline_event"}
    assert got["QUIET"]["reason"] == "below_move_threshold" and got["QUIET"]["move_pct"] == 1.0
    assert got["HOT"]["reason"] == "cooling_down" and got["HOT"]["hours_since_last"] > 1
    assert got["B"]["reason"] == "over_name_cap" and got["B"]["rank"] == 2
    assert "A" not in got  # the picked name is not a drop
