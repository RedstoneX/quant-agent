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
from src.cost_circuit.parts.emergency_latch import EmergencyLatch
from src.cost_circuit.parts.infra_retry import InfraRetry
from src.cost_circuit.parts.operator_controls import OperatorControls
from src.cost_circuit.parts.session_lifecycle import SessionLifecycle
from src.cost_circuit.parts.settlement import Settlement
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


LIFTED = [AlertFormats, EpisodeWording, OwnerNotify, CircuitState, QuotaHolds, Admission,
          Settlement, EmergencyLatch, InfraRetry, OperatorControls, SessionLifecycle]


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
    "src.cost_circuit.parts.settlement",
    "src.cost_circuit.parts.emergency_latch",
    "src.cost_circuit.parts.infra_retry",
    "src.cost_circuit.parts.operator_controls",
    "src.cost_circuit.parts.session_lifecycle",
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


def test_latch_sync_writes_the_sentinel_through_to_the_host_under_its_own_lock():
    """`_sync_emergency_latch` ASSIGNS `_infrastructure_error` / `_unavailable_sentinel`
    inside its own `with self._infrastructure_lock:`; the part's setter writes
    each through to the host, so the lock stays inside the body (same side as
    on the mixin) and the host sees the installed sentinel immediately."""
    host = {"sentinel": None, "error": None}
    lock = threading.Lock()
    seen_locked = []

    def write_sentinel(value):
        seen_locked.append(lock.locked())
        host["sentinel"] = value

    latch = _build(
        EmergencyLatch, infrastructure_lock=lock,
        read_unavailable_sentinel=lambda: host["sentinel"],
        write_unavailable_sentinel=write_sentinel,
        read_infrastructure_error=lambda: host["error"],
        write_infrastructure_error=lambda v: host.__setitem__("error", v),
        read_emergency_latch=lambda: (RuntimeError("latched"), {"attempts": "3"}),
        emergency_file_lock=None, emergency_latch_path=None, emergency_lock_path=None,
    )
    latch._sync_emergency_latch()
    assert seen_locked == [True]
    assert host["sentinel"] is not None and str(host["error"]) == "latched"
    assert host["sentinel"].attempts == 3
    latch._sync_emergency_latch()  # already installed: not replaced
    assert seen_locked == [True]
    assert EmergencyLatch._safe_optional_int(True) is None


def test_retry_mark_unavailable_writes_both_fields_through_and_returns_the_sentinel_answer():
    host = {"sentinel": None, "error": None}
    retry = _build(
        InfraRetry, infrastructure_lock=threading.Lock(),
        context=lambda: ("run", "mode"),
        best_effort_emergency_snapshot=lambda run_id: {},
        write_emergency_latch=MagicMock(name="write_latch"),
        read_unavailable_sentinel=lambda: host["sentinel"],
        write_unavailable_sentinel=lambda v: host.__setitem__("sentinel", v),
        read_infrastructure_error=lambda: host["error"],
        write_infrastructure_error=lambda v: host.__setitem__("error", v),
        emergency_latch_path=None, emergency_lock_path=None,
        infra_retry_backoff_s=None, mark_unavailable=None,
    )
    assert retry._run_with_infra_retry.__func__ is InfraRetry._run_with_infra_retry
    answer = retry.mark_unavailable(OSError("disk"), agent_name="pricing_preflight", attempts=0)
    assert host["sentinel"] is not None and isinstance(host["error"], OSError)
    assert answer["suspended"] is True
    with pytest.raises(Exception):
        retry._raise_if_unavailable("x")


def test_session_uses_its_own_seed_unless_swapped_and_operator_reset_clears_host_state():
    sess = _build(SessionLifecycle, seed_today=None, validate_accounting_invariants=None)
    assert sess._seed_today.__func__ is SessionLifecycle._seed_today
    swapped = MagicMock(name="swapped")
    assert _build(SessionLifecycle, seed_today=swapped)._seed_today is swapped
    ctx = MagicMock()
    sess = _build(SessionLifecycle, session_context=ctx)
    sess.set_session_context("r", "m")
    ctx.set.assert_called_once_with(("r", "m"))
    op = _build(OperatorControls, enabled=False)
    assert op.status() == {"enabled": False, "suspended": False}
    settle = _build(Settlement, enabled=False)
    assert settle.complete_call(MagicMock(reservation_id="x"), 1.0) is None


def test_every_remaining_shim_builds_its_part_per_call_and_sees_a_swapped_body():
    from src.cost_circuit.breaker_latch import _BreakerLatchMixin
    from src.cost_circuit.breaker_retry import _BreakerRetryMixin
    from src.cost_circuit.breaker_session import _BreakerSessionMixin

    class Host(_BreakerLatchMixin, _BreakerRetryMixin, _BreakerSessionMixin):
        config = object()
        enabled = True
        notifier = None
        _connect = None
        _session_context = None
        _infrastructure_lock = threading.Lock()
        _emergency_latch_path = None
        _emergency_lock_path = None
        _unavailable_sentinel = None
        _infrastructure_error = None
        _context = staticmethod(lambda: ("r", "m"))
        _reconcile_quota_holds_locked = _notify_if_needed = enforce_current_limits = status = None

    host = Host()
    assert host._emergency_latch()._read_emergency_latch.__func__ is EmergencyLatch._read_emergency_latch
    assert host._infra_retry().mark_unavailable.__func__ is InfraRetry.mark_unavailable
    assert host._session_lifecycle()._seed_today.__func__ is SessionLifecycle._seed_today
    host._seed_today = MagicMock(name="swapped_seed")
    assert host._session_lifecycle()._seed_today is host._seed_today
    assert host._infra_retry()._unavailable_sentinel is None
    host._unavailable_sentinel = "installed-mid-call"
    assert host._infra_retry()._unavailable_sentinel == "installed-mid-call"
