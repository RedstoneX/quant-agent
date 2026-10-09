"""The hard-ATR-floor recorder counts in ROWS; it must never decide.

Why this exists: the production DB (read-only, 2026-10-04) holds 9
`stop_honoured_at_computed_level` placements and ZERO
`stop_widened_to_absolute_atr_floor` ones, so the floor under every stop has
never once bound and its multiple of 1 cannot be re-derived from anything.
These tests pin the counting, not a number. The module stores nothing, so
there is no reset and no test can inherit another test's tallies.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib

import pytest

from src.portfolio_constructor import absolute_floor_record as rec
from src.portfolio_constructor.config import (
    STOP_RULE_ABSOLUTE_FLOOR,
    STOP_RULE_LEVEL_HONOURED,
)


def _call(inside, stop=95.0, atr=10.0):
    return rec.noted(
        inside_hard_floor=inside,
        symbol="AAA",
        side_label="LONG",
        side_word="below",
        entry_price=100.0,
        stop_loss=stop,
        atr=atr,
        level=stop,
        hard_floor=90.0,
        floor_multiple=1.0,
        multiple=2.5,
        band_edge=75.0,
    )


def test_the_module_holds_no_state_at_all():
    """The stored-bookkeeping mandate: no tally, no reset, nothing to clear."""
    assert not hasattr(rec, "reset")
    held = [n for n, v in vars(rec).items() if isinstance(v, (list, dict, set)) and not n.startswith("__")]
    assert held == [], held


def test_a_clear_stop_is_returned_untouched():
    assert _call(False, stop=88.0) == (88.0, STOP_RULE_LEVEL_HONOURED)


def test_a_bound_stop_is_returned_at_the_floor():
    assert _call(True, stop=95.0) == (90.0, STOP_RULE_ABSOLUTE_FLOOR)


def test_the_module_never_recomputes_the_verdict():
    """A stop nowhere near the floor still binds if the caller says it does."""
    assert _call(True, stop=10.0) == (90.0, STOP_RULE_ABSOLUTE_FLOOR)


def test_one_row_is_written_per_placement(caplog):
    with caplog.at_level("INFO"):
        _call(False, stop=88.0)
        _call(True, stop=95.0)
    rows = rec.rows_from(caplog.messages)
    assert len(rows) == 2
    assert [r["floor_bound"] for r in rows] == [False, True]
    assert [r["rule"] for r in rows] == [
        STOP_RULE_LEVEL_HONOURED,
        STOP_RULE_ABSOLUTE_FLOOR,
    ]


def test_the_spread_is_derived_from_the_rows_not_from_a_tally(caplog):
    with caplog.at_level("INFO"):
        _call(False, stop=88.0)  # 1.2 ATRs
        _call(False, stop=70.0)  # 3.0 ATRs
        _call(True, stop=95.0)  # 0.5 ATRs
    summary = rec.summarise(rec.rows_from(caplog.messages))
    assert summary["level_backed_total"] == 3
    assert summary["floor_binds"] == 1
    assert summary["floor_clears"] == 2
    assert summary["distance_readings"] == 3
    assert summary["tightest_atr_seen"] == pytest.approx(0.5)
    assert summary["widest_atr_seen"] == pytest.approx(3.0)
    assert summary["mean_atr_seen"] == pytest.approx((1.2 + 3.0 + 0.5) / 3)


def test_no_rows_reports_none_not_a_stand_in_number():
    summary = rec.summarise([])
    assert summary["tightest_atr_seen"] is None
    assert summary["mean_atr_seen"] is None
    assert summary["floor_binds"] == 0


def test_unrelated_log_lines_and_junk_rows_are_ignored():
    rows = rec.rows_from(["nothing here", rec.ROW_TAG + "{not json", "x"])
    assert rows == []


@pytest.mark.parametrize("atr", [0.0, -1.0, float("nan"), float("inf"), None, "x"])
def test_an_unreadable_atr_yields_no_reading_rather_than_zero(atr):
    assert rec.distance_in_atrs(100.0, 95.0, atr) is None


def test_a_row_with_no_distance_still_counts_as_a_placement():
    rows = [{"symbol": "AAA", "rule": "r", "floor_bound": False, "distance_atr": None}]
    summary = rec.summarise(rows)
    assert summary["level_backed_total"] == 1
    assert summary["distance_readings"] == 0
    assert summary["tightest_atr_seen"] is None


def test_the_resolver_routes_its_level_backed_branch_through_the_recorder():
    import inspect

    from src.portfolio_constructor.entry_stop import resolver

    assert "absolute_floor_record.noted(" in inspect.getsource(resolver)


def _logger_info_calls(src):
    found = {}
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "info"
            and getattr(node.func.value, "id", "") == "logger"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            found[ast.literal_eval(node.args[0])] = node
    return found


FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "resolver_log_bodies_pre_move.py"
# The pre-move text is a FACT OF HISTORY, so its digest is pinned here: editing
# the fixture to make a drifted body pass fails this line first.
FIXTURE_SHA256 = "c3b1ff36a73da7596bb7df01d2eb2c133af6f709b853f5fa2354a0151e8c3e48"


def test_the_pinned_pre_move_fixture_has_not_been_edited():
    """Two files would have to be forged, and both are guard files."""
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_both_moved_log_bodies_are_ast_identical_to_the_pre_move_original():
    """Faithful is proven, not asserted: 2 of 2 bodies must match the original.

    The reference is commit 8858100a's resolver, frozen into the fixture --
    NOT ``origin/main``. Comparing against trunk could only ever be green
    while the move was unmerged: once merged, trunk IS the moved version and
    the originals are gone from it, so that form red on a clean main and
    blocked every open change. A commit that is already in history cannot
    change, so this form cannot go red on a clean trunk tomorrow.
    """
    before = _logger_info_calls(FIXTURE.read_text(encoding="utf-8"))
    after = _logger_info_calls(open(rec.__file__, encoding="utf-8").read())
    moved = [k for k in after if k.startswith("Constructor:")]
    assert len(moved) == 2, moved
    assert sorted(before) == sorted(moved), (sorted(before), sorted(moved))
    for key in moved:
        assert ast.dump(before[key]) == ast.dump(after[key]), key
