"""The ledger's union merge keeps both sides of independent appends, and the
two existing guards catch every bad shape union can produce from a collision.

Runs real `git merge` in a throwaway repo using the repo's real .gitattributes.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from tests.test_ledger_no_duplicate_keys import duplicate_keys

ROOT = Path(__file__).resolve().parent.parent
LEDGER = "config/number_ledger.yaml"
BASE = 'numbers:\n  - id: a.A\n    value: 1\n    note: "x"\n'


def _entry(i: str, v: int, n: str) -> str:
    return f'  - id: {i}\n    value: {v}\n    note: "{n}"\n'


def _git(d: Path, *a: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)


def _merge(edit1, edit2) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        _git(d, "init", "-q", "-b", "main")
        _git(d, "config", "user.email", "t@t")
        _git(d, "config", "user.name", "t")
        (d / "config").mkdir()
        (d / ".gitattributes").write_text((ROOT / ".gitattributes").read_text())
        (d / LEDGER).write_text(BASE)
        _git(d, "add", LEDGER, ".gitattributes")
        _git(d, "commit", "-qm", "base")
        for name, edit in (("b1", edit1), ("b2", edit2)):
            _git(d, "checkout", "-q", "-b", name, "main")
            (d / LEDGER).write_text(edit(BASE))
            _git(d, "add", LEDGER)
            _git(d, "commit", "-qm", name)
        _git(d, "checkout", "-q", "b1")
        r = _git(d, "merge", "--no-edit", "b2")
        return r.returncode == 0, (d / LEDGER).read_text()


def test_independent_appends_keep_both_without_conflict() -> None:
    ok, text = _merge(lambda b: b + _entry("b.B", 2, "y"), lambda b: b + _entry("c.C", 3, "z"))
    assert ok
    assert [e["id"] for e in yaml.safe_load(text)["numbers"]] == ["a.A", "b.B", "c.C"]
    assert not duplicate_keys(text)


@pytest.mark.parametrize(
    "e1,e2",
    [
        (lambda b: b + _entry("b.B", 2, "y"), lambda b: b + _entry("b.B", 9, "q")),
        (lambda b: b.replace("value: 1", "value: 5"), lambda b: b.replace("value: 1", "value: 6")),
    ],
)
def test_a_colliding_union_is_caught_by_an_existing_guard(e1, e2) -> None:
    ok, text = _merge(e1, e2)
    assert ok
    ids = [e["id"] for e in yaml.safe_load(text)["numbers"]] if not duplicate_keys(text) else []
    assert duplicate_keys(text) or len(ids) != len(set(ids))
