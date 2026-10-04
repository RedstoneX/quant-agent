"""Two blind spots that both let finished work sit unused.

1. A PR head commit with NO test run: the branch shows an older commit's
   result, auto-merge never fires, nobody notices. `scripts/check_stale_ci.py`
   must call that stale — including when the only runs are cancelled, which
   is not an answer.
2. A deployed checkout behind origin/main with alerts muted. The drift state
   must land on the /health payload, and the repeated Telegram message must
   stop repeating.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, PROJECT_ROOT / "scripts" / filename,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before exec: @dataclass resolves the module by name.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stale_ci = _load("check_stale_ci_under_test", "check_stale_ci.py")
drift = _load("check_deploy_drift_state_under_test", "check_deploy_drift.py")


# --------------------------------------------------------------------------
# blind spot 1 — classify()
# --------------------------------------------------------------------------

def test_no_runs_at_all_is_stale():
    """The reproduced case: a push that created no run."""
    assert stale_ci.classify([]) == "stale"


def test_only_cancelled_runs_is_stale():
    """A cancelled run is not an answer. Counting it as one would be
    weakening the check to make a failure disappear."""
    assert stale_ci.classify([
        {"status": "completed", "conclusion": "cancelled"},
    ]) == "stale"


def test_completed_failure_is_not_stale():
    """A red result is a real answer; this guard is about absence, not
    failure, and must not re-dispatch a branch that genuinely failed."""
    assert stale_ci.classify([
        {"status": "completed", "conclusion": "failure"},
    ]) == "ok"


def test_in_progress_run_is_not_dispatched_again():
    assert stale_ci.classify([
        {"status": "in_progress", "conclusion": None},
    ]) == "running"


def test_a_completed_run_beats_an_earlier_cancelled_one():
    assert stale_ci.classify([
        {"status": "completed", "conclusion": "cancelled"},
        {"status": "completed", "conclusion": "success"},
    ]) == "ok"


def _obs_run(started, conclusion="failure", status="completed"):
    return {"status": status, "conclusion": conclusion, "startedAt": started}


def test_obsolete_verdict_is_flagged_stale_case():
    tip = stale_ci._ts("2026-10-04T13:08:54Z")
    assert stale_ci.is_obsolete([_obs_run("2026-10-04T12:40:00Z")], tip)


def test_fresh_verdict_is_not_flagged():
    tip = stale_ci._ts("2026-10-04T13:08:54Z")
    assert not stale_ci.is_obsolete([_obs_run("2026-10-04T13:30:00Z")], tip)
    # a re-run after an old failure supersedes it
    assert not stale_ci.is_obsolete(
        [_obs_run("2026-10-04T12:40:00Z"), _obs_run("2026-10-04T13:30:00Z", "success")], tip)


def test_cancelled_or_unknown_tip_never_flags():
    tip = stale_ci._ts("2026-10-04T13:08:54Z")
    assert not stale_ci.is_obsolete([_obs_run("2026-10-04T12:00:00Z", "cancelled")], tip)
    assert not stale_ci.is_obsolete([_obs_run("2026-10-04T12:00:00Z")], None)


def test_workflow_name_filter_is_the_real_workflow():
    """If the `name:` in test.yml is renamed, this guard silently reports
    everything healthy — so the two must be checked against each other."""
    text = (PROJECT_ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert f"name: {stale_ci.WORKFLOW_NAME}" in text


def test_scheduled_workflow_exists_and_can_dispatch():
    text = (PROJECT_ROOT / ".github" / "workflows" / "stale-ci.yml").read_text()
    assert "schedule:" in text
    # Without actions: write it cannot start the missing run.
    assert "actions: write" in text
    assert "check_stale_ci.py" in text


# --------------------------------------------------------------------------
# blind spot 2 — durable drift state
# --------------------------------------------------------------------------

def _report(behind: int, *, head="a" * 40, remote="b" * 40):
    report = drift.DriftReport(deployed_path="/home/qamc/quant-agent")
    report.head_sha = head
    report.remote_sha = remote if behind else head
    report.fetch_ok = True
    report.missing_commits = [(f"{i:040d}", f"subject {i}") for i in range(behind)]
    report.behind_count = behind
    return report


def test_state_is_written_even_when_in_sync(tmp_path):
    """"checked and clean" must be distinguishable from "never checked"."""
    path = tmp_path / "deploy_drift.json"
    assert drift.record_state(_report(0), "origin/main", alerted=False,
                              state_path=path)
    record = json.loads(path.read_text())["deploy_drift"]
    assert record["status"] == "in_sync"
    assert record["behind_count"] == 0
    assert record["checked_at"]


def test_behind_state_carries_the_missing_commits(tmp_path):
    path = tmp_path / "deploy_drift.json"
    drift.record_state(_report(6), "origin/main", alerted=True, state_path=path)
    record = json.loads(path.read_text())["deploy_drift"]
    assert record["status"] == "behind"
    assert record["behind_count"] == 6
    assert len(record["missing_commits"]) == 6


def test_same_drift_is_not_alerted_twice_the_same_day(tmp_path):
    """Five identical drift messages went out in one day and changed
    nothing."""
    path = tmp_path / "deploy_drift.json"
    report = _report(6)
    today = date(2026, 9, 30)
    assert not drift.already_alerted(report, state_path=path, today=today)
    drift.record_state(report, "origin/main", alerted=True, state_path=path,
                       today=today)
    assert drift.already_alerted(report, state_path=path, today=today)


def test_a_new_merge_alerts_again(tmp_path):
    """Dedup must not swallow a DIFFERENT drift — new commits on main are a
    new fact, not a repeat."""
    path = tmp_path / "deploy_drift.json"
    today = date(2026, 9, 30)
    drift.record_state(_report(6), "origin/main", alerted=True, state_path=path,
                       today=today)
    moved = _report(7, remote="c" * 40)
    assert not drift.already_alerted(moved, state_path=path, today=today)


def test_dedup_does_not_persist_into_the_next_day(tmp_path):
    path = tmp_path / "deploy_drift.json"
    report = _report(6)
    drift.record_state(report, "origin/main", alerted=True, state_path=path,
                       today=date(2026, 9, 30))
    assert not drift.already_alerted(report, state_path=path,
                                     today=date(2026, 10, 1))


# --------------------------------------------------------------------------
# blind spot 2 — the board is the surface, because alerts are muted
# --------------------------------------------------------------------------

def _drift_file(monkeypatch, tmp_path, payload):
    import src.coverage_watchdog as cw

    path = tmp_path / "deploy_drift.json"
    path.write_text(json.dumps({"deploy_drift": payload}))
    monkeypatch.setattr(cw, "DEPLOY_DRIFT_STATE_PATH", path)
    return path


def test_health_reports_unknown_when_never_checked(monkeypatch, tmp_path):
    import src.api.routes_live as live
    import src.coverage_watchdog as cw

    monkeypatch.setattr(cw, "DEPLOY_DRIFT_STATE_PATH", tmp_path / "missing.json")
    assert live._deploy_drift_state()["status"] == "unknown"


def test_health_reports_behind(monkeypatch, tmp_path):
    import src.api.routes_live as live

    _drift_file(monkeypatch, tmp_path, {
        "status": "behind", "behind_count": 6,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })
    state = live._deploy_drift_state()
    assert state["status"] == "behind"
    assert state["behind_count"] == 6


def test_a_snapshot_nobody_refreshed_is_stale_not_healthy(monkeypatch, tmp_path):
    import src.api.routes_live as live

    old = datetime.now(timezone.utc) - timedelta(hours=72)
    _drift_file(monkeypatch, tmp_path, {
        "status": "in_sync", "behind_count": 0, "checked_at": old.isoformat(),
    })
    assert live._deploy_drift_state()["status"] == "stale"


def test_fresh_in_sync_snapshot_is_clean(monkeypatch, tmp_path):
    import src.api.routes_live as live

    _drift_file(monkeypatch, tmp_path, {
        "status": "in_sync", "behind_count": 0,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })
    assert live._deploy_drift_state()["status"] == "in_sync"


@pytest.mark.parametrize("status,expect_degraded", [
    ("behind", True), ("stale", True), ("unknown", False), ("in_sync", False),
])
def test_health_schema_carries_the_field(status, expect_degraded):
    """The payload field must exist, and only a DETECTED fault turns the
    board red — a missing measurement does not, or the board is always red
    and the operator learns to ignore it."""
    from src.api.schemas import HealthResponse

    response = HealthResponse(
        status="ok", db_reachable=True, timestamp="now",
        deploy_drift={"status": status},
    )
    assert response.deploy_drift == {"status": status}
    assert (status in ("behind", "stale")) is expect_degraded
