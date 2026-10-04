"""Clause-5 witness for src.pipeline_candidate_records: exercised with stand-ins, no trading pipeline built."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import src.pipeline_candidate_records as candidate_records
from src.pipeline_candidate_records import (
    _account_for_pm_candidates,
    _record_execution_skip,
    _record_heal_safely,
    _record_pipeline_event,
)

MOVED = ("_record_execution_skip", "_record_pipeline_event", "_PM_ACCOUNTING_SEAT",
         "_record_accounted_candidate", "_account_for_pm_candidates", "_record_heal_safely")


def _ctx(**kw):
    base = dict(run_id="run-1", decision_id="dec-1", execution_skips=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_execution_skip_appends_to_ctx_and_writes_through_the_stand_in_db():
    db = MagicMock(name="db")
    ctx = _ctx()
    _record_execution_skip(SimpleNamespace(db=db), ctx, "AAPL", "unfunded", "no cash")
    assert ctx.execution_skips == [{"symbol": "AAPL", "reason": "unfunded", "detail": "no cash"}]
    assert db.method_calls, "the evidence row must reach the handed-in db"


def test_pipeline_event_reaches_the_stand_in_db_read_off_the_host_per_call():
    host = SimpleNamespace(db=MagicMock(name="db1"))
    _record_pipeline_event(host, _ctx(), "AAPL", "risk", "ok", "fine")
    first = len(host.db.method_calls)
    assert first
    host.db = MagicMock(name="db2")  # the collaborator is re-read from the host, never snapshotted
    _record_pipeline_event(host, _ctx(), "AAPL", "risk", "ok", "fine")
    assert len(host.db.method_calls) == first


def test_heal_record_calls_the_host_recorder_and_never_raises_when_it_fails():
    seen = []
    host = SimpleNamespace(_record_heal=lambda ctx, result, alert: seen.append((result, alert)))
    _record_heal_safely(host, _ctx(), "RESULT", alert=False)
    assert seen == [("RESULT", False)]

    def boom(ctx, result, alert):
        raise RuntimeError("db down")

    _record_heal_safely(SimpleNamespace(_record_heal=boom), _ctx(), SimpleNamespace(outcome="x"))


def test_candidate_accounting_with_nothing_to_account_asks_the_seat_nothing():
    host = SimpleNamespace(db=MagicMock(name="db"), portfolio_manager=MagicMock(name="pm"),
                           _require_paid_analysis=MagicMock(name="paid"), _record_heal=MagicMock(name="heal"))
    _account_for_pm_candidates(host, _ctx(), run_id="run-1", analyses=[], positions=[],
                               decision=SimpleNamespace(targets=[], rejections=[]), pm_decide_kwargs={})
    host.portfolio_manager.decide.assert_not_called()
    host._require_paid_analysis.assert_not_called()


def test_every_moved_name_is_the_same_object_through_the_old_import_path():
    import src.pipeline_stages as stages

    for name in MOVED:
        assert getattr(stages, name) is getattr(candidate_records, name), name
    # stays behind: its body reaches the broker seam
    assert "_record_scale_in_window_closed" not in vars(candidate_records)
