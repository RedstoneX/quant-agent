"""A settlement recording in state `built` must point at a field something
actually WRITES.

WHY THIS EXISTS. On 2026-10-01 three separate settlement recordings -- each
built to settle a number that governs money -- were measured against the
production database and found to have recorded NOTHING. The per-closed-trade
stop evidence, the structural-ceiling flag and the noise-band refusal payload
all had their columns present, their migrations run and their code shipped.
What none of them had was a write that could reach storage: one read the value
off an object with no such field, one never reached the insert, and one built
its payload into a prose string the only persisting call discarded.

"The column exists" is therefore not the check. "Something writes it" is, and
it is the check `src.number_sources.written_fields` performs by reading the
storage layer's own AST: a name counts only when executable code uses it, not
when the `_ensure_column` migration or a docstring merely mentions it.
"""
from __future__ import annotations

import pytest
import yaml

from src.number_sources import (
    REPO_ROOT,
    settlement_route_problem,
    written_fields,
)


def _ledger() -> dict:
    with open(REPO_ROOT / "config" / "number_ledger.yaml", encoding="utf-8") as fh:
        return {n["id"]: n for n in yaml.safe_load(fh)["numbers"]}


def test_every_built_route_names_fields_the_storage_layer_writes() -> None:
    written = written_fields()
    assert written, "the storage-layer AST scan found no written fields at all"
    built = {
        site: entry["settles_by"]
        for site, entry in _ledger().items()
        if isinstance(entry.get("settles_by"), dict)
        and entry["settles_by"].get("state") == "built"
    }
    assert built, (
        "no settlement route is in state `built`; if one was just moved to "
        "`specified` make sure that was deliberate rather than a way past "
        "this check"
    )
    for site, route in built.items():
        targets = route.get("writes")
        assert isinstance(targets, list) and targets, (
            f"{site} declares a BUILT recording but names no `writes:` fields"
        )
        for target in targets:
            field = str(target).rsplit(".", 1)[-1]
            assert field in written, (
                f"{site}: settlement route points at {target!r} but nothing "
                f"in src/storage/db.py writes {field!r}. The recording "
                f"cannot accrue evidence and the number can never settle."
            )


def test_route_check_rejects_a_field_nothing_writes() -> None:
    """The guard must FAIL on a dead field, not merely pass on live ones."""
    entry = {
        "status": "arbitrary",
        "settles_by": {
            "kind": "recording",
            "state": "built",
            "where": "src/storage/db.py",
            "records": "a column that exists in the schema and that no code path anywhere ever assigns",
            "closes_when": "enough rows carry it, which they never will because nothing writes it",
            "writes": ["trades.a_column_no_code_ever_writes"],
        },
    }
    why = settlement_route_problem(entry)
    assert why is not None and "nothing in" in why


def test_route_check_rejects_a_built_route_with_no_writes_list() -> None:
    entry = {
        "status": "arbitrary",
        "settles_by": {
            "kind": "recording",
            "state": "built",
            "where": "src/storage/db.py",
            "records": "something that is claimed to be built but names no field at all",
            "closes_when": "never, because the claim cannot be checked against the storage layer",
        },
    }
    why = settlement_route_problem(entry)
    assert why is not None and "writes:" in why


@pytest.mark.parametrize(
    "field",
    ["entry_atr", "stop_basis", "max_adverse_excursion", "max_favourable_excursion"],
)
def test_migration_only_mention_does_not_count_as_a_write(field: str) -> None:
    """`_ensure_column` creating a column is exactly the thing that fooled the
    desk three times; it must not satisfy the check on its own."""
    source = f'''
def _migrate(conn):
    _ensure_column("trades", "{field}", "{field} REAL")
'''
    assert field not in written_fields(source)
