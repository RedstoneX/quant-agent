"""A replay cannot judge code from provider attempts it cannot reconstruct."""

import sqlite3

from ops.rehearsal.replay import RecordedCall, ResponseLibrary
from ops.rehearsal.report import RehearsalReport, _verdict


def _call(*, attempts=1, chunks=None):
    chunks = chunks or []
    message = "\n\n".join(f"--- {label} ---\n{prompt}" for label, prompt, _ in chunks)
    response = "\n\n".join(f"--- {label} ---\n{answer}" for label, _, answer in chunks)
    return RecordedCall(
        row_id=718, agent_name="portfolio_manager", run_id="run-14170a8e",
        timestamp="2026-10-01 14:00:00", model="m",
        input_message=message or "one retained prompt",
        full_response=response or '{"targets": []}',
        input_tokens=100, output_tokens=20, cost_usd=0.001,
        finish_reason="stop", actual_provider="openrouter",
        provider_requests=attempts,
    )


def test_collapsed_provider_attempts_are_named_as_a_fidelity_gap():
    library = ResponseLibrary([_call(attempts=3)], source_run_id="run-14170a8e")
    gaps = [f for f in library.findings if f["kind"] == "incomplete_provider_attempt_recording"]
    assert len(gaps) == 1
    assert (gaps[0]["attempted"], gaps[0]["represented"]) == (3, 1)
    assert "routing, cost and circuit state" in gaps[0]["detail"]


def test_chunk_markers_account_for_each_provider_attempt_without_a_gap():
    call = _call(attempts=2, chunks=[
        ("chunk 1/2", "first prompt", '{"symbol": "AAPL"}'),
        ("chunk 2/2", "second prompt", '{"symbol": "MSFT"}'),
    ])
    library = ResponseLibrary([call], source_run_id=call.run_id)
    assert not [f for f in library.findings if f["kind"] == "incomplete_provider_attempt_recording"]


def test_database_loader_carries_attempt_count_into_fidelity_check(tmp_path):
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE agent_logs (id INTEGER PRIMARY KEY, agent_name TEXT, run_id TEXT, "
        "timestamp TEXT, model TEXT, input_message TEXT, full_response TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, finish_reason TEXT, "
        "actual_provider TEXT, provider_requests INTEGER)"
    )
    connection.execute(
        "INSERT INTO agent_logs VALUES (1, 'risk_manager', 'run-x', '2026-10-01', "
        "'m', 'prompt', '{}', 10, 2, 0.001, 'stop', 'openrouter', 3)"
    )
    connection.commit()
    connection.close()
    library = ResponseLibrary.from_database(str(path), run_id="run-x")
    assert library.available() == {"risk_manager": 1}
    assert [(f["attempted"], f["represented"]) for f in library.findings] == [(3, 1)]


def test_collapsed_attempt_history_makes_verdict_inconclusive():
    report = RehearsalReport(
        session="morning", rehearsed_date="2026-10-01", run_id="r",
        source_run_id="run-x", status="executed", completed=True,
        findings=[{
            "kind": "incomplete_provider_attempt_recording",
            "agent": "portfolio_manager",
            "detail": "three provider attempts occurred but only one was retained",
        }],
    )
    assert _verdict(report) == "INCONCLUSIVE"
    report.verdict = _verdict(report)
    rendered = " ".join(report.render().split())
    assert "VERDICT: INCONCLUSIVE" in rendered
    assert "three provider attempts occurred" in rendered
