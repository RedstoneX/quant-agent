"""Owner pause door: a desk session with no intent database fails closed."""

import pytest
from pydantic import ValidationError

from src.config.operations import StorageConfig
from src.execution import owner_flags_gate as gate


@pytest.fixture(autouse=True)
def _reset():
    saved = gate.unknown_state_recorder
    yield
    gate.unknown_state_recorder = saved
    gate.release()


@pytest.mark.parametrize("path", [None, ""])
def test_no_database_halts_orders_with_one_alert(path):
    alerts = []
    gate.unknown_state_recorder = alerts.append
    gate.configure(path)
    first = gate._verdict("submit_order")
    second = gate._verdict("submit_order")
    assert first and second
    assert len(alerts) == 1
    assert gate._verdict("replace_stop_loss") is None  # stops stay allowed


@pytest.mark.parametrize("path", ["", "   "])
def test_settings_refuse_empty_db_path(path):
    with pytest.raises(ValidationError):
        StorageConfig(db_path=path)
