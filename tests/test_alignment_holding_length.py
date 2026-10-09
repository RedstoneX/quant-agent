"""The holding-length research harness counts holds and censoring correctly."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "alignment_holding_length", REPO / "ops/research/alignment_holding_length.py"
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_holds_are_sessions_to_the_next_exit_and_the_tail_is_censored() -> None:
    # exit flags on sessions 2 and 4 of a six-session series.
    flags = [False, False, True, False, True, False]
    holds, censored = _mod.holds_from_flags(flags)
    # entry 0 -> exit 2 (2), 1 -> 2 (1), 2 -> 4 (2), 3 -> 4 (1);
    # entries 4 and 5 never see a later exit, so they are censored.
    assert holds == [2, 1, 2, 1]
    assert censored == 2


def test_no_exit_anywhere_censors_every_entry() -> None:
    holds, censored = _mod.holds_from_flags([False] * 5)
    assert holds == []
    assert censored == 5
