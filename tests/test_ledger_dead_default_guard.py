"""A dead-default excuse in the number ledger must name a caller that really passes the argument."""
from __future__ import annotations

import yaml

from scripts import ledger_dead_default_guard as g

CALLEE = "def build(self, lookback_days=5, top_n=15):\n    return top_n\n"
GOOD = "class S:\n    def run(self):\n        return self.build(lookback_days=5, top_n=15)\n"
NOPASS = "class S:\n    def run(self):\n        return self.build(lookback_days=5)\n"
NOCALL = "class S:\n    def run(self):\n        return 1\n"
POSITIONAL = "class S:\n    def run(self):\n        return self.build(5, 15)\n"
SAME_NAME = "from src import m\n\ndef build():\n    return m.build(top_n=15)\n"
SAME_FILE_SIBLING = "class S:\n    def build(self, lookback_days=5, top_n=15):\n        return top_n\n\n" \
                    "def build():\n    return S().build(top_n=15)\n"
FILES = {"src/m.py": CALLEE, "src/good.py": GOOD, "src/nopass.py": NOPASS, "src/nocall.py": NOCALL,
         "src/pos.py": POSITIONAL, "src/same_name.py": SAME_NAME, "src/sib.py": SAME_FILE_SIBLING}


def _row(cite: str, param: str = "top_n") -> str:
    note = f"Dead default ({cite}): the one caller passes it explicitly." if cite else "Dead default: passed."
    return yaml.safe_dump({"numbers": [{"id": f"src.m.build({param})", "value": 15, "site": "src/m.py",
                                        "status": "not-trade-governing", "note": note}]})


def _bad(cite: str, param: str = "top_n") -> list[str]:
    return g.check(_row(cite, param), FILES.get)


def test_true_excuse_passes_by_keyword_and_by_position():
    assert _bad("src/good.py::S.run") == []
    assert _bad("src/pos.py::S.run") == []


def test_caller_that_never_passes_the_argument_is_refused():
    assert "never passes top_n" in _bad("src/nopass.py::S.run")[0]


def test_citation_that_never_calls_the_method_is_refused():
    assert "never calls build" in _bad("src/nocall.py::S.run")[0]


def test_dangling_self_and_missing_citations_are_refused():
    assert "not defined" in _bad("src/good.py::S.gone")[0]
    assert "does not exist" in _bad("src/none.py::S.run")[0]
    assert "definition itself" in _bad("src/m.py::build")[0]
    assert "cites no caller" in _bad("")[0]


def test_a_caller_that_shares_the_methods_name_is_a_caller_not_the_definition():
    # Identity, not spelling: `build` in another file calling `m.build` is a real caller.
    assert _bad("src/same_name.py::build") == []


def test_same_file_same_name_is_judged_by_position():
    sib = yaml.safe_dump({"numbers": [{"id": "src.sib.S.build(top_n)", "value": 15, "site": "src/sib.py",
                                       "note": "Dead default (src/sib.py::build): the caller passes it."}]})
    assert g.check(sib, FILES.get) == []
    self_cite = sib.replace("src/sib.py::build", "src/sib.py::S.build")
    assert "definition itself" in g.check(self_cite, FILES.get)[0]


def test_a_row_whose_definition_cannot_be_found_is_refused_not_excused():
    gone = yaml.safe_dump({"numbers": [{"id": "src.m.vanished(top_n)", "value": 15, "site": "src/m.py",
                                        "note": "Dead default (src/good.py::S.run): passed."}]})
    assert "cannot be told" in g.check(gone, FILES.get)[0]


def test_row_without_the_excuse_is_not_judged():
    text = yaml.safe_dump({"numbers": [{"id": "src.m.build(top_n)", "value": 1, "note": "plain"}]})
    assert g.check(text, FILES.get) == []


def test_the_real_ledger_holds_no_false_dead_default_excuse():
    assert g.violations() == []
