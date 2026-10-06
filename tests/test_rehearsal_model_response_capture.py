"""The private model bundle contains only observed rows from one morning."""

import json
import sqlite3

import pytest

from ops.rehearsal.model_response_capture import (
    ModelResponseCaptureError,
    export_model_responses,
    import_model_responses,
)
from src.storage.db import Database


def _private_db(path):
    db = Database(str(path))
    db.initialize()
    db.close()
    path.chmod(0o600)


def _insert(path, run_id, response, *, agent="portfolio_manager"):
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO agent_logs (run_id, agent_name, input_message, "
            "full_response, model, status, decision_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, agent, 'real prompt', response, 'served-model',
             'success', 'decision-1'),
        )


def _paths(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    before = private / "before.db"
    after = private / "after.db"
    replay = private / "replay.db"
    bundle = private / "models.json"
    _private_db(before)
    _insert(before, "run-older", '{"old":true}')
    # Database uses WAL; a plain copy can omit committed rows still in WAL.
    with sqlite3.connect(before) as source, sqlite3.connect(after) as target:
        source.backup(target)
    with sqlite3.connect(before) as source, sqlite3.connect(replay) as target:
        source.backup(target)
    after.chmod(0o600)
    replay.chmod(0o600)
    return before, after, replay, bundle


def test_real_rows_round_trip_into_pre_session_copy(tmp_path):
    before, after, replay, bundle = _paths(tmp_path)
    _insert(after, "run-captured", '{"proposal":"BUY"}')
    _insert(after, "run-captured", '{"approved":false}', agent="risk_manager")
    assert export_model_responses(before, after, "run-captured", bundle) == 2
    assert bundle.stat().st_mode & 0o777 == 0o600
    assert import_model_responses(bundle, replay) == 2
    with sqlite3.connect(after) as captured, sqlite3.connect(replay) as restored:
        assert restored.execute("SELECT * FROM agent_logs ORDER BY id").fetchall() == \
            captured.execute("SELECT * FROM agent_logs ORDER BY id").fetchall()
    with pytest.raises(ModelResponseCaptureError, match="differs"):
        import_model_responses(bundle, replay)


@pytest.mark.parametrize("fault", ["missing", "other_run", "empty_response"])
def test_export_refuses_missing_or_mixed_evidence(tmp_path, fault):
    before, after, _replay, bundle = _paths(tmp_path)
    if fault == "other_run":
        _insert(after, "run-other", '{"x":1}')
        _insert(after, "run-captured", '{"x":2}')
    elif fault == "empty_response":
        _insert(after, "run-captured", None)
    with pytest.raises(ModelResponseCaptureError):
        export_model_responses(before, after, "run-captured", bundle)
    assert not bundle.exists()


def test_import_refuses_modified_pre_session_copy_without_partial_insert(tmp_path):
    before, after, replay, bundle = _paths(tmp_path)
    _insert(after, "run-captured", '{"x":1}')
    export_model_responses(before, after, "run-captured", bundle)
    _insert(replay, "run-unrelated", '{"x":2}')
    with pytest.raises(ModelResponseCaptureError, match="differs"):
        import_model_responses(bundle, replay)
    with sqlite3.connect(replay) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_logs WHERE run_id='run-captured'").fetchone()[0] == 0


def test_import_refuses_tampered_run_linkage(tmp_path):
    before, after, replay, bundle = _paths(tmp_path)
    _insert(after, "run-captured", '{"x":1}')
    export_model_responses(before, after, "run-captured", bundle)
    payload = json.loads(bundle.read_text())
    payload["rows"][0][payload["columns"].index("run_id")] = "run-other"
    bundle.write_text(json.dumps(payload))
    with pytest.raises(ModelResponseCaptureError, match="rows changed"):
        import_model_responses(bundle, replay)


def test_import_never_writes_original_pre_session_snapshot(tmp_path):
    before, after, _replay, bundle = _paths(tmp_path)
    _insert(after, "run-captured", '{"x":1}')
    export_model_responses(before, after, "run-captured", bundle)
    with pytest.raises(ModelResponseCaptureError, match="original"):
        import_model_responses(bundle, before)


def test_export_requires_private_directory(tmp_path):
    before, after, _replay, _bundle = _paths(tmp_path)
    _insert(after, "run-captured", '{"x":1}')
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ModelResponseCaptureError, match="not private"):
        export_model_responses(before, after, "run-captured", public / "models.json")
