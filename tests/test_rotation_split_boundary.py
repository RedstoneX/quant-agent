"""Boundary witness: the lifted rotation pieces import and run with no pipeline behind them."""

from __future__ import annotations

import pytest

import src.rotation as rotation
from src.rotation_parts import constraints
from tests.boundary_harness import check_boundary

MODULES = (
    "src.rotation_parts.constraints",
    "src.rotation_parts.wording",
    "src.rotation_parts.reporting",
    "src.rotation_parts.reporting_lines",
)


@pytest.mark.parametrize("module", MODULES)
def test_rotation_part_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_rotation_types_pass_the_boundary_check_apart_from_generated_init():
    # Frozen dataclasses get their __init__ generated, which clause 1 cannot
    # see; every other clause must hold with no exemption.
    verdict = check_boundary("src.rotation_parts.types")
    names = {"RotationOpportunity", "RotationRefusal", "RotationOutcome", "RotationPrecheck", "RotationClearance"}
    assert {f.split(":")[0] for f in verdict.failures.get(1, [])} <= names
    assert all(f.endswith("no __init__") for f in verdict.failures.get(1, []))
    assert {k for k in verdict.failures if k != 1} == set()


def test_moved_names_still_resolve_on_the_rotation_module():
    from src.rotation_parts import reporting, reporting_lines, types, wording

    assert rotation.RotationOpportunity is types.RotationOpportunity
    assert rotation.rotation_sell_reason is wording.rotation_sell_reason
    assert rotation.precheck_record is reporting.precheck_record
    assert rotation.owner_precheck_lines is reporting_lines.owner_precheck_lines
    assert rotation._tier_two_line is reporting_lines._tier_two_line
    assert rotation.rotation_binding_constraints is constraints.rotation_binding_constraints
