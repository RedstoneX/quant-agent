"""Board item 232: a citation that resolves is not thereby one that substantiates."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import guard_reference, ledger_substantiation_guard as g
from scripts.guard_reference import ReferenceUnavailable

SRC = '''"""doc"""
from os import path
__all__ = ["CEILING", "other"]
# a comment that says 7 is the ceiling
CEILING = 7


def unrelated():
    return "nothing here"


def uses():
    return CEILING * 3
'''


def _verdicts(source: str, value=7, site="src.m.CEILING", tmp: Path | None = None) -> list[str]:
    ledger = {site: {"id": site, "value": value, "source": source}}
    return [p.verdict for p in g.classify(ledger, lambda rel: SRC if rel == "src/m.py" else None)]


@pytest.mark.parametrize("cite,expected", [
    ("src/m.py@`from os import path`", "dead"),
    ("src/m.py::__all__", "dead"),
    ("src/m.py@`# a comment that says 7 is the ceiling`", "dead"),
    ("src/m.py@`__all__ = [\"CEILING\", \"other\"]`", "dead"),
    ("src/m.py::unrelated", "no_mention"),
    ("src/m.py::CEILING", "mentions"),
    ("src/m.py::uses", "mentions"),
    ("src/missing.py::CEILING", "unresolved"),
])
def test_each_landing_is_classified_by_the_ast(cite, expected):
    assert _verdicts(cite) == [expected]


def test_a_symbol_that_never_mentions_the_row_is_not_substantiating():
    """The shape of the five wrong rows: a real symbol, unrelated to the number."""
    assert _verdicts("src/m.py::unrelated", value=99, site="src.m.WINDOW") == ["no_mention"]


def test_the_working_tree_adds_nothing_against_the_trunk():
    bad = g.violations()
    assert not bad, "\n  ".join(bad)


def test_the_ledger_is_actually_measured():
    assert len(g.working_pins()) > 100 and len(g.trunk_pins()) > 100


def test_a_new_bad_pin_is_caught_as_an_identity_not_a_total():
    pins = g.working_pins()
    base = [p for p in pins if p.verdict in g.BAD]
    extra = g.Pin("src.x.NEW", "src/x.py::nothing", "no_mention", "definition names neither")
    swapped = [p for p in pins if p is not base[0]] + [extra]
    bad = g.violations(now=swapped, before=pins)
    assert len(bad) == 1 and "src.x.NEW" in bad[0]  # one removed, one added: net zero, still caught


def test_it_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    repo = tmp_path / "norepo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "number_ledger.yaml").write_text("numbers: []\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "config/number_ledger.yaml"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=repo, check=True)
    monkeypatch.setattr(guard_reference, "ROOT", Path(repo))
    monkeypatch.setattr(g, "ROOT", Path(repo))
    with pytest.raises(ReferenceUnavailable) as exc:
        g.violations(now=[])
    assert "origin/main" in str(exc.value)
    assert g.main() == 2
