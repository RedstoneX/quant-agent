"""update_open_take_profit must refuse cleanly, never raise NameError."""

import logging

import pytest

from src.storage.db import Database


@pytest.mark.parametrize(
    "target, action, message",
    [
        (0, None, "refused a non-positive target"),
        (-5.0, None, "refused a non-positive target"),
        (10.0, "HOLD", "refused unknown action"),
    ],
)
def test_refusal_returns_false_and_logs_intended_message(caplog, target, action, message):
    db = Database(":memory:")
    with caplog.at_level(logging.ERROR):
        assert db.update_open_take_profit("AAPL", target, action=action) is False
    if message:
        assert any(message in r.getMessage() for r in caplog.records)
