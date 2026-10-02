"""Boundary witness: the lifted stop-amend piece builds and runs with no broker or pipeline behind it.

Every collaborator is an explicit keyword-only constructor argument, so the
class is built from stubs alone (clause 5 of tests/boundary_harness.py).
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.execution.broker_parts.stop_amend import (
    StopAmender, _AMEND_NOT_ATTEMPTED, _is_terminal_broker_rejection, _quantize_price,
)
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


def test_stop_amender_is_constructible_from_stubs():
    _build(StopAmender)
    params = inspect.signature(StopAmender).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_stop_amend_module_passes_the_boundary_check():
    verdict = check_boundary("src.execution.broker_parts.stop_amend")
    assert verdict.passed, verdict.failures


def test_moved_helpers_still_resolve_on_the_broker_module():
    import src.execution.broker as broker_module
    assert broker_module._AMEND_NOT_ATTEMPTED is _AMEND_NOT_ATTEMPTED
    assert broker_module._is_terminal_broker_rejection is _is_terminal_broker_rejection
    assert broker_module._quantize_price is _quantize_price
    assert broker_module.AlpacaBroker._AMEND_DEAD_STATES is StopAmender._AMEND_DEAD_STATES
    assert broker_module.AlpacaBroker._failed_amend_payload is StopAmender._failed_amend_payload
    assert broker_module.AlpacaBroker._stop_order_amendable_in_place is StopAmender._stop_order_amendable_in_place


def test_amend_refusal_without_a_broker_object():
    """A stand-alone amender: the broker's replace call raising a terminal
    rejection leaves the original stop resting and reports the refusal."""
    client = MagicMock(name="client")
    client.replace_order_by_id.side_effect = RuntimeError("rejected")
    amender = StopAmender(client=client, list_open_stop_orders_by_side=MagicMock(return_value=[]),
                          snapshot_stop_order=MagicMock(return_value=None))
    out = amender._amend_one_stop_price(symbol="ZZZ", spec={"id": "s1", "qty": 1, "stop_price": 9.0, "limit_price": None}, new_price=9.5)
    assert isinstance(out, dict)
    client.replace_order_by_id.assert_called_once()
