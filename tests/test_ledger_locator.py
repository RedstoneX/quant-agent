"""The ledger is found by shape; anything but exactly one match refuses."""

import pytest

from scripts import ledger_locator as ll
from scripts.guard_reference import ReferenceUnavailable

LEDGER = "numbers:\n  - id: a.b\n    value: 1\n    status: sourced\n    site: src/x.py\n"
PROMPT = "numbers:\n  - id: t.x\n    sheet: s.md\n    pattern: 'x'\n    status: arbitrary\n"
HISTORY = "changes:\n  - id: a.b\n"


def _read(blobs):
    return lambda paths: {p: blobs[p] for p in paths if p in blobs}


def test_finds_the_one_ledger_wherever_it_sits():
    blobs = {"cfg/l.yaml": LEDGER, "cfg/p.yaml": PROMPT, "cfg/h.yaml": HISTORY}
    assert ll.locate(list(blobs), _read(blobs), "t") == "cfg/l.yaml"


def test_refuses_when_none_matches():
    blobs = {"cfg/p.yaml": PROMPT, "cfg/h.yaml": HISTORY}
    with pytest.raises(ReferenceUnavailable):
        ll.locate(list(blobs), _read(blobs), "t")


def test_refuses_when_two_match():
    blobs = {"a.yaml": LEDGER, "b.yaml": LEDGER}
    with pytest.raises(ReferenceUnavailable):
        ll.locate(list(blobs), _read(blobs), "t")


def test_real_trunk_ledger_is_found():
    assert ll.working_ledger().endswith("number_ledger.yaml")


def test_history_siblings_count_as_ledger_files():
    assert ll.is_ledger_path("cfg/l_history.yaml", "cfg/l.yaml")
    assert not ll.is_ledger_path("cfg/other.yaml", "cfg/l.yaml")
