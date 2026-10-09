"""A response with no usage information must say so, not look like a free success."""

from types import SimpleNamespace

import pytest

from src.agents.base import agent_log_kwargs, usage_telemetry_word


def _res(i, o, cost, reqs=1):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cost_usd=cost, provider_requests=reqs)


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
    base = dict(input_summary="i", output_summary="o", full_response="{}", model="free-x", tokens_used=0)
    db.insert_agent_log(
        agent_name="a",
        run_id="r",
        input_tokens=0,
        output_tokens=0,
        cost_usd=None,
        **agent_log_kwargs(_res(0, 0, None)),
        **base,
    )
    db.insert_agent_log(
        agent_name="a",
        run_id="r",
        input_tokens=3,
        output_tokens=2,
        cost_usd=0.0,
        **agent_log_kwargs(_res(3, 2, 0.0)),
        **base,
    )
    db.insert_agent_log(agent_name="a", run_id="r", **base)
    rows = db.execute("SELECT telemetry FROM agent_logs ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["no_usage", "complete", None]


def _score_rows(rows, with_telemetry_column=True):
    """Score agent_logs rows with the cost circuit's own unknown-cost SQL."""
    import sqlite3

    from src.cost_circuit import _unknown_cost_row_expr

    conn = sqlite3.connect(":memory:")
    cols = "cost_usd REAL, provider_requests INTEGER, status TEXT" + (
        ", telemetry TEXT" if with_telemetry_column else ""
    )
    conn.execute(f"CREATE TABLE agent_logs ({cols})")
    names = ["cost_usd", "provider_requests", "status"]
    if with_telemetry_column:
        names.append("telemetry")
    conn.executemany(
        f"INSERT INTO agent_logs ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
        [tuple(row[n] for n in names) for row in rows],
    )
    expr = _unknown_cost_row_expr(conn)
    return [r[0] for r in conn.execute(f"SELECT {expr} FROM agent_logs")]


def test_a_success_with_no_usage_telemetry_is_unknown_not_a_measured_zero():
    """Item 203: a provider request that happened and reported no tokens is
    counted as unknown cost even though its `cost_usd` is a literal 0.0 --
    the zero was never measured, and no list rate is substituted for it."""
    scores = _score_rows(
        [
            # The real hole: zero cost written because nothing came back.
            {"cost_usd": 0.0, "provider_requests": 1, "status": "success", "telemetry": "no_usage"},
            # Free route reporting a real zero against real token counts: known.
            {"cost_usd": 0.0, "provider_requests": 1, "status": "success", "telemetry": "complete"},
            # Tokens known, price absent: already unknown via the NULL test.
            {"cost_usd": None, "provider_requests": 1, "status": "success", "telemetry": "no_cost"},
            # No provider request at all (cache hit): proven zero, item 147.
            {"cost_usd": None, "provider_requests": 0, "status": "success", "telemetry": None},
        ]
    )
    assert scores == [1, 0, 1, 0]


def test_no_usage_telemetry_counts_as_unknown_on_a_failed_row_too():
    """A non-success row that still reached the provider and reported no
    usage is unknown; `_PROVEN_ZERO_ROW_SQL` must not forgive it."""
    assert _score_rows(
        [
            {"cost_usd": 0.0, "provider_requests": 1, "status": "fallback", "telemetry": "no_usage"},
        ]
    ) == [1]


def test_older_agent_logs_without_a_telemetry_column_still_scores():
    """The clause is dropped, not fabricated, when the proof column is absent."""
    assert _score_rows(
        [
            {"cost_usd": 0.0, "provider_requests": 1, "status": "success"},
            {"cost_usd": None, "provider_requests": 1, "status": "success"},
        ],
        with_telemetry_column=False,
    ) == [0, 1]
