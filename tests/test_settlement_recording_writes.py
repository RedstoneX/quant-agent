"""A settlement recording in state `built` must point at a column something
actually WRITES -- to the table it names.

WHY THIS EXISTS. On 2026-10-01 three separate settlement recordings -- each
built to settle a number that governs money -- were measured against the
production database and found to have recorded NOTHING. The per-closed-trade
stop evidence, the structural-ceiling flag and the noise-band refusal payload
all had their columns present, their migrations run and their code shipped.
What none of them had was a write that could reach storage.

"The column exists" is therefore not the check. The FIRST version of the real
check was still not it either: it matched the bare column name as a WORD in
three hardcoded storage files, so a column nothing writes passed if the word
occurred anywhere, a genuine write in any other file could not pass at all,
and the table was never compared. The check now resolves identity instead --
does some INSERT or UPDATE in the tree write THIS column of THIS table --
from `src.storage_write_index`, which also documents the one dynamic shape it
accepts as a deliberate strict proxy.
"""

from __future__ import annotations

import pytest
import yaml

from src.number_sources import REPO_ROOT, settlement_route_problem
from src.storage_write_index import written_columns


def _ledger() -> dict:
    with open(REPO_ROOT / "config" / "number_ledger.yaml", encoding="utf-8") as fh:
        return {n["id"]: n for n in yaml.safe_load(fh)["numbers"]}


def _route(writes: list[str]) -> dict:
    return {
        "status": "arbitrary",
        "settles_by": {
            "kind": "recording",
            "state": "built",
            "where": "src/storage/db.py",
            "records": "a settlement recording written only to exercise this guard",
            "closes_when": "enough rows carry the field this route claims to write",
            "writes": writes,
        },
    }


def test_every_built_route_names_columns_the_storage_layer_writes() -> None:
    written = written_columns()
    assert written, "the storage-write scan found no written columns at all"
    built = {
        site: entry
        for site, entry in _ledger().items()
        if isinstance(entry.get("settles_by"), dict) and entry["settles_by"].get("state") == "built"
    }
    assert built, (
        "no settlement route is in state `built`; if one was just moved to "
        "`specified` make sure that was deliberate rather than a way past "
        "this check"
    )
    for site, entry in built.items():
        why = settlement_route_problem(entry)
        assert why is None, f"{site}: {why}"


def test_a_column_a_real_write_names_is_allowed() -> None:
    """PROOF 1 -- a genuinely written column passes."""
    assert ("specialist_evidence", "evidence_json") in written_columns()
    assert settlement_route_problem(_route(["specialist_evidence.evidence_json"])) is None


def test_a_word_in_a_storage_file_is_not_a_write() -> None:
    """PROOF 2 -- the hole. The word occurs in storage source; nothing writes it."""
    source = '''
WHERE_CLAUSE = "SELECT consecutive_misses FROM intraday_symbol_health"

def record(conn, value):
    """Mentions consecutive_misses in prose, and reads it above."""
    conn.execute("INSERT INTO trades (symbol) VALUES (?)", (value,))
'''
    written = written_columns(source)
    assert ("trades", "symbol") in written
    assert not any(c == "consecutive_misses" for _, c in written)


def test_a_write_outside_the_old_hardcoded_files_is_allowed() -> None:
    """PROOF 3 -- the false refusal. `trade_refusals` is written by
    `src/storage/trades/trade_refusals_store.py`, which was in none of the
    three files the old check read, and through a joined column tuple."""
    assert ("trade_refusals", "requested_risk_pct") in written_columns()
    assert settlement_route_problem(_route(["trade_refusals.requested_risk_pct"])) is None


def test_a_real_column_of_the_wrong_table_is_refused() -> None:
    """PROOF 4 -- `requested_risk_pct` is real, but not on `daily_pnl`."""
    why = settlement_route_problem(_route(["daily_pnl.requested_risk_pct"]))
    assert why is not None and "not to 'daily_pnl'" in why


def test_unreadable_source_refuses_rather_than_passing() -> None:
    """PROOF 5 -- a scan that cannot see the code must raise, never return
    an empty index, which would wave every route through."""
    with pytest.raises(FileNotFoundError):
        written_columns(root=REPO_ROOT / "no" / "such" / "directory")
    with pytest.raises(SyntaxError):
        written_columns("def broken(:\n")


def test_route_check_rejects_a_field_nothing_writes() -> None:
    """The guard must FAIL on a dead field, not merely pass on live ones."""
    why = settlement_route_problem(_route(["trades.a_column_no_code_ever_writes"]))
    assert why is not None and "no INSERT or UPDATE" in why


def test_route_check_rejects_a_built_route_with_no_writes_list() -> None:
    entry = _route([])
    del entry["settles_by"]["writes"]
    why = settlement_route_problem(entry)
    assert why is not None and "writes:" in why


@pytest.mark.parametrize(
    "column",
    ["entry_atr", "stop_basis", "max_adverse_excursion", "max_favourable_excursion"],
)
def test_migration_only_mention_does_not_count_as_a_write(column: str) -> None:
    """`_ensure_column` creating a column is exactly the thing that fooled the
    desk three times; it must not satisfy the check on its own."""
    source = f'''
def _migrate(conn):
    _ensure_column("trades", "{column}", "{column} REAL")
'''
    assert not any(c == column for _, c in written_columns(source))
