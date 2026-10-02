"""Boundary witnesses: the lifted cost-circuit owner-alert pieces build and run with no
breaker (and no pipeline) behind them.

Every collaborator is an explicit keyword-only constructor argument, so each
class is built from stubs alone (clause 5 of tests/boundary_harness.py).
"""
from __future__ import annotations

import functools
import inspect
import threading
from unittest.mock import MagicMock

import pytest

from src.cost_circuit.parts.admission import Admission
from src.cost_circuit.parts.alert_formats import AlertFormats
from src.cost_circuit.parts.circuit_state import CircuitState
from src.cost_circuit.parts.episode_wording import EpisodeWording
from src.cost_circuit.parts.owner_notify import OwnerNotify
from src.cost_circuit.parts.quota_holds import QuotaHolds
from src.cost_circuit.parts.shim_guard import _is_class_shim
from src.cost_circuit.breaker_state import _BreakerStateMixin
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


LIFTED = [AlertFormats, EpisodeWording, OwnerNotify, CircuitState, QuotaHolds, Admission]


@pytest.mark.parametrize("cls", LIFTED)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", [
    "src.cost_circuit.parts.alert_formats",
    "src.cost_circuit.parts.episode_wording",
    "src.cost_circuit.parts.owner_notify",
    "src.cost_circuit.parts.circuit_state",
    "src.cost_circuit.parts.quota_holds",
    "src.cost_circuit.parts.admission",
])
def test_every_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_formats_run_with_nothing_behind_them():
    text = AlertFormats().format_quota_alert({"scope": "day", "trigger_code": "t", "day": "d"})
    assert text.startswith("🟠 QAMC PAID ANALYSIS QUOTA HOLD")
    assert AlertFormats().format_recovery_alert({"scope": "day"}).startswith("🟢")


def test_wording_reads_the_window_off_config_with_the_same_default():
    assert EpisodeWording(config=object())._self_clear_window_minutes() == 15.0
    cfg = MagicMock(transient_latch_cooldown_minutes=7)
    assert EpisodeWording(config=cfg)._self_clear_window_minutes() == 7.0


def test_owner_notify_disabled_breaker_sends_nothing():
    notifier = MagicMock()
    on = _build(OwnerNotify, enabled=False, notifier=notifier)
    on._notify_if_needed()
    notifier.send.assert_not_called()


def test_owner_notify_uses_its_own_scans_unless_swapped():
    on = _build(OwnerNotify, notify_quota_holds_if_needed=None)
    assert on._notify_quota_holds_if_needed.__func__ is OwnerNotify._notify_quota_holds_if_needed
    swapped = MagicMock(name="swapped")
    assert _build(OwnerNotify, notify_quota_holds_if_needed=swapped)._notify_quota_holds_if_needed is swapped


def test_shim_guard_sees_through_bound_methods_and_partials():
    from src.cost_circuit.breaker_notify import _BreakerNotifyMixin

    class Host(_BreakerNotifyMixin):
        pass

    host = Host()
    attr = "_notify_quota_holds_if_needed"
    assert _is_class_shim(getattr(host, attr), attr, _BreakerNotifyMixin)
    assert _is_class_shim(functools.partial(getattr(_BreakerNotifyMixin, attr), host), attr, _BreakerNotifyMixin)
    assert not _is_class_shim(MagicMock(), attr, _BreakerNotifyMixin)
    assert not _is_class_shim(None, attr, _BreakerNotifyMixin)


def test_state_statics_run_with_nothing_behind_them():
    assert CircuitState._scope_key("day", day="d", run_id="r", mode="m") == \
        _BreakerStateMixin._scope_key("day", day="d", run_id="r", mode="m")
    assert _BreakerStateMixin._totals is not None


def test_holds_uses_its_own_trip_unless_swapped():
    holds = _build(QuotaHolds, trip_locked=None, hold_quota_locked=None)
    assert holds._trip_locked.__func__ is QuotaHolds._trip_locked
    swapped = MagicMock(name="swapped")
    assert _build(QuotaHolds, trip_locked=swapped)._trip_locked is swapped


def test_admission_reads_the_sentinel_live_not_snapshotted():
    """`enforce_current_limits` reads the sentinel AFTER `_sync_emergency_latch` /
    `_run_with_infra_retry` can install it on the host, so it must not be a
    construction-time copy."""
    host = {"sentinel": None}
    sentinel = MagicMock(name="sentinel")
    sentinel.enforce_current_limits.return_value = {"suspended": True}

    def sync():
        host["sentinel"] = sentinel

    adm = _build(
        Admission, enabled=True, sync_emergency_latch=sync,
        read_unavailable_sentinel=lambda: host["sentinel"],
        infrastructure_lock=threading.Lock(),
        enforce_settled_limits_locked=None, enforce_current_limits=None,
    )
    assert adm.enforce_current_limits("preflight") == {"suspended": True}
    sentinel.enforce_current_limits.assert_called_once_with("preflight")
    adm._run_with_infra_retry.assert_not_called()


def test_shims_build_the_state_part_per_call_and_see_a_swapped_body():
    from src.cost_circuit.breaker_holds import _BreakerHoldsMixin

    class Host(_BreakerStateMixin, _BreakerHoldsMixin):
        config = object()
        _context = staticmethod(lambda: ("r", "m"))
        _emergency_latch_path = None

    host = Host()
    assert host._circuit_state()._state_row.__func__ is CircuitState._state_row
    assert host._quota_holds()._trip_locked.__func__ is QuotaHolds._trip_locked
    host._trip_locked = MagicMock(name="swapped_trip")
    assert host._quota_holds()._trip_locked is host._trip_locked
    assert not _is_class_shim(host._trip_locked, "_trip_locked", _BreakerHoldsMixin)
