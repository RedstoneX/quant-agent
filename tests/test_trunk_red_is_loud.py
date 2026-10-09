"""Trunk-red check runs on every main push; main pushes never share a slot."""

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
        return {"github.workflow": "tests", "github.ref": ref, "github.sha": sha, "github.event_name": event}.get(
            e, e.strip("'")
        )

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


def test_trunk_check_runs_on_push_to_main_and_keeps_schedule():
    wf = yaml.safe_load((ROOT / ".github/workflows/stale-ci.yml").read_text())
    on = wf.get("on", wf.get(True))
    assert on["push"]["branches"] == ["main"] and "schedule" in on
    assert wf["jobs"]["sweep"]["if"] == "github.event_name != 'push'"
    assert "if" not in wf["jobs"]["main-red"]
    assert not hasattr(cmr, "sync_issue")
