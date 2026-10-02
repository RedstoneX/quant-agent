"""Boundary witnesses: the lifted cost-circuit owner-alert pieces build and run with no
breaker (and no pipeline) behind them.

Every collaborator is an explicit keyword-only constructor argument, so each
class is built from stubs alone (clause 5 of tests/boundary_harness.py).
"""
from __future__ import annotations

import functools
import inspect
from unittest.mock import MagicMock

import pytest

from src.cost_circuit.parts.alert_formats import AlertFormats
from src.cost_circuit.parts.episode_wording import EpisodeWording
from src.cost_circuit.parts.owner_notify import OwnerNotify
from src.cost_circuit.parts.shim_guard import _is_class_shim
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


LIFTED = [AlertFormats, EpisodeWording, OwnerNotify]


@pytest.mark.parametrize("cls", LIFTED)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", [
    "src.cost_circuit.parts.alert_formats",
    "src.cost_circuit.parts.episode_wording",
    "src.cost_circuit.parts.owner_notify",
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
