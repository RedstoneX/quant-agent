"""`from src.portfolio_constructor import <leaf>` must not reach a write name.

That statement executes the package `__init__`, so any trade-writing name the
init defines or imports lands in the importer's reachable set and trips
`test_api_cannot_reach_write_capable_names` through no fault of the author.
"""
import ast

from tests import test_api_cannot_trade as guard

PKG_INIT = guard.SRC / "portfolio_constructor" / "__init__.py"


def _closure_hits(source: str) -> set[str]:
    """The guard's own walk, started from a synthetic importer."""
    write_names = guard.derive_write_names()
    seen, queue, hits = set(), [ast.parse(source)], set()
    while queue:
        tree = queue.pop()
        for mod in guard._imports(tree):
            f = guard._module_file(mod)
            if f and f not in seen and f not in guard._defining_files():
                seen.add(f)
                parsed = guard._parse(f)
                hits |= {f"{guard._rel(f)}::{n}" for n, _ in guard._name_hits(parsed, write_names)}
                queue.append(parsed)
    return hits


def test_package_init_names_no_write_capable_name():
    names = guard.derive_write_names()
    assert not list(guard._name_hits(guard._parse(PKG_INIT), names))


def test_importing_a_leaf_from_the_package_reaches_no_write_name():
    new = _closure_hits("from src.portfolio_constructor import config") - guard.ALLOWLIST
    assert new == set()
