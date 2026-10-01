"""A response with no usage information must say so, not look like a free success."""
from types import SimpleNamespace

import pytest

from src.agents.base import agent_log_kwargs, usage_telemetry_word


def _res(i, o, cost, reqs=1):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cost_usd=cost,
                           provider_requests=reqs)


def test_words():
    assert usage_telemetry_word(10, 5, 0.01, 1) == "complete"
    assert usage_telemetry_word(10, 5, None, 1) == "no_cost"
    assert usage_telemetry_word(0, 0, None, 1) == "no_usage"
    assert usage_telemetry_word(0, 0, None, 0) is None
    assert usage_telemetry_word(object(), 1, None, 1) is None


def test_kwargs_carry_word():
    assert agent_log_kwargs(_res(0, 0, None))["telemetry"] == "no_usage"


@pytest.fixture
def db(tmp_path):
    from src.storage.db import Database
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


def test_record_distinguishes_missing_from_measured_zero(db):
    base = dict(input_summary="i", output_summary="o", full_response="{}",
                model="free-x", tokens_used=0)
    db.insert_agent_log(agent_name="a", run_id="r", input_tokens=0, output_tokens=0,
                        cost_usd=None, **agent_log_kwargs(_res(0, 0, None)), **base)
    db.insert_agent_log(agent_name="a", run_id="r", input_tokens=3, output_tokens=2,
                        cost_usd=0.0, **agent_log_kwargs(_res(3, 2, 0.0)), **base)
    db.insert_agent_log(agent_name="a", run_id="r", **base)
    rows = db.execute("SELECT telemetry FROM agent_logs ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["no_usage", "complete", None]
