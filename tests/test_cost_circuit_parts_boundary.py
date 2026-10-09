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
from src.cost_circuit.breaker import LLMCostCircuitBreaker
from src.cost_circuit.assembly import hold_parts
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


LIFTED = [
    AlertFormats,
    EpisodeWording,
    OwnerNotify,
    CircuitState,
    QuotaHolds,
    Admission,
    Settlement,
    EmergencyLatch,
    InfraRetry,
    OperatorControls,
    SessionLifecycle,
]


@pytest.mark.parametrize("cls", LIFTED)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize(
    "module",
    [
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
        "src.cost_circuit.assembly",
    ],
)
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
    """The guard is still used by other seams (the PM seat); it no longer has a
    cost-circuit shim to test against, so a local host stands in."""

    class Host:
        def _notify_quota_holds_if_needed(self):
            return None

    host = Host()
    attr = "_notify_quota_holds_if_needed"
    assert _is_class_shim(getattr(host, attr), attr, Host)
    assert _is_class_shim(functools.partial(getattr(Host, attr), host), attr, Host)
    assert not _is_class_shim(MagicMock(), attr, Host)
    assert not _is_class_shim(None, attr, Host)


def test_state_statics_run_with_nothing_behind_them():
    assert CircuitState._scope_key("day", day="d", run_id="r", mode="m") == LLMCostCircuitBreaker._scope_key(
        "day", day="d", run_id="r", mode="m"
    )
    assert LLMCostCircuitBreaker._totals is CircuitState._totals


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
        Admission,
        enabled=True,
        sync_emergency_latch=sync,
        read_unavailable_sentinel=lambda: host["sentinel"],
        infrastructure_lock=threading.Lock(),
        enforce_settled_limits_locked=None,
        enforce_current_limits=None,
    )
    assert adm.enforce_current_limits("preflight") == {"suspended": True}
    sentinel.enforce_current_limits.assert_called_once_with("preflight")
    adm._run_with_infra_retry.assert_not_called()


def _real_breaker():
    from types import SimpleNamespace

    class _Notifier:
        enabled = True

        def send(self, _message, **_kwargs):
            return True

    cfg = SimpleNamespace(
        enabled=True,
        session_cost_limit_usd=10.0,
        daily_cost_limit_usd=20.0,
        max_calls_per_session=1000,
        max_provider_attempts_per_call=2,
        input_chars_per_token=3.5,
    )
    return LLMCostCircuitBreaker(":memory:", cfg, _Notifier())


HELD = {
    "_alert_formats": AlertFormats,
    "_episode_wording": EpisodeWording,
    "_circuit_state": CircuitState,
    "_quota_holds": QuotaHolds,
    "_emergency_latch": EmergencyLatch,
    "_infra_retry": InfraRetry,
    "_session_lifecycle": SessionLifecycle,
    "_owner_notify": OwnerNotify,
    "_admission": Admission,
    "_settlement": Settlement,
    "_operator_controls": OperatorControls,
}
GONE_SHIMS = (
    "formats",
    "wording",
    "state",
    "holds",
    "latch",
    "retry",
    "session",
    "notify",
    "admission",
    "settlement",
    "operator",
)


def test_breaker_holds_every_part_instead_of_inheriting_any():
    """Composition, not inheritance: the breaker owns one instance of each of
    the eleven parts and every same-named method delegates to it. No shim
    mixin remains in the MRO or on disk."""
    import importlib
    import src.cost_circuit.breaker as breaker_mod

    for gone in GONE_SHIMS:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"src.cost_circuit.breaker_{gone}")
    assert LLMCostCircuitBreaker.__mro__ == (LLMCostCircuitBreaker, object)
    assert all(cls.__name__ in breaker_mod.__dict__ for cls in HELD.values())

    breaker = _real_breaker()
    assert breaker._unavailable_sentinel is None
    for attr, cls in HELD.items():
        assert isinstance(getattr(breaker, attr), cls), attr
    # The held QuotaHolds reads state through the held CircuitState, not the breaker.
    assert breaker._quota_holds._state_row.__self__ is breaker._circuit_state
    assert breaker._quota_holds._trip_locked.__func__ is QuotaHolds._trip_locked
    assert breaker._circuit_state._context.__self__ is breaker
    # A part keeps its own lifted bodies; it is never handed the breaker's delegate for them.
    assert breaker._session_lifecycle._seed_today.__func__ is SessionLifecycle._seed_today
    assert breaker._infra_retry.mark_unavailable.__func__ is InfraRetry.mark_unavailable
    assert breaker._owner_notify._notify_quota_holds_if_needed.__func__ is OwnerNotify._notify_quota_holds_if_needed
    assert breaker._admission.enforce_current_limits.__func__ is Admission.enforce_current_limits
    # Delegation reaches the instance: swapping a body on the held part is what runs.
    breaker._quota_holds._trip_locked = MagicMock(name="swapped_trip", return_value="tripped")
    assert breaker._trip_locked(None, scope="day") == "tripped"
    breaker._episode_wording._self_clear_window_minutes = lambda: 42.0
    assert breaker._self_clear_window_minutes() == 42.0
    breaker._operator_controls.status = lambda: {"swapped": True}
    assert breaker.status() == {"swapped": True}


