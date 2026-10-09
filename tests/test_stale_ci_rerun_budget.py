"""Blind spot 3 of the stale-CI watch (moved out of test_stale_ci_and_drift_state.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name,
        PROJECT_ROOT / "scripts" / filename,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stale_ci = _load("check_stale_ci_rerun_budget_under_test", "check_stale_ci.py")


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
        {"statusCheckRollup": [{"context": stale_ci.REQUIRED_CHECK, "state": "FAILURE"}]}
    )


def test_a_green_required_check_is_never_re_asked():
    """Protection is non-strict, so a green against an older trunk already
    merges; re-running it only spends the cap a blocked change needs."""
    assert not stale_ci.required_check_is_red(_rollup(stale_ci.REQUIRED_CHECK, "SUCCESS"))
    assert not stale_ci.required_check_is_red({"statusCheckRollup": []})
    assert not stale_ci.required_check_is_red({})


def test_an_unrelated_red_advisory_check_does_not_earn_a_rerun():
    """`board-number-advisory` is red on most open PRs and blocks nothing."""
    assert not stale_ci.required_check_is_red(_rollup("board-number-advisory (not required to merge)", "FAILURE"))


def test_a_still_running_required_check_is_not_a_red():
    assert not stale_ci.required_check_is_red(_rollup(stale_ci.REQUIRED_CHECK, None))


def test_the_rerun_budget_is_read_off_the_runs_not_stored():
    runs = [{"event": "pull_request"}, {"event": "workflow_dispatch"}, {"event": "workflow_dispatch"}]
    assert stale_ci.reruns_already_spent(runs) == 2
    assert stale_ci.reruns_already_spent([{"event": "pull_request"}]) == 0
    assert stale_ci.reruns_already_spent([]) == 0


def test_the_budget_is_bounded_so_a_real_failure_stops_costing_runners():
    assert stale_ci.MAX_RERUNS_PER_HEAD >= 1
    spent = [{"event": "workflow_dispatch"}] * stale_ci.MAX_RERUNS_PER_HEAD
    assert stale_ci.reruns_already_spent(spent) >= stale_ci.MAX_RERUNS_PER_HEAD
