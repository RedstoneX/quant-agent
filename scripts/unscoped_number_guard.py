"""Unscoped-number guard against a fixed, committed allow-list.

A module-level numeric constant in a ``src/`` file outside ``SCOPED_PATHS`` is
a number nobody classified. This used to be held back by a hardcoded
``MAX_UNSCOPED_NUMERIC_SITES`` plus a test pinning its value — a cached
measurement committed to the repo, so every change adding such a constant
edited the same line and collided with every other one.

The limit is now FIXED and written in the repo:
``config/check_allowlists/code_unscoped_number.txt`` lists every existing
unscoped constant by site id (``pkg.mod.NAME``), never a count. The guard runs
the scanner in ``src.number_sources.collect_unscoped_sites`` over the working
tree and fails on a site not in the list and on a listed site that no longer
occurs. It reads no git ref, so an unrelated merge cannot redden it. A constant
that moves or is renamed changes its site id, so the list line is edited in the
same change, visibly, with a Guard-rule-change line.

Run it directly: ``python -m scripts.unscoped_number_guard``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.check_allowlist import ALLOWLIST_DIR, compare, report
from scripts.guard_reference import ROOT
from src import number_sources


def working_number_sites() -> list[number_sources.NumberSite]:
    """Every unscoped numeric constant in the working tree."""
    return number_sources.collect_unscoped_sites(ROOT)


def working_sites() -> list[str]:
    """Site ids of every unscoped numeric constant in the working tree."""
    return [s.site_id for s in working_number_sites()]


def money_reach_gap() -> tuple[int, int]:
    """(money modules outside the ledger's scope, numbers a full scan finds in them).

    MEASURED, NOT GATED: the ledger scope is a reviewed list and 135 derived money
    modules are outside it. Making this absolute would red the trunk until those
    numbers are registered, so it is reported here and the gate stays the fixed allow-list.
    """
    from scripts.money_modules import derive

    scoped = {p.resolve() for p in number_sources._scoped_files(ROOT)}
    scoped.update(p.resolve() for p in number_sources.config_modules(ROOT))
    outside = [m for m in derive(ROOT) if (ROOT / m).resolve() not in scoped]
    count = sum(len(number_sources._scan_module(ROOT / m, m, None, ROOT)) for m in outside)
    return len(outside), count


ALLOWLIST = ALLOWLIST_DIR / "code_unscoped_number.txt"


def check(allowlist: Path = ALLOWLIST) -> tuple[list[str], list[str]]:
    """``(unlisted, stale)``: sites missing from the fixed list, and listed ones now gone."""
    return compare([(site,) for site in working_sites()], allowlist)


def main(argv: list[str] | None = None) -> int:
    unlisted, stale = check()
    if unlisted or stale:
        print(
            "a numeric constant was added in a production file (root, src, ops, scripts) outside SCOPED_PATHS; if it "
            "decides, sizes, prices or exits a trade, bring its module into scope "
            "and ledger it (src/number_sources.py):\n" + report(unlisted, stale, ALLOWLIST),
            file=sys.stderr,
        )
        return 1
    print("unscoped-number guard: unscoped numeric constants match the fixed allow-list exactly.")
    modules, numbers = money_reach_gap()
    print(f"measured, not gated: {modules} money modules outside ledger scope hold {numbers} unledgered numbers.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