def test_assembly_wires_every_part_onto_a_stub_with_no_breaker_behind_it():
    """The wiring is its own piece: `hold_parts` builds all eleven parts onto
    any object carrying the breaker's slots -- here a bare stub, no breaker,
    no pipeline -- and the breaker's `_hold_parts` is a two-line call to it."""
    import inspect

    stub = MagicMock(name="breaker_stub")
    stub.config = object()
    hold_parts(stub)
    for attr, cls in HELD.items():
        assert isinstance(getattr(stub, attr), cls), attr
    assert stub._quota_holds._state_row.__self__ is stub._circuit_state
    assert stub._emergency_latch.notifier is stub.notifier
    # The breaker only delegates: no construction logic is left in the holder.
    body = inspect.getsource(LLMCostCircuitBreaker._hold_parts).strip().splitlines()
    assert body == ["def _hold_parts(self) -> None:", "        hold_parts(self)"], body
    for part, names in LLMCostCircuitBreaker._DELEGATES.items():
        for name in names:
            assert part in HELD and hasattr(HELD[part], name), (part, name)


def test_held_parts_see_collaborators_that_change_after_construction():
    """What the per-call shims gave for free, the held parts must still get:
    a notifier reassigned on the breaker, a `_connect` swapped on the
    instance, and a sentinel installed mid-call all reach the parts live."""
    breaker = _real_breaker()
    good = MagicMock(name="swapped_notifier", enabled=True)
    breaker.notifier = good
    assert breaker._emergency_latch.notifier is good
    assert breaker._infra_retry.notifier is good
    assert breaker._owner_notify.notifier is good

    calls = []
    real_connect = breaker._connect

    def counting_connect():
        calls.append(1)
        return real_connect()

    breaker._connect = counting_connect
    breaker.status()
    assert calls, "a `_connect` swapped on the instance must be what the parts open"

    assert breaker._owner_notify._unavailable_sentinel is None
    assert breaker._admission._unavailable_sentinel is None
    breaker._unavailable_sentinel = "installed-mid-call"
    assert breaker._owner_notify._unavailable_sentinel == "installed-mid-call"
    assert breaker._infra_retry._unavailable_sentinel == "installed-mid-call"
    assert breaker._settlement._unavailable_sentinel == "installed-mid-call"


def test_fail_closed_sentinel_still_holds_every_part():
    from types import SimpleNamespace

    breaker = LLMCostCircuitBreaker.fail_closed(
        ":memory:",
        SimpleNamespace(enabled=True),
        RuntimeError("boom"),
        notifier=MagicMock(enabled=True),
    )
    assert breaker._unavailable_sentinel is not None
    for attr, cls in HELD.items():
        assert isinstance(getattr(breaker, attr), cls), attr


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
        EmergencyLatch,
        infrastructure_lock=lock,
        read_unavailable_sentinel=lambda: host["sentinel"],
        write_unavailable_sentinel=write_sentinel,
        read_infrastructure_error=lambda: host["error"],
        write_infrastructure_error=lambda v: host.__setitem__("error", v),
        read_emergency_latch=lambda: (RuntimeError("latched"), {"attempts": "3"}),
        emergency_file_lock=None,
        emergency_latch_path=None,
        emergency_lock_path=None,
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
        InfraRetry,
        infrastructure_lock=threading.Lock(),
        context=lambda: ("run", "mode"),
        best_effort_emergency_snapshot=lambda run_id: {},
        write_emergency_latch=MagicMock(name="write_latch"),
        read_unavailable_sentinel=lambda: host["sentinel"],
        write_unavailable_sentinel=lambda v: host.__setitem__("sentinel", v),
        read_infrastructure_error=lambda: host["error"],
        write_infrastructure_error=lambda v: host.__setitem__("error", v),
        emergency_latch_path=None,
        emergency_lock_path=None,
        infra_retry_backoff_s=None,
        mark_unavailable=None,
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
