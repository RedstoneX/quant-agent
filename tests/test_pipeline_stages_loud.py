"""Converted stage catch-alls: swallowed fault, clean pass, and an unreached site stay distinct."""
import logging
from types import SimpleNamespace

from src.pipeline_stage_helpers import _live_stops_from_heat, record_stage
from src.sentinel.reconciliation import ReconciliationLog
from src.storage.db import Database


def _pipeline(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    db.initialize()
    return SimpleNamespace(db=db)


def test_swallowed_fault_logs_traceback_and_disagreed_row(caplog, tmp_path):
    pl = _pipeline(tmp_path)
    try:
        raise TypeError("boom")
    except TypeError as exc:
        with caplog.at_level(logging.ERROR):
            record_stage(pl, "heal_record", exc)
    assert any(r.exc_info for r in caplog.records)
    assert ReconciliationLog(conn=pl.db.conn).status(kind="guarded:stages.heal_record") == "disagreed"


def test_clean_pass_row_and_unreached_site_has_none(tmp_path):
    pl = _pipeline(tmp_path)
    record_stage(pl, "heal_record")
    log = ReconciliationLog(conn=pl.db.conn)
    assert log.status(kind="guarded:stages.heal_record") == "agreed"
    assert log.status(kind="guarded:stages.soft_exit_drain") == "not_run"


def test_live_stop_map_swallows_bad_heat_loudly(caplog):
    bad = SimpleNamespace(per_position=[SimpleNamespace()])
    ctx = SimpleNamespace(facts=SimpleNamespace(heat=bad))
    with caplog.at_level(logging.ERROR):
        assert _live_stops_from_heat(ctx) is None
    assert any(r.exc_info for r in caplog.records)
