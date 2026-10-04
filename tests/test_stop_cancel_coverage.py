"""The parked defect: a pre-SELL stop cancel whose ROLLBACK also fails
ends with fewer shares protected than it started with, and says so only
with a bare False.

These tests pin the mechanism and the contract that removes it.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.execution.broker import AlpacaBroker
from src.stop_cancel_outcome import StopCancelOutcome


SPEC_A = {"id": "stop-a", "qty": 51.0, "stop_price": 248.5, "limit_price": 240.0}
SPEC_B = {"id": "stop-b", "qty": 20.0, "stop_price": 246.0, "limit_price": 238.0}


def _broker(mock_tc_cls, failing_id="stop-b"):
    client = MagicMock()

    def _cancel(oid):
        if oid == failing_id:
            raise RuntimeError("alpaca 500")

    client.cancel_order_by_id.side_effect = _cancel
    mock_tc_cls.return_value = client
    return AlpacaBroker(api_key="test", secret_key="test", paper=True), client


@patch("src.execution.broker.TradingClient")
def test_failed_rollback_reports_which_shares_are_naked(mock_tc_cls):
    """THE DEFECT. stop-a cancels, stop-b's cancel raises, and the rollback
    of stop-a is itself rejected. 51 shares that were covered on entry are
    naked on exit. The old code returned a bare False here."""
    broker, _client = _broker(mock_tc_cls)
    # Rollback restores nothing and hands back the spec it could not place.
    broker._restore_stop_orders = MagicMock(return_value=(0, [SPEC_A]))

    outcome = broker.cancel_snapshotted_stops("AMZN", [SPEC_A, SPEC_B])

    assert isinstance(outcome, StopCancelOutcome)
    assert outcome.cleared is False
    assert outcome.coverage_shrank is True
    assert [s["id"] for s in outcome.unprotected] == ["stop-a"]
    assert outcome.unprotected_qty == 51.0
    # stop-b's cancel failed, so that stop is still alive and still covering.
    assert [s["id"] for s in outcome.still_resting] == ["stop-b"]


@patch("src.execution.broker.TradingClient")
def test_successful_rollback_is_not_coverage_loss(mock_tc_cls):
    broker, _client = _broker(mock_tc_cls)
    broker._restore_stop_orders = MagicMock(return_value=(1, []))

    outcome = broker.cancel_snapshotted_stops("AMZN", [SPEC_A, SPEC_B])

    assert outcome.cleared is False
    assert outcome.coverage_shrank is False
    assert outcome.unprotected == ()
    assert {s["id"] for s in outcome.still_resting} == {"stop-a", "stop-b"}


@patch("src.execution.broker.TradingClient")
def test_clean_cancel_reports_cleared(mock_tc_cls):
    broker, client = _broker(mock_tc_cls, failing_id=None)
    outcome = broker.cancel_snapshotted_stops("AMZN", [SPEC_A, SPEC_B])
    assert outcome.cleared is True
    assert outcome.coverage_shrank is False
    assert client.cancel_order_by_id.call_count == 2


@patch("src.execution.broker.TradingClient")
def test_no_specs_is_cleared(mock_tc_cls):
    broker, _client = _broker(mock_tc_cls)
    assert broker.cancel_snapshotted_stops("AMZN", []).cleared is True


def test_outcome_refuses_to_be_a_boolean():
    """The forcing function: `if not broker.cancel_snapshotted_stops(...)`
    cannot compile away a coverage loss ever again."""
    out = StopCancelOutcome(
        symbol="AMZN", requested=(SPEC_A,), unprotected=(SPEC_A,),
    )
    with pytest.raises(TypeError, match="not a boolean"):
        bool(out)
    with pytest.raises(TypeError):
        if not out:  # noqa: SIM103 - the point of the test
            pass


# --------------------------------------------------------------------------
# The amplifier: the WAL recovery row was DELETED on that bare False.
# --------------------------------------------------------------------------


from src.pipeline_protection import ProtectionMixin  # noqa: E402


class _FakePipe(ProtectionMixin):
    """Minimal stand-in for the pipeline's _cancel_stops_with_write_ahead."""

    def __init__(self, outcome):
        self.broker = MagicMock()
        self.broker.snapshot_protective_stops.return_value = (True, [SPEC_A, SPEC_B])
        self.broker.cancel_snapshotted_stops.return_value = outcome
        self.db = MagicMock()
        self._last_stop_clear_refusal = ""

    def _write_ahead_protection_restore(self, *a, **k):
        return 77


