"""Boundary witness: the pieces lifted out of pipeline_rotation_exec stand alone."""
from __future__ import annotations

import pytest

import src.pipeline_rotation_exec as old
from src import rotation_buy_leg_post, rotation_projection
from tests.boundary_harness import check_boundary

MODULES = ("src.rotation_projection", "src.rotation_buy_leg_post")


@pytest.mark.parametrize("module", MODULES)
def test_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_moved_names_still_resolve_on_the_old_module():
    for name in ("_projected_sale_qty", "_projected_post_sale_book", "_scaled_position",
                 "_projected_post_sale_cash", "_projected_entry_cost"):
        assert getattr(old, name) is getattr(rotation_projection, name)
    for name in ("_alert_rotation_executed", "_drop_buys_sold_today_below_bar",
                 "_drop_rotation_buy_if_room_not_freed", "_record_rotation_buy_leg_outcome",
                 "_alert_rotation_buy_leg_missing"):
        assert getattr(old, name) is getattr(rotation_buy_leg_post, name)
