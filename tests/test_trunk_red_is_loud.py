"""Trunk red must raise an issue, and main pushes must never share a slot."""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import check_main_red as cmr  # noqa: E402


def _group(event, ref, sha):
    wf = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    expr = wf["concurrency"]["group"]
    cancel = wf["concurrency"]["cancel-in-progress"]

    def ev(e):
        e = e.strip()
        if "&&" in e:
            cond, rest = e.split("&&")
            a, b = rest.split("||")
            return ev(a) if ev(cond) else ev(b)
        if "==" in e:
            l, r = e.split("==")
            return ev(l) == ev(r)
        return {"github.workflow": "tests", "github.ref": ref,
                "github.sha": sha, "github.event_name": event}.get(e, e.strip("'"))

    sub = lambda m: str(ev(m.group(1)))
    return re.sub(r"\$\{\{(.*?)\}\}", sub, expr), bool(ev(cancel.strip()[3:-2]))


def test_two_main_pushes_never_share_a_group():
    a, ca = _group("push", "refs/heads/main", "aaa")
    b, cb = _group("push", "refs/heads/main", "bbb")
    assert a != b and not ca and not cb


def test_pull_requests_on_one_branch_still_share_and_cancel():
    a, ca = _group("pull_request", "refs/pull/1/merge", "aaa")
    b, _ = _group("pull_request", "refs/pull/1/merge", "bbb")
    assert a == b and ca


def test_red_streak_keeps_one_issue_and_green_closes():
    t = cmr.ISSUE_PREFIX + " main is red at abcd1234"
    assert cmr.plan_issue_actions([], "abcd1234ff") == {
        "create": True, "close": [], "comment": None}
    same = [{"number": 7, "title": t}]
    assert cmr.plan_issue_actions(same, "abcd1234ff") == {
        "create": False, "close": [], "comment": None}
    # a newer red commit comments on the one issue, never opens a second
    assert cmr.plan_issue_actions(same, "ffff0000aa") == {
        "create": False, "close": [], "comment": 7}
    assert cmr.plan_issue_actions(same, None) == {
        "create": False, "close": [7], "comment": None}
    assert cmr.plan_issue_actions([{"number": 9, "title": "other"}], None)["close"] == []


def test_ten_red_pushes_open_exactly_one_issue():
    issues, created = [], 0
    for n in range(10):
        sha = f"{n:08x}ff"
        plan = cmr.plan_issue_actions(issues, sha)
        if plan["create"]:
            created += 1
            issues.append({"number": 1, "title": f"{cmr.ISSUE_PREFIX} main is red at {sha[:8]}"})
    assert created == 1 and len(issues) == 1


def test_trunk_check_runs_on_push_to_main_and_keeps_schedule():
    wf = yaml.safe_load((ROOT / ".github/workflows/stale-ci.yml").read_text())
    on = wf.get("on", wf.get(True))
    assert on["push"]["branches"] == ["main"] and "schedule" in on
    assert wf["jobs"]["sweep"]["if"] == "github.event_name != 'push'"
    assert "if" not in wf["jobs"]["main-red"]


def test_unsyncable_issue_is_loud(monkeypatch, capsys):
    def boom(args):
        raise cmr.GhError("the repository has disabled issues")
    monkeypatch.setattr(cmr, "_gh", boom)
    assert cmr.sync_issue("o/r", ["x"], "abcd1234ff") is False
    assert "::warning::" in capsys.readouterr().err
