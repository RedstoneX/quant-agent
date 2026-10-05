"""One resolved location for the desk's data, and no second way to spell it.

Before `src/data_paths.py`, five modules each derived `data/quant_agent.db`
from their own `__file__` position, in two different depths because they sit
at two different nesting levels.  Both spellings happened to land on the same
file; nothing in the tree said so and nothing stopped the next one being wrong.
This test fails if a module starts re-deriving a data path for itself again.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.data_paths import (
    alerting_dir,
    board_dir,
    data_dir,
    db_path,
    diary_dir,
    parse_failure_dir,
    repo_root,
)

SRC = Path(__file__).resolve().parents[1] / "src"

#: `Path(__file__)` walked up with `.parent` hops or `parents[...]` and then
#: joined to the data directory or straight to the database file.
AD_HOC = re.compile(
    r"Path\(__file__\)[^\n]*?(?:\.parent|parents\[\d+\])[^\n]*?"
    r"(?:\"data\"|'data'|quant_agent\.db)"
)

ALLOWED = {"data_paths.py"}


def test_no_module_derives_a_data_path_from_its_own_file_position() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if AD_HOC.search(line):
                offenders.append(f"{path.relative_to(SRC.parent)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "These modules derive a data path from their own __file__ instead of "
        "asking src.data_paths, which is how two different depths got written "
        "for the same file:\n" + "\n".join(offenders)
    )


def test_every_data_path_is_computed_under_the_one_root() -> None:
    root = repo_root()
    assert data_dir() == root / "data"
    assert db_path() == root / "data" / "quant_agent.db"
    assert alerting_dir() == root / "data" / "alerting"
    assert board_dir() == root / "data" / "board"
    assert diary_dir() == root / "data" / "diary"
    assert parse_failure_dir() == root / "data" / "parse_failures"


def test_the_root_is_the_checkout_holding_this_test() -> None:
    assert repo_root() == Path(__file__).resolve().parents[1]


def test_paths_are_recomputed_per_call_and_never_shared() -> None:
    first, second = db_path(), db_path()
    assert first == second
    assert first is not second


def test_the_converted_writers_bind_to_the_one_source() -> None:
    """Each writer's module-level name is bound from `src.data_paths`.

    Asserting the live attribute instead would be order-dependent: other tests
    in the suite monkeypatch these names onto a tmp database for the duration
    of a run, so the check is made against the source, where it is stable.
    """
    expected = {
        "src/alert_watchdog.py": "DB_PATH = db_path()",
        "src/coverage_watchdog.py": "DB_PATH = db_path()",
        "src/silence_watchdog.py": "DB_PATH = db_path()",
        "src/refusal_signature.py": "DB_PATH = db_path()",
        "src/notifier/base.py": "_DB_PATH = db_path()",
    }
    root = SRC.parent
    for rel, binding in expected.items():
        text = (root / rel).read_text()
        assert binding in text, f"{rel} no longer binds its database path from src.data_paths"
        assert "from src.data_paths import" in text, rel
