"""Duplicate-method guard for the pipeline modules, with no stored inventory.

Replaces ``tests/pipeline_method_inventory.json``, a regenerated copy of every
method name that each change to the pipeline had to edit. What that file guarded
is a method DUPLICATED during a move: ``TradingPipeline`` and its mixins are
combined into one object, so two of them defining one name is a silent win for
whichever base comes first. Per docs/GUARDS_WITHOUT_STORED_STATE.md this stores
nothing: at check time it names every owner of a duplicated method in the working
tree, names them again on ``origin/main``, and fails on any IDENTITY (method
name, owning module::class) the tree holds more copies of than the trunk. A move
that leaves one copy is not a duplicate and never fails; a removal never fails.
If ``origin/main`` cannot be read it REFUSES (exit 2); it never passes by default.

Run it directly: ``PYTHONPATH=. .venv/bin/python -m scripts.pipeline_method_guard``.
"""
from __future__ import annotations

import ast
import sys

from scripts.guard_reference import (
    ROOT,
    TRUNK,
    ReferenceUnavailable,
    added_sites,
    trunk_blobs,
)
from scripts.pipeline_method_inventory import TRACKED_MODULES, text_inventory

#: One offender: (method name, "path::Class") for an owner of a duplicated name.
Site = tuple[str, str]

FIX = (
    "Fix: a method must live on exactly one of TradingPipeline or its mixins; "
    "delete the copy you left behind when you moved it."
)


def duplicate_sites(texts: dict[str, str]) -> list[Site]:
    """Every (name, owner) for a non-dunder method defined on 2+ pipeline/mixin classes."""
    owners: dict[str, list[str]] = {}
    for path in sorted(texts):
        for cls, methods in text_inventory(path, texts[path])["classes"].items():
            if cls != "TradingPipeline" and not cls.endswith("Mixin"):
                continue  # stage classes share run/__init__ on purpose; not MRO-combined
            for name in methods:
                if not (name.startswith("__") and name.endswith("__")):
                    owners.setdefault(name, []).append(f"{path}::{cls}")
    return [(n, o) for n, where in sorted(owners.items()) if len(where) > 1 for o in where]


def working_texts() -> dict[str, str]:
    texts: dict[str, str] = {}
    for rel in TRACKED_MODULES:
        path = ROOT / rel
        if not path.is_file():
            raise ReferenceUnavailable(f"{rel} is tracked but missing from the working tree; cannot measure.")
        texts[rel] = path.read_text(encoding="utf-8")
    return texts


def violations() -> list[str]:
    """Every duplicated-method owner this tree holds that ``origin/main`` does not."""
    try:
        now = duplicate_sites(working_texts())
        before = duplicate_sites(trunk_blobs(sorted(TRACKED_MODULES)))
    except SyntaxError as exc:
        raise ReferenceUnavailable(f"cannot parse a tracked module: {exc}") from exc
    return [
        f"method `{name}` is now defined on {owner} as well as elsewhere "
        f"(x{n} here vs x{was} on {TRUNK}). {FIX}"
        for (name, owner), n, was in added_sites(now, before)
    ]


def main(argv: list[str] | None = None) -> int:
    try:
        bad = violations()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        print("Duplicated pipeline methods grew against %s:\n%s" % (TRUNK, "\n".join(bad)), file=sys.stderr)
        return 1
    print(f"pipeline method guard: no new duplicated method against {TRUNK}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
