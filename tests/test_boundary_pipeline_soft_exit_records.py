"""Clause-5 witness for src.pipeline_soft_exit_records: exercised with stand-ins, no trading pipeline built."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import src.pipeline_soft_exit_records as soft_exit_records
from src.pipeline_soft_exit_records import (
    _isolate_empty_soft_exit_entries,
    _record_mechanical_soft_exit_restores,
    _record_soft_exit_heals,
    _targets_admitted_to_book,
)

MOVED = (
    "_target_increase_missing_falsifier",
    "_targets_admitted_to_book",
    "_record_mechanical_soft_exit_restores",
    "_record_soft_exit_heals",
    "_record_soft_exit_missing_after_retry",
    "_record_soft_exit_refusal_count",
    "_isolate_empty_soft_exit_entries",
)


def _ctx(**kw):
    base = dict(run_id="run-1", decision_id="dec-1", total_value=0.0, positions=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_no_targets_admits_nothing_and_refuses_nothing():
    assert _targets_admitted_to_book([]) == ([], [])
    assert _targets_admitted_to_book(None, positions=None) == ([], [])


def test_soft_exit_heals_drain_off_the_host_land_on_ctx_and_in_the_stand_in_db():
    pm = SimpleNamespace(drain_soft_exit_heals=lambda: {"AAPL": {"outcome": "paid_retry", "detail": "re-asked"}})
    host = SimpleNamespace(db=MagicMock(name="db"), portfolio_manager=pm)
    ctx = _ctx()
    _record_soft_exit_heals(host, ctx)
    assert ctx.soft_exit_heals == {"AAPL": {"outcome": "paid_retry", "detail": "re-asked"}}
    assert host.db.method_calls, "one pipeline_event row per healed name must reach the handed-in db"


def test_soft_exit_heals_with_no_drain_on_the_host_writes_nothing():
    host = SimpleNamespace(db=MagicMock(name="db"), portfolio_manager=None)
    ctx = _ctx()
    _record_soft_exit_heals(host, ctx)
    assert not hasattr(ctx, "soft_exit_heals")


def test_mechanical_restore_record_never_raises_without_a_db():
    _record_mechanical_soft_exit_restores(SimpleNamespace(db=None), _ctx())


def test_isolate_with_no_decision_returns_an_empty_list_and_touches_nothing():
    host = SimpleNamespace(db=MagicMock(name="db"))
    assert _isolate_empty_soft_exit_entries(host, _ctx(), None) == []
    assert not host.db.method_calls


def test_every_moved_name_is_the_same_object_through_the_old_import_path():
    import src.pipeline_stages as stages

    for name in MOVED:
        assert getattr(stages, name) is getattr(soft_exit_records, name), name
