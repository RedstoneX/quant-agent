"""Boundary witness: the lifted number-site scanner runs with no ledger, scope list
or `src.number_sources` module state behind it.

Every scanner function takes its inputs as arguments (a parsed tree, the module
name, the names a module binds), so the module is exercised from a tree built in
the test and nothing else (clause 5 of tests/boundary_harness.py).
"""

from __future__ import annotations

import ast
import textwrap

from src.number_site_scan import (
    CONFIG_CLASS_SUFFIX,
    FACTOR_BAND,
    NEUTRAL_VALUES,
    NumberSite,
    _factor_operands,
    _field_default,
    _imported_constants,
    _leaves,
    _module_constants,
    _numeric,
    _own_nodes,
    _qualified_scopes,
    _scan_extended_shapes,
)
from tests.boundary_harness import check_boundary


def test_number_site_scan_module_passes_the_boundary_check():
    verdict = check_boundary("src.number_site_scan")
    # Clause 1 names exactly the frozen @dataclass `NumberSite`, whose __init__
    # is generated, not written; the same reading the harness gives
    # `CarryForward` in tests/test_research_continuity_parts2_boundary.py.
    assert set(verdict.failures) == {1}, verdict.failures
    assert verdict.failures[1] == ["NumberSite: no __init__"], verdict.failures


def test_moved_names_still_resolve_on_number_sources():
    import src.number_sources as owner

    assert owner.NumberSite is NumberSite
    assert owner.NEUTRAL_VALUES is NEUTRAL_VALUES
    assert owner.FACTOR_BAND is FACTOR_BAND
    assert owner.CONFIG_CLASS_SUFFIX is CONFIG_CLASS_SUFFIX
    assert owner._scan_extended_shapes is _scan_extended_shapes
    assert owner._numeric is _numeric
    assert owner._leaves is _leaves
    assert owner._field_default is _field_default
    assert owner._module_constants is _module_constants
    assert owner._imported_constants is _imported_constants
    assert owner._qualified_scopes is _qualified_scopes
    assert owner._factor_operands is _factor_operands
    assert owner._own_nodes is _own_nodes


def _tree(src: str) -> ast.Module:
    return ast.parse(textwrap.dedent(src))


def test_numeric_reads_literals_negations_and_bound_names_without_a_module():
    names = {"HALF": 0.5}
    assert _numeric(_tree("1.25").body[0].value) == 1.25
    assert _numeric(_tree("-3").body[0].value) == -3
    assert _numeric(_tree("HALF").body[0].value, names) == 0.5
    assert _numeric(_tree("'text'").body[0].value) is None


def test_module_constants_are_read_off_a_tree_built_in_the_test():
    tree = _tree(
        """
        A = 2.5
        B = -A
        C = "no"
        """
    )
    consts = _module_constants(tree)
    assert consts["A"] == 2.5
    assert consts["B"] == -2.5
    assert "C" not in consts


def test_extended_shapes_find_a_factor_band_multiplier_with_no_owner_object():
    tree = _tree(
        """
        def price(limit):
            return limit * 1.25
        """
    )
    sites = _scan_extended_shapes(tree, "mod", "src/mod.py", {}, set())
    assert any(isinstance(s, NumberSite) and s.value == 1.25 for s in sites), sites
    assert all(s.path == "src/mod.py" for s in sites)


def test_extended_shapes_leave_a_neutral_value_alone():
    tree = _tree(
        """
        def zero(x):
            return x * 0.0
        """
    )
    sites = _scan_extended_shapes(tree, "mod", "src/mod.py", {}, set())
    assert not any(s.value in NEUTRAL_VALUES for s in sites)
