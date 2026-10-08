"""Duplicate-method guard for the pipeline modules: an ABSOLUTE rule.

``TradingPipeline`` and its mixins are combined into one object, so two of them
defining one name is a silent win for whichever base comes first. The rule is
absolute: no method name is owned by two pipeline classes. The pairs that exist
today are pinned by identity (method name | owning module::class) in the fixed
list ``config/check_allowlists/struct_pipeline_method.txt``; nothing is compared
with any trunk. The check fails on a duplicate owner not in the list and on a
listed owner that no longer duplicates (stale entry).

Run it directly: ``PYTHONPATH=. .venv/bin/python -m scripts.pipeline_method_guard``.
"""
from __future__ import annotations

import ast
import sys

from scripts import struct_allowlist
from scripts.struct_allowlist import ROOT
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


class ReferenceUnavailable(RuntimeError):
    """A tracked module could not be read or parsed, so nothing can be measured."""


def working_texts() -> dict[str, str]:
    texts: dict[str, str] = {}
    for rel in TRACKED_MODULES:
        path = ROOT / rel
        if not path.is_file():
            raise ReferenceUnavailable(f"{rel} is tracked but missing from the working tree; cannot measure.")
        texts[rel] = path.read_text(encoding="utf-8")
    return texts


def found() -> list[str]:
    """Every duplicated-method owner in the working tree, as ``name | owner``."""
    try:
        return [f"{name} | {owner}" for name, owner in duplicate_sites(working_texts())]
    except SyntaxError as exc:
        raise ReferenceUnavailable(f"cannot parse a tracked module: {exc}") from exc


def violations(directory=None) -> list[str]:
    """Duplicate owners not in the fixed list, and listed owners that no longer duplicate."""
    return struct_allowlist.problems("pipeline_method", found(), FIX, directory)


def main(argv: list[str] | None = None) -> int:
    try:
        bad = violations()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        print("Duplicated pipeline methods differ from the fixed list:\n%s" % "\n".join(bad), file=sys.stderr)
        return 1
    print("pipeline method guard: duplicated methods match the fixed list.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
