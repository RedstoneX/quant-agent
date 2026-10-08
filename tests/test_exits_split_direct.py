"""The lifted exit bodies exercised directly with plain stand-in objects."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.exits_parts import risk_review, trails


def test_trails_with_no_positions_places_no_orders():
    owner = SimpleNamespace(db=MagicMock(), broker=MagicMock())
    assert trails._apply_deterministic_trails(owner, [], run_id="r1") == []
    owner.broker.submit_order.assert_not_called()


def test_risk_review_without_exits_returns_no_veto_and_no_verdict():
    review = SimpleNamespace(actions=[SimpleNamespace(action="HOLD", symbol="AAA", reason="")])
    owner = SimpleNamespace(db=MagicMock())
    assert risk_review._risk_review_exits(
        owner, review, [], run_id="r1", total_value=1000.0,
    ) == (set(), None)
    assert risk_review._risk_review_exits(
        owner, None, [], run_id="r1", total_value=1000.0,
    ) == (set(), None)
