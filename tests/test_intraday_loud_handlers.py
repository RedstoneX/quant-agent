"""Intraday catch-alls: swallowed fault, clean pass, unreached site, missing ledger handle."""
import logging
from types import SimpleNamespace

from src.sentinel.guarded_site import record_site
from src.sentinel.reconciliation import ReconciliationLog
from src.storage.db import Database

_SITES = ("report_persist", "trigger_atr_context", "macro_state_load", "tech_store_load",
          "cooldown_legacy_trades",
          "scan_lock_release", "skip_reason_lock_contended", "skip_reason_open_overlap")


def _owner(tmp_path):
    db = Database(str(tmp_path / "i.db"))
    db.initialize()
    return SimpleNamespace(db=db)


def _status(owner, site):
    return ReconciliationLog(conn=owner.db.conn).status(kind=f"guarded:intraday.{site}")


def test_swallowed_fault_logs_traceback_and_counts_row(tmp_path, caplog):
    owner = _owner(tmp_path)
    for site in _SITES:
        try:
            raise RuntimeError(site)
        except RuntimeError as exc:
            with caplog.at_level(logging.ERROR):
                record_site(owner, site, exc)
        assert _status(owner, site) == "disagreed"
    assert sum(1 for r in caplog.records if r.exc_info) >= len(_SITES)


def test_clean_pass_writes_own_row(tmp_path):
    owner = _owner(tmp_path)
    record_site(owner, "tech_store_load")
    assert _status(owner, "tech_store_load") == "agreed"


def test_unreached_site_has_no_row(tmp_path):
    owner = _owner(tmp_path)
    record_site(owner, "tech_store_load")
    assert _status(owner, "macro_state_load") == "not_run"


def test_missing_ledger_handle_logs_traceback_and_does_not_raise(caplog):
    try:
        raise ValueError("x")
    except ValueError as exc:
        with caplog.at_level(logging.ERROR):
            record_site(SimpleNamespace(), "report_persist", exc)
    assert any(r.exc_info for r in caplog.records)
