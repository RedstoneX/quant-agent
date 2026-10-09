"""The settlement-fill guard must BITE on a silent supplier, not just pass.

A guard that has never been seen to fail is a guard nobody has tested. These
plant the exact shape that emptied `trades.entry_atr` for the whole life of
that feature, assert the guard names it, then remove the plant and assert the
same guard goes quiet.
"""

from __future__ import annotations

import pytest

from scripts.settlement_fill_guard import (
    built_route_fields,
    offending_sites,
    scan,
)
from src.recording_accessors import pinned_evidence

_LEDGER = """
numbers:
  - id: src.config.Thing.value
    settles_by:
      state: built
      writes: ["trades.entry_atr", "trades.stop_basis"]
  - id: src.config.Other.value
    settles_by:
      state: specified
"""

_VIOLATION = """
def write_entry(db, decision, analysis):
    db.record_trade(
        symbol=decision.symbol,
        entry_atr=getattr(analysis, "atr_14", None),
    )
"""

_CLEAN = """
from src.recording_accessors import pinned_evidence


def write_entry(db, decision, analysis):
    db.record_trade(
        symbol=decision.symbol,
        entry_atr=pinned_evidence(analysis, "atr_14"),
    )
"""

_NONE_LITERAL = """
def write_entry(db):
    db.record_trade(stop_basis=None)
"""


def test_only_built_routes_contribute_fields() -> None:
    assert built_route_fields(_LEDGER) == {"entry_atr", "stop_basis"}


def test_planted_silent_default_is_named() -> None:
    found = offending_sites(_VIOLATION, "src/fake.py", {"entry_atr"})
    assert found == [("src/fake.py", "write_entry", "entry_atr", "silent_default_getattr")]


def test_removing_the_plant_passes() -> None:
    assert offending_sites(_CLEAN, "src/fake.py", {"entry_atr"}) == []


def test_hardcoded_none_is_named() -> None:
    found = offending_sites(_NONE_LITERAL, "src/fake.py", {"stop_basis"})
    assert found and found[0][3] == "hardcoded_none"


def test_a_field_no_built_route_names_is_ignored() -> None:
    assert offending_sites(_VIOLATION, "src/fake.py", {"something_else"}) == []


def test_storage_layer_is_not_scanned() -> None:
    blobs = {"src/storage/db.py": _VIOLATION, "src/stage.py": _VIOLATION}
    paths = {site[0] for site in scan(blobs, {"entry_atr"})}
    assert paths == {"src/stage.py"}


def test_the_real_entry_write_no_longer_uses_a_silent_default() -> None:
    """The two fields measured empty in production are read loudly now, and
    so is the third built-route field the same call pins. The write lives in
    src/entry_record.py, lifted out of the execution stage."""
    from pathlib import Path

    from scripts.guard_reference import ROOT

    text = Path(ROOT, "src/entry_record.py").read_text(encoding="utf-8")
    assert 'entry_atr=pinned_evidence(entry_analysis, "atr_14")' in text
    assert 'stop_basis=pinned_evidence(decision, "stop_rule")' in text
    assert 'requested_risk_pct=pinned_evidence(decision, "requested_risk_pct")' in text
    assert 'getattr(decision, "requested_risk_pct"' not in text


class _Analysis:
    atr_14 = 1.5


def test_pinned_evidence_returns_none_for_an_absent_source() -> None:
    assert pinned_evidence(None, "atr_14") is None


def test_pinned_evidence_reads_a_present_attribute() -> None:
    assert pinned_evidence(_Analysis(), "atr_14") == 1.5


def test_pinned_evidence_raises_instead_of_recording_nothing() -> None:
    with pytest.raises(AttributeError, match="NULL forever"):
        pinned_evidence(_Analysis(), "atr_41")


def test_the_repo_itself_matches_the_fixed_allow_list() -> None:
    """CI's entry point: run the guard over the real tree against the committed list."""
    from scripts.settlement_fill_guard import main

    assert main() == 0, "a settlement-recording field is supplied by a silent default, or the list is stale"


def _list(tmp_path, identities):
    from scripts.check_allowlist import render

    path = tmp_path / "code_settlement_fill.txt"
    path.write_text(render("settlement-fill", identities), encoding="utf-8")
    return path


def test_a_new_silent_supplier_not_in_the_list_fails(tmp_path) -> None:
    from scripts.settlement_fill_guard import check

    unlisted, stale = check({"src/fake.py": _VIOLATION}, {"entry_atr"}, _list(tmp_path, []))
    assert len(unlisted) == 1 and "silent_default_getattr" in unlisted[0] and not stale


def test_a_listed_silent_supplier_passes(tmp_path) -> None:
    from scripts.settlement_fill_guard import check

    listed = _list(tmp_path, [("src/fake.py", "write_entry", "entry_atr", "silent_default_getattr")])
    assert check({"src/fake.py": _VIOLATION}, {"entry_atr"}, listed) == ([], [])


def test_a_stale_entry_fails(tmp_path) -> None:
    from scripts.settlement_fill_guard import check

    listed = _list(tmp_path, [("src/fake.py", "write_entry", "entry_atr", "silent_default_getattr")])
    unlisted, stale = check({"src/fake.py": _CLEAN}, {"entry_atr"}, listed)
    assert not unlisted and len(stale) == 1


def test_the_guard_reads_no_git_trunk() -> None:
    import scripts.settlement_fill_guard as g

    assert not hasattr(g, "trunk_blobs") and "origin/main" not in g.ALLOWLIST.read_text()


def test_the_subject_is_every_tracked_production_module_root_included() -> None:
    from scripts.settlement_fill_guard import _is_subject, _working_subject_paths

    paths = _working_subject_paths()
    assert "main.py" in paths and any(p.startswith("ops/") for p in paths)
    assert not any(p.startswith("tests/") for p in paths)
    assert _is_subject("main.py") and _is_subject("ops/x.py") and not _is_subject("tests/t.py")
    assert offending_sites(_VIOLATION, "main.py", {"entry_atr"}) == [
        ("main.py", "write_entry", "entry_atr", "silent_default_getattr")
    ]
