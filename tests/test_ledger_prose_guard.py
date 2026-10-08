"""The ledger's prose may only NAME things that still exist (resolution check).

Separate from tests/test_ledger_citations.py, which resolves citations, and
from any check that a citation substantiates its number.
"""
from scripts import ledger_prose_guard


def _row(note: str) -> dict:
    return {"row.x": {"id": "row.x", "note": note, "status": "sourced"}}


def test_the_real_ledger_names_only_things_that_exist() -> None:
    assert ledger_prose_guard.violations() == []


def test_a_note_naming_a_symbol_that_does_not_exist_is_refused() -> None:
    bad = ledger_prose_guard.violations(
        _row("it is `totally_vanished_helper` in src/rotation.py::no_such_thing")
    )
    assert any("totally_vanished_helper" in v for v in bad)
    assert any("no_such_thing" in v for v in bad)


def test_a_note_naming_a_missing_path_or_missing_member_is_refused() -> None:
    bad = ledger_prose_guard.violations(
        _row("see `src/gone_module.py` and `src.rotation.no_such_member`")
    )
    assert any("gone_module" in v and "does not exist" in v for v in bad)
    assert any("no_such_member" in v for v in bad)


def test_names_that_exist_and_plain_words_pass() -> None:
    assert ledger_prose_guard.violations(
        _row("`load_ledger` in `src.number_sources.load_ledger` and the `funding` word")
    ) == []
