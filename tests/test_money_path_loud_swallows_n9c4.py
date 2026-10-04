"""Swallows made loud (traceback at ERROR) and counted, behaviour unchanged."""
from __future__ import annotations

import logging
import sqlite3

from src.execution.stop_records import _holding_is_short
from src.sentinel.guarded import NO_LEDGER, record_guarded_pass


class _Boom:
    def get_positions(self):
        raise RuntimeError("positions down")


class _Ok:
    def get_positions(self):
        return []


def test_swallow_logs_traceback_and_keeps_behaviour(caplog):
    with caplog.at_level(logging.ERROR):
        assert _holding_is_short(_Boom(), "AAA") is None
    assert any(r.exc_info for r in caplog.records)


def test_clean_pass_and_exempt_site_never_raise():
    assert _holding_is_short(_Ok(), "AAA") is not True
    record_guarded_pass(NO_LEDGER, "x.y", ValueError("v"))
    record_guarded_pass(NO_LEDGER, "x.y")
