"""The derived money-module scope may only grow: a FIXED written list must stay inside it.

``config/money_scope_fixed.txt`` is the scope ``scripts/money_modules.py`` derived on
2026-10-09 with its name-based fallbacks still in place (385 modules [measured]). It is a
committed file, never recomputed from trunk at test time, so the derivation can move to a
type-based rule (``src.money``) and drop those fallbacks only if no module listed here
leaves the scope. A module that is deleted from the repo is removed from the list by hand
in the same change, so the list shrinks only when the module no longer exists.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import money_modules
from scripts.guard_reference import ReferenceUnavailable

ROOT = Path(__file__).resolve().parent.parent
FIXED = ROOT / "config" / "money_scope_fixed.txt"


def fixed_scope() -> list[str]:
    return [ln for ln in FIXED.read_text().splitlines() if ln and not ln.startswith("#")]


def test_fixed_list_is_well_formed():
    rows = fixed_scope()
    assert rows, "the fixed list is empty"
    assert rows == sorted(set(rows)), "the fixed list must be sorted and free of duplicates"
    assert all(r.endswith(".py") and not r.startswith("tests/") for r in rows)


def test_every_fixed_module_still_exists():
    tracked = set(
        subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--", "*.py"], capture_output=True, text=True, check=True
        ).stdout.splitlines()
    )
    gone = sorted(r for r in fixed_scope() if r not in tracked)
    assert not gone, f"listed in config/money_scope_fixed.txt but no longer tracked (remove by hand): {gone}"


def test_fixed_scope_is_a_subset_of_the_derived_scope():
    try:
        derived = set(money_modules.derive(ROOT))
    except ReferenceUnavailable as exc:
        pytest.fail(f"money scope could not be derived; the superset check cannot run: {exc}")
    lost = sorted(set(fixed_scope()) - derived)
    assert not lost, (
        f"{len(lost)} module(s) left the money scope; the derivation may only widen "
        f"(python {sys.executable} -m scripts.money_modules to see today's scope): {lost}"
    )
