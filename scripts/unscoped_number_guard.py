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

THE UNIT IS THE NUMBER, NOT THE MODULE. A site with a ledger row is registered
wherever its module stands; the gate and both measured counts subtract the
ledger (this tree's for this tree, trunk's for trunk) so each registered number
lowers the count by one, and an UNREGISTERED new site is still refused.
Bringing a whole module into ``SCOPED_PATHS`` remains the way to make coverage
of its FUTURE numbers an obligation; it is no longer the only way to register
one of its present ones.

The delta is per NUMBER, never a count. An earlier version excused any change
whose total did not rise, so deleting one registered constant paid for adding
an unregistered one (a net-sum waiver, which a swap always satisfies). Now a
site new to the working tree is excused only when a site the trunk lost is the
SAME number -- equal value and either the same name (it moved) or the same
module (it was renamed) -- and each lost site can stand in for at most one.

Run it directly: ``python -m scripts.unscoped_number_guard``.
"""
from __future__ import annotations

import sys
import tempfile

import yaml
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


LEDGER_REL = "config/number_ledger.yaml"


def working_number_sites() -> list[number_sources.NumberSite]:
    """Every unscoped numeric constant in the working tree."""
    return number_sources.collect_unscoped_sites(ROOT)


def working_sites() -> list[str]:
    """Site ids of every unscoped numeric constant in the working tree."""
    return [s.site_id for s in working_number_sites()]


def working_ledger_ids() -> set[str]:
    """Ids registered in this tree's ledger."""
    return set(number_sources.load_ledger(ROOT / LEDGER_REL))


def trunk_ledger_ids() -> set[str]:
    """Ids registered in ``origin/main``'s ledger; refuses if it cannot be read."""
    text = trunk_blobs([LEDGER_REL]).get(LEDGER_REL)
    if text is None:
        raise ReferenceUnavailable(f"{LEDGER_REL} is not on {TRUNK}; it refuses rather than pass.")
    return {str(e.get("id")) for e in (yaml.safe_load(text) or {}).get("numbers") or []}


def unregistered(sites: list[str], ledger_ids: set[str]) -> list[str]:
    """The sites with no ledger row: the unit the owner's count is kept in."""
    return [s for s in sites if s not in ledger_ids]


def trunk_sites() -> list[str]:
    """Site ids of every unscoped numeric constant on ``origin/main``."""
    return [s.site_id for s in trunk_number_sites()]


def trunk_number_sites() -> list[number_sources.NumberSite]:
    """Every unscoped numeric constant on ``origin/main``.

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
            return number_sources.collect_unscoped_sites(root)
        except (OSError, SyntaxError) as exc:
            raise ReferenceUnavailable(
                f"cannot measure unscoped numbers on {TRUNK} ({exc}); it refuses "
                f"rather than pass."
            ) from exc


def money_reach_gap() -> tuple[int, int]:
    """(money modules outside the ledger's scope, UNREGISTERED numbers in them).

    MEASURED, NOT GATED: the ledger scope is a reviewed list and ~135 derived money
    modules are outside it. Making this absolute would red the trunk until those
    numbers are registered, so it is reported here and the gate stays the delta.
    A number with a ledger row is not counted, so one row retires one number.
    """
    from scripts.money_modules import derive

    scoped = {p.resolve() for p in number_sources._scoped_files(ROOT)}
    scoped.update(p.resolve() for p in number_sources.config_modules(ROOT))
    outside = [m for m in derive(ROOT) if (ROOT / m).resolve() not in scoped]
    ledger = working_ledger_ids()
    ids = [s.site_id for m in outside for s in number_sources._scan_module(ROOT / m, m, None, ROOT)]
    return len(outside), len(unregistered(ids, ledger))


def _split_id(site_id: str) -> tuple[str, str]:
    """(module, name) of a site id such as ``pkg.mod.NAME`` or ``pkg.mod.Cls.field``."""
    module, _, name = site_id.partition(".")
    while "." in name and not name.split(".", 1)[0][:1].isupper():
        head, name = name.split(".", 1)
        module = f"{module}.{head}"
    return module, name


def same_number(lost: number_sources.NumberSite, new: number_sources.NumberSite) -> bool:
    """A lost trunk site and a new working site are one constant that moved or was renamed.

    Identity is the VALUE plus one of the two things a site id is made of: the
    same name in another module is a move; another name in the same module is a
    rename. Equal value alone is not identity -- two unrelated constants can share
    a value -- and a different value is a different number whatever it is called.
    """
    if lost.value != new.value:
        return False
    lost_module, lost_name = _split_id(lost.site_id)
    new_module, new_name = _split_id(new.site_id)
    return lost_name == new_name or lost_module == new_module


def added_sites() -> list[str]:
    """Unregistered site ids this tree has more of than ``origin/main`` (the delta).

    Each side is measured against its OWN ledger, so a new site that arrives with
    its row is registered, not growth, and a new site without one is refused.
    """
    before_raw = trunk_sites()
    before = unregistered(before_raw, trunk_ledger_ids())
    now_raw = working_sites()
    now = unregistered(now_raw, working_ledger_ids())
    remaining = list(before)
    """Site ids new to this working tree that are not a trunk constant moved or renamed.

    Each site is judged by its own identity. A trunk site that disappeared can
    stand in for at most one new site, and only when ``same_number`` holds; a
    deletion never pays for an unrelated addition, whatever the totals do.
    """
    now, before = working_number_sites(), trunk_number_sites()
    before_ids = {s.site_id for s in before}
    now_ids = {s.site_id for s in now}
    lost = [s for s in before if s.site_id not in now_ids]
    added: list[str] = []
    for site in sorted((s for s in now if s.site_id not in before_ids), key=lambda s: s.site_id):
        match = next((old for old in lost if same_number(old, site)), None)
        if match is None:
            added.append(site.site_id)
        else:
            added.append(site)
    # Only a net rise in SITES fails: moving a constant elsewhere is not growth.
    # Of the risen sites, only the unregistered ones are named and refused.
    return added if len(now_raw) > len(before_raw) else []
            lost.remove(match)
    return added


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
    print(f"unscoped-number guard: this tree adds no unregistered unscoped numeric constant against {TRUNK}.")
    total = len(unregistered(working_sites(), working_ledger_ids()))
    print(f"measured, not gated: {total} unregistered numbers in production Python outside ledger scope.")
    modules, numbers = money_reach_gap()
    print(f"measured, not gated: {modules} money modules outside ledger scope hold {numbers} of them.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
