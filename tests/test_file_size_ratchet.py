"""File-size ratchet: no tracked Python file (tests included) may grow.

Two source files once reached 19,100 and 10,200 lines because nothing stopped
them. Baselines live in tests/file_size_baseline.json (generated, never
hand-edited); shrink a file, then run ``python -m scripts.regen_file_size_baseline``
to lock the gain in. Files already over the ceiling are legacy debt: they may
only shrink. No other file may cross the ceiling.
"""
from __future__ import annotations

from scripts.regen_file_size_baseline import (
    CEILING,
    FLOOR,
    SHRINK_SLACK,
    load_baseline,
    tracked_py_files,
    line_count,
)

_FIX = "python -m scripts.regen_file_size_baseline"


def _sizes() -> dict[str, int]:
    return {p: line_count(p) for p in tracked_py_files()}


def test_no_file_grows_past_its_baseline():
    base, bad = load_baseline(), []
    for path, n in _sizes().items():
        limit = base.get(path, FLOOR)
        if n > limit:
            bad.append(f"{path}: {n} lines > allowed {limit}. Split it instead of growing it.")
    assert not bad, "Files grew past their size baseline:\n" + "\n".join(bad)


def test_no_new_file_crosses_the_hard_ceiling():
    base = load_baseline()
    bad = [
        f"{p}: {n} lines > ceiling {CEILING}"
        for p, n in _sizes().items()
        if n > CEILING and p not in base
    ]
    assert not bad, "Files over the hard ceiling:\n" + "\n".join(bad)


def test_baseline_is_tight():
    sizes, base, bad = _sizes(), load_baseline(), []
    for path, limit in base.items():
        if path not in sizes:
            bad.append(f"{path}: in baseline but no longer tracked")
        elif sizes[path] < limit - SHRINK_SLACK:
            bad.append(f"{path}: shrank {limit} -> {sizes[path]}; baseline is slack")
    assert not bad, f"Good news, but re-tighten the ratchet with `{_FIX}`:\n" + "\n".join(bad)
