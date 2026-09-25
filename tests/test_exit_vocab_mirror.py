"""Mechanical guard for the exit-action vocabulary mirror.

`src/api/db_reads.py` deliberately does NOT import the write-capable
`src/storage/db.py` (see tests/test_api_safety.py), so it keeps its OWN copy
of the exit-action vocabulary used to decide when a position chain has gone
flat. Those two copies MUST agree: if the writer counts an action as a real
closed lot but the reader does not, the reader under-counts exits and a
position the broker actually closed reports "open" forever in
get_position_history (item 173(a): exactly what happened when RECONCILED_EXIT
was added to the writer but not the reader).

This test imports both vocabularies and asserts the read side covers every
exit action the write side counts. A reader that UNDER-counts is the bug, so
the invariant is: reader exit vocabulary is a superset of the writer's. Any
future label added to the writer and forgotten on the reader fails CI here.
"""

from __future__ import annotations

from src.api import db_reads
from src.storage import db as write_db


def test_reader_exit_actions_cover_writer_exit_actions() -> None:
    """Every action the writer treats as a chain-closing exit must also be
    recognised by the reader, or get_position_history leaves the chain open."""
    missing = set(write_db._POSITION_EXIT_ACTIONS) - set(
        db_reads._POSITION_EXIT_ACTIONS
    )
    assert not missing, (
        "src/api/db_reads.py._POSITION_EXIT_ACTIONS is missing exit actions "
        f"the writer counts: {sorted(missing)}. The reader will under-count "
        "exits and report closed positions as still open. Add them to the "
        "reader's set to restore the mirror."
    )


def test_reader_exit_prefixes_cover_writer_exit_prefixes() -> None:
    """Same invariant for the prefix families (SELL/COVER/etc.)."""
    missing = set(write_db._POSITION_EXIT_PREFIXES) - set(
        db_reads._POSITION_EXIT_PREFIXES
    )
    assert not missing, (
        "src/api/db_reads.py._POSITION_EXIT_PREFIXES is missing exit prefixes "
        f"the writer counts: {sorted(missing)}."
    )


def test_reader_open_actions_cover_writer_open_actions() -> None:
    """Open-side vocabulary must mirror too: a missing open action would make
    the reader mis-count the entry leg of the same chain."""
    missing = set(write_db._POSITION_OPEN_ACTIONS) - set(
        db_reads._POSITION_OPEN_ACTIONS
    )
    assert not missing, (
        "src/api/db_reads.py._POSITION_OPEN_ACTIONS is missing open actions "
        f"the writer recognises: {sorted(missing)}."
    )


def test_reconciled_exit_is_mirrored() -> None:
    """Item 173(a) regression pin: RECONCILED_EXIT specifically must be on the
    read side, since that is the label whose omission triggered this guard."""
    assert "RECONCILED_EXIT" in write_db._POSITION_EXIT_ACTIONS
    assert "RECONCILED_EXIT" in db_reads._POSITION_EXIT_ACTIONS
