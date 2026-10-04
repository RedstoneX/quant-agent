"""A key declared twice in one ledger mapping is refused.

YAML keeps one of the two values and discards the other without any error, so
a ledger row can assert a route or a cost that nobody chose. Found 2026-10-04:
16 rows, 15 repeated `settles_by` and 1 repeated `cost_while_unanswered`.
Parsed with a duplicate-detecting loader, never grepped.
"""

from pathlib import Path

import yaml

LEDGER = Path(__file__).resolve().parent.parent / "config" / "number_ledger.yaml"


class _DupLoader(yaml.SafeLoader):
    pass


def _construct(loader: _DupLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            loader.duplicates.append((key, key_node.start_mark.line + 1))
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_DupLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct)


def duplicate_keys(text: str) -> list[tuple[object, int]]:
    loader = _DupLoader(text)
    loader.duplicates = []
    try:
        loader.get_single_data()
    finally:
        loader.dispose()
    return loader.duplicates


def test_the_ledger_declares_no_key_twice() -> None:
    dups = duplicate_keys(LEDGER.read_text())
    assert not dups, (
        "config/number_ledger.yaml repeats a key inside one mapping; YAML silently "
        f"keeps only one value. (key, line): {dups}"
    )


def test_the_guard_bites_on_a_repeated_key() -> None:
    assert duplicate_keys("a:\n  k: 1\n  k: 2\n") == [("k", 3)]
    assert duplicate_keys("a:\n  k: 1\nb:\n  k: 2\n") == []
