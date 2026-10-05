"""Unscoped-number guard with no stored ceiling.

A module-level numeric constant in a ``src/`` file outside ``SCOPED_PATHS`` is
a number nobody classified. This used to be held back by a hardcoded
``MAX_UNSCOPED_NUMERIC_SITES`` plus a test pinning its value — a cached
measurement committed to the repo, so every change adding such a constant
edited the same line and collided with every other one.

So this stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md). At check time it
runs the scanner in ``src.number_sources.collect_unscoped_sites`` over the
working tree, runs the SAME scanner (same scope rules) over a throwaway copy of
``origin/main``'s ``src/``, and reports the DELTA. If ``origin/main`` cannot be
read it REFUSES; it never passes by default.

Run it directly: ``python -m scripts.unscoped_number_guard``.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    trunk_blobs,
    trunk_paths,
)
from src import number_sources
from src.number_universe import is_production


def working_sites() -> list[str]:
    """Site ids of every unscoped numeric constant in the working tree."""
    return [s.site_id for s in number_sources.collect_unscoped_sites(ROOT)]


def trunk_sites() -> list[str]:
    """Site ids of every unscoped numeric constant on ``origin/main``.

    The trunk's ``src/`` is written to a temp dir that is deleted on exit; the
    working tree's scope rules are applied to it so both sides are measured by
    the same rule. A scoped path the trunk does not have yet is created empty.
    """
    paths = [p for p in trunk_paths(".py") if is_production(p)]
    blobs = trunk_blobs(paths)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for path, text in blobs.items():
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        (root / "src").mkdir(exist_ok=True)
        for entry in number_sources.SCOPED_PATHS:
            target = root / entry
            if not target.exists():
                if entry.endswith(".py"):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("", encoding="utf-8")
                else:
                    target.mkdir(parents=True, exist_ok=True)
        try:
            return [s.site_id for s in number_sources.collect_unscoped_sites(root)]
        except (OSError, SyntaxError) as exc:
            raise ReferenceUnavailable(
                f"cannot measure unscoped numbers on {TRUNK} ({exc}); it refuses "
                f"rather than pass."
            ) from exc


def money_reach_gap() -> tuple[int, int]:
    """(money modules outside the ledger's scope, numbers a full scan finds in them).

    MEASURED, NOT GATED: the ledger scope is a reviewed list and 135 derived money
    modules are outside it. Making this absolute would red the trunk until those
    numbers are registered, so it is reported here and the gate stays the delta.
    """
    from scripts.money_modules import derive

    scoped = {p.resolve() for p in number_sources._scoped_files(ROOT)}
    scoped.update(p.resolve() for p in number_sources.config_modules(ROOT))
    outside = [m for m in derive(ROOT) if (ROOT / m).resolve() not in scoped]
    count = sum(len(number_sources._scan_module(ROOT / m, m, None, ROOT)) for m in outside)
    return len(outside), count


def added_sites() -> list[str]:
    """Site ids this working tree has more of than ``origin/main`` (the delta)."""
    now, before = working_sites(), trunk_sites()
    remaining = list(before)
    added: list[str] = []
    for site in sorted(now):
        if site in remaining:
            remaining.remove(site)
        else:
            added.append(site)
    # Only a net rise fails: moving a constant elsewhere is not growth.
    return added if len(now) > len(before) else []


def violations() -> list[str]:
    added = added_sites()
    return [f"+{len(added)} unscoped numeric constant(s) vs {TRUNK}: " + ", ".join(added)] if added else []


def main(argv: list[str] | None = None) -> int:
    try:
        bad = violations()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        print(
            "a numeric constant was added in a production file (root, src, ops, scripts) outside SCOPED_PATHS; if it "
            "decides, sizes, prices or exits a trade, bring its module into scope "
            "and ledger it (src/number_sources.py). Delta against %s:\n%s"
            % (TRUNK, "\n".join(bad)),
            file=sys.stderr,
        )
        return 1
    print(f"unscoped-number guard: this tree adds no unscoped numeric constant against {TRUNK}.")
    modules, numbers = money_reach_gap()
    print(f"measured, not gated: {modules} money modules outside ledger scope hold {numbers} unledgered numbers.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
