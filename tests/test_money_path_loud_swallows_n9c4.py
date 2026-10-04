"""Swallows made loud (traceback at ERROR) with behaviour unchanged."""
from __future__ import annotations

import logging

from src.execution.stop_records import _holding_is_short
from src.sentinel.swallow_record import record_swallow


class _Boom:
    def get_positions(self):
        raise RuntimeError("positions down")


def test_swallow_logs_traceback_and_keeps_behaviour(caplog):
    with caplog.at_level(logging.ERROR):
        assert _holding_is_short(_Boom(), "AAA") is None
    assert any(r.exc_info for r in caplog.records)


def test_recorder_never_raises_and_clean_pass_ok():
    record_swallow("x.y", None, k=1)
    record_swallow("x.y", ValueError("v"))
