"""Clause-5 witness for src.pipeline_candidate_records: exercised with stand-ins, no trading pipeline built."""
from types import SimpleNamespace
from unittest.mock import patch

import src.pipeline_candidate_records as records
from src.pipeline_candidate_records import (
    _record_heal_safely,
    _record_pipeline_event,
    _soft_exit_heal_detail,
)


def test_soft_exit_heal_detail_reads_the_recorded_outcome_off_a_bare_context():
    ctx = SimpleNamespace(soft_exit_heals={"ABC": {"outcome": "retried", "detail": "paid retry filed"}})
    assert _soft_exit_heal_detail(ctx, " abc ") == "heal outcome 'retried': paid retry filed"


def test_soft_exit_heal_detail_says_plainly_when_nothing_was_recorded():
    assert _soft_exit_heal_detail(SimpleNamespace(), "XYZ").startswith("no soft-exit heal was recorded")


def test_record_heal_safely_swallows_a_failing_recorder_and_forwards_alert():
    calls = []

    def boom(ctx, result, *, alert):
        calls.append(alert)
        raise RuntimeError("bookkeeping down")

    _record_heal_safely(SimpleNamespace(_record_heal=boom), ctx=object(), result=SimpleNamespace(outcome="x"), alert=False)
    assert calls == [False]


def test_record_pipeline_event_routes_through_persist_evidence_with_the_stand_in_db():
    db = object()
    pipeline = SimpleNamespace(db=db)
    ctx = SimpleNamespace(run_id="run-1", decision_id="dec-1")
    with patch.object(records, "_persist_evidence") as persist:
        _record_pipeline_event(pipeline, ctx, "ABC", "decision", "kept", "why", extra=1)
    assert persist.call_count == 1
    args, kwargs = persist.call_args
    assert args == (db,)
    assert kwargs["run_id"] == "run-1" and kwargs["symbol"] == "ABC"


def test_every_moved_name_is_the_same_object_through_the_old_import_path():
    import src.pipeline_stages as stages

    for name in ("_account_for_pm_candidates", "_isolate_empty_soft_exit_entries",
                 "_record_pipeline_event", "_PM_ACCOUNTING_SEAT"):
        assert getattr(stages, name) is getattr(records, name)
