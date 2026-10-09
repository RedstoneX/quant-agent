"""The cash sweeper's swallowed failures leave a durable row (2026-10-01).

Split off `test_cash_sweep.py` (at its size baseline). `release_retired_vehicle`
used to log a broker position-read failure and return None — indistinguishable
from "nothing held". The silent-swallow ratchet flagged it; now it records a
`pipeline_event` row before returning.
"""

from tests.test_cash_sweep import _retired_pipeline


def test_retired_release_position_read_failure_is_recorded_durably(tmp_path):
    from src.storage.db import Database

    p = _retired_pipeline()
    p.db = Database(str(tmp_path / "t.db"))
    p.db.initialize()
    p.broker.get_positions.side_effect = ConnectionError("down")

    assert p.cash_sweeper.release_retired_vehicle(run_id="run-fail") is None

    rows = p.db.conn.execute(
        "SELECT run_id, symbol, evidence_json FROM specialist_evidence WHERE kind = 'pipeline_event'"
    ).fetchall()
    assert len(rows) == 1
    run_id, symbol, evidence = rows[0]
    assert run_id == "run-fail" and symbol == "SGOV"
    assert '"stage": "cash_sweep_release"' in evidence
    assert '"outcome": "skipped"' in evidence
    assert "down" in evidence
    p._submit_protected_sell.assert_not_called()
