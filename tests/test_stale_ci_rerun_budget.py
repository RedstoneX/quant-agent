"""Blind spot 3 of the stale-CI watch (moved out of test_stale_ci_and_drift_state.py)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, PROJECT_ROOT / "scripts" / filename,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stale_ci = _load("check_stale_ci_rerun_budget_under_test", "check_stale_ci.py")


# --------------------------------------------------------------------------
# blind spot 3 — main's own post-merge run (scripts/check_main_red.py)
#
# Branch protection reads a PR's `pytest` check and does not require the
# branch to be current with main first, so main can go red while every open
# PR is green. Nothing read main's own run until this watch existed.
# --------------------------------------------------------------------------
main_red = _load("check_main_red_under_test", "check_main_red.py")


def _run(conclusion, *, status="completed", event="push", workflow="tests",
         sha="deadbeef", created="2026-10-01T04:00:59Z"):
    return {
        "workflowName": workflow, "status": status, "conclusion": conclusion,
        "event": event, "headSha": sha, "createdAt": created,
        "databaseId": 1, "displayTitle": "a change", "url": "https://x/1",
    }


def test_a_failed_push_run_on_main_is_red():
    assert main_red.verdict([_run("failure")]) == "red"


def test_a_cancelled_newest_run_is_not_treated_as_a_pass():
    """A cancelled run is not an answer; calling it green would be the
    exact weakening `check_stale_ci.py` already refuses."""
    assert main_red.verdict([_run("cancelled"), _run("success")]) == "red"


def test_a_passing_newest_push_run_is_green():
    assert main_red.verdict([_run("success"), _run("failure")]) == "green"


def test_a_pull_request_run_is_not_a_verdict_on_main():
    """A PR run is about a branch. Counting it would reproduce the very
    confusion this watch exists to end."""
    assert main_red.verdict([_run("success", event="pull_request"),
                             _run("failure")]) == "red"


def test_an_unfinished_run_does_not_hide_the_finished_failure_under_it():
    assert main_red.verdict([_run(None, status="in_progress"),
                             _run("failure")]) == "red"


def test_another_workflow_cannot_vouch_for_the_test_workflow():
    assert main_red.verdict([_run("success", workflow="stale-ci"),
                             _run("failure")]) == "red"


def test_no_completed_run_is_unknown_not_green():
    assert main_red.verdict([_run(None, status="queued")]) == "unknown"
    assert main_red.verdict([]) == "unknown"


def test_the_streak_reports_how_long_main_has_been_red():
    runs = [_run("failure", sha="aaa", created="2026-10-01T04:00:59Z"),
            _run("failure", sha="bbb", created="2026-10-01T03:59:29Z"),
            _run("success", sha="ccc", created="2026-10-01T03:55:51Z")]
    streak = main_red.red_streak(runs)
    assert [r["headSha"] for r in streak] == ["aaa", "bbb"]
    text = "\n".join(main_red.record_lines("main", streak))
    assert "aaa" in text and "red since  : 2026-10-01T03:59:29Z" in text
    assert "2 consecutive non-passing push runs" in text


def test_the_record_stands_alone_for_a_single_red_run():
    text = "\n".join(main_red.record_lines("main", [_run("failure")]))
    assert "main is RED" in text and "deadbeef" in text
    assert "red since" not in text


def test_an_api_failure_is_not_reported_as_a_green_main(monkeypatch):
    """A GitHub hiccup must never be mistaken for a healthy branch."""
    def _boom(*_a, **_k):
        raise main_red.GhError("rate limited")
    monkeypatch.setattr(main_red, "branch_runs", _boom)
    assert main_red.main(["--repo", "o/n"]) == 2


def test_a_red_main_exits_non_zero_and_writes_a_durable_record(
    monkeypatch, tmp_path,
):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(main_red, "branch_runs",
                        lambda *_a, **_k: [_run("failure")])
    assert main_red.main(["--repo", "o/n"]) == 1
    assert "main is RED" in summary.read_text()
    assert "deadbeef" in summary.read_text()


def test_a_green_main_exits_zero(monkeypatch):
    monkeypatch.setattr(main_red, "branch_runs",
                        lambda *_a, **_k: [_run("success")])
    assert main_red.main(["--repo", "o/n"]) == 0


def test_the_scheduled_workflow_actually_runs_this_watch():
    """A script nothing invokes is not a watch."""
    text = (PROJECT_ROOT / ".github/workflows/stale-ci.yml").read_text()
    assert "scripts/check_main_red.py" in text


# --------------------------------------------------------------------------
# blind spot 3 — an obsolete verdict is only worth re-asking when it is RED,
# and only a bounded number of times.
#
# MEASURED 2026-10-04 19:27 UTC against the live repository: main moves every
# ~17 minutes and this sweep runs every 30, so "the newest run predates main's
# tip" held for all 26 open PRs at once, green ones included. Re-running on
# that alone is a re-run-everything loop (480 runs/day) that also re-asks a
# permanently-broken change 48 times a day. These tests pin the two conditions
# that make a re-run mean something.
# --------------------------------------------------------------------------

def _rollup(name, verdict):
    return {"statusCheckRollup": [{"name": name, "conclusion": verdict}]}


def test_a_red_required_check_is_what_earns_a_rerun():
    """The case the guard must still act on after this narrowing."""
    assert stale_ci.required_check_is_red(_rollup(stale_ci.REQUIRED_CHECK, "FAILURE"))


def test_a_commit_status_spelling_is_read_too():
    assert stale_ci.required_check_is_red(
        {"statusCheckRollup": [{"context": stale_ci.REQUIRED_CHECK, "state": "FAILURE"}]})


def test_a_green_required_check_is_never_re_asked():
    """Protection is non-strict, so a green against an older trunk already
    merges; re-running it only spends the cap a blocked change needs."""
    assert not stale_ci.required_check_is_red(_rollup(stale_ci.REQUIRED_CHECK, "SUCCESS"))
    assert not stale_ci.required_check_is_red({"statusCheckRollup": []})
    assert not stale_ci.required_check_is_red({})


def test_an_unrelated_red_advisory_check_does_not_earn_a_rerun():
    """`board-number-advisory` is red on most open PRs and blocks nothing."""
    assert not stale_ci.required_check_is_red(
        _rollup("board-number-advisory (not required to merge)", "FAILURE"))


def test_a_still_running_required_check_is_not_a_red():
    assert not stale_ci.required_check_is_red(_rollup(stale_ci.REQUIRED_CHECK, None))


def test_the_rerun_budget_is_read_off_the_runs_not_stored():
    runs = [{"event": "pull_request"}, {"event": "workflow_dispatch"},
            {"event": "workflow_dispatch"}]
    assert stale_ci.reruns_already_spent(runs) == 2
    assert stale_ci.reruns_already_spent([{"event": "pull_request"}]) == 0
    assert stale_ci.reruns_already_spent([]) == 0


def test_the_budget_is_bounded_so_a_real_failure_stops_costing_runners():
    assert stale_ci.MAX_RERUNS_PER_HEAD >= 1
    spent = [{"event": "workflow_dispatch"}] * stale_ci.MAX_RERUNS_PER_HEAD
    assert stale_ci.reruns_already_spent(spent) >= stale_ci.MAX_RERUNS_PER_HEAD
