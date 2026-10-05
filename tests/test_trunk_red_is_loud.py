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


def test_red_opens_one_issue_per_commit_and_green_closes():
    t = cmr.ISSUE_PREFIX + " main is red at abcd1234"
    assert cmr.plan_issue_actions([], "abcd1234ff") == {"create": True, "close": []}
    same = [{"number": 7, "title": t}]
    assert cmr.plan_issue_actions(same, "abcd1234ff") == {"create": False, "close": []}
    assert cmr.plan_issue_actions(same, "ffff0000aa") == {"create": True, "close": [7]}
    assert cmr.plan_issue_actions(same, None) == {"create": False, "close": [7]}
    assert cmr.plan_issue_actions([{"number": 9, "title": "other"}], None)["close"] == []