def _run_cancel(outcome):
    pipe = _FakePipe(outcome)
    return ProtectionMixin._cancel_stops_with_write_ahead(pipe, "AMZN", 71.0), pipe


def test_wal_row_is_kept_whole_when_coverage_shrank():
    outcome = StopCancelOutcome(
        symbol="AMZN", requested=(SPEC_A, SPEC_B), cancelled=(SPEC_A,),
        still_resting=(SPEC_B,), unprotected=(SPEC_A,),
    )
    (ok, specs, row), pipe = _run_cancel(outcome)

    assert ok is False
    # The hole must survive the session: the row is NOT discharged. It is
    # left whole — the drain's idempotency check skips the stops still alive.
    pipe.db.delete_pending_protection_restore.assert_not_called()
    assert pipe._last_stop_clear_refusal == "coverage_shrank"


def test_wal_row_is_discharged_when_rollback_fully_succeeded():
    outcome = StopCancelOutcome(
        symbol="AMZN", requested=(SPEC_A, SPEC_B),
        still_resting=(SPEC_A, SPEC_B),
    )
    (ok, specs, row), pipe = _run_cancel(outcome)

    assert ok is False
    pipe.db.delete_pending_protection_restore.assert_called_once_with(77)
    pipe.db.update_pending_protection_restore_specs.assert_not_called()
    assert pipe._last_stop_clear_refusal == "cancel_rolled_back"


def test_scale_in_keeps_the_wal_when_coverage_shrank():
    """The same amplifier on the scale-in path: the add is abandoned, but
    the recovery row must survive so the naked shares get re-protected."""
    from src.stop_cancel_outcome import handle_add_cancel

    db, prep = MagicMock(), MagicMock(wal_row_id=42)
    cancel = StopCancelOutcome(
        symbol="COP", requested=(SPEC_A, SPEC_B), cancelled=(SPEC_A,),
        still_resting=(SPEC_B,), unprotected=(SPEC_A,),
    )
    assert handle_add_cancel(db, prep, cancel, MagicMock(), MagicMock()) is False
    db.delete_pending_protection_restore.assert_not_called()
    assert prep.skip_reason == "scale_in_coverage_shrank"
    assert prep.wal_row_id == 42


def test_scale_in_discharges_the_wal_when_nothing_was_lost():
    from src.execution import scale_in as si

    db = MagicMock()
    si.discharge_scale_in_wal(db, 42)
    db.delete_pending_protection_restore.assert_called_once_with(42)


def test_no_production_caller_treats_the_outcome_as_a_truth_value():
    """The mechanical guard on the whole class: every production call of
    cancel_snapshotted_stops must bind the outcome and ask it a question.
    `if not broker.cancel_snapshotted_stops(...)` is the shape that hid a
    live coverage loss, and it must never come back."""
    import ast
    import pathlib as _p

    offenders = []
    src_root = _p.Path(__file__).resolve().parent.parent / "src"
    paths = sorted(src_root.rglob("*.py"))
    assert len(paths) > 50, f"source tree not found at {src_root}"
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.While, ast.UnaryOp, ast.BoolOp)):
                continue
            tests = (
                [node.test] if isinstance(node, (ast.If, ast.While))
                else [node.operand] if isinstance(node, ast.UnaryOp)
                else list(node.values)
            )
            for t_ in tests:
                for sub in ast.walk(t_):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "cancel_snapshotted_stops"):
                        offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        "a protection outcome is being used as a truth value at: "
        + ", ".join(sorted(set(offenders)))
    )


def test_every_spec_lands_in_exactly_one_bucket():
    """No share can be silently dropped: requested == cancelled +
    still_resting + unprotected, always."""
    out = StopCancelOutcome(
        symbol="X", requested=(SPEC_A, SPEC_B), cancelled=(),
        still_resting=(SPEC_B,), unprotected=(SPEC_A,),
    )
    buckets = len(out.cancelled) + len(out.still_resting) + len(out.unprotected)
    assert buckets == len(out.requested)
    assert out.unprotected_qty == 51.0
    assert out.covered_qty == 20.0
