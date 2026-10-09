"""Direct tests: each lifted pipeline part is imported on its own, with no pipeline object behind it."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from src.pipeline_parts import evening, kill_repair, morning_helpers, pnl_gaps, review


def test_forced_close_side_and_qty_is_direction_aware():
    assert pnl_gaps._forced_close_side_and_qty(5) == ("sell", 5.0)
    assert pnl_gaps._forced_close_side_and_qty(-3) == ("buy", 3.0)
    assert pnl_gaps._forced_close_side_and_qty(0) is None
    assert pnl_gaps._forced_close_side_and_qty(float("nan")) is None


def test_trade_executed_or_pending_ignores_zero_fill_cancels():
    assert pnl_gaps._trade_executed_or_pending({"fill_status": "submitted"})
    assert not pnl_gaps._trade_executed_or_pending({"fill_status": "canceled", "fill_qty": 0})
    assert pnl_gaps._trade_executed_or_pending({"fill_status": "canceled", "fill_qty": 2})


def test_report_kill_repair_names_each_stop_it_could_not_add(caplog):
    outcomes = [
        SimpleNamespace(placed=False, symbol="ZZZ", qty=2.0, detail="rejected"),
        SimpleNamespace(placed=True, symbol="YYY", qty=1.0, detail=""),
    ]
    with caplog.at_level(logging.WARNING):
        kill_repair._report_kill_repair("ctx", outcomes)
    text = caplog.text
    assert "could NOT add the stop owed on ZZZ" in text
    assert "placed 1 of 2 owed stop(s)" in text


def test_evening_stop_proximity_builds_the_session_from_the_pipeline(monkeypatch):
    seen = {}

    class FakeSession:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run(self, positions):
            return [{"n": len(positions)}]

    monkeypatch.setattr(evening, "EveningStopProximitySession", FakeSession)
    pipeline = SimpleNamespace(db="DB", _collab=lambda name: f"collab:{name}")
    assert evening._evening_stop_proximity(pipeline, [1, 2]) == [{"n": 2}]
    assert seen["db"] == "DB" and seen["broker"] == "collab:broker"


def test_record_name_coverage_hands_config_to_the_session(monkeypatch):
    class FakeSession:
        def __init__(self, config):
            self.config = config

        def run(self, ctx, record):
            record(self.config, ctx)

    monkeypatch.setattr(morning_helpers, "NameCoverageRecordSession", FakeSession)
    got = []
    pipeline = SimpleNamespace(_collab=lambda name: f"collab:{name}")
    morning_helpers._record_name_coverage(pipeline, "ctx", lambda a, b: got.append((a, b)))
    assert got == [("collab:config", "ctx")]


def test_review_module_exposes_the_lifted_entry_points():
    for name in (
        "run_position_review",
        "run_earnings_preprocess",
        "_persist_review_metrics",
        "_run_position_review_body",
    ):
        assert callable(getattr(review, name))
