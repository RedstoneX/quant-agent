"""Boundary witnesses: the intraday parts (safety pass, intra-check session, the gates,
the scan's candidates and the scan body) build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so each class is
built from stubs alone (clause 5 of tests/boundary_harness.py). Follows
tests/test_prompt_facts_parts_boundary.py. This file never imports the pipeline class:
the scan body is imported from its module by name only.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from src.intraday.candidates import IntradayCandidates
from src.intraday.gating import IntradayGating
from src.intraday.safety import IntradaySafety
from src.intraday.session import IntradaySession
from src.pipeline_intraday import IntradayScanBody
from tests.boundary_harness import check_boundary

PARTS = [IntradaySafety, IntradaySession, IntradayGating, IntradayCandidates, IntradayScanBody]
MODULES = [f"src.intraday.{m}" for m in ("safety", "session", "gating", "candidates")]


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


class _State:
    """A stand-in for the host's get/set state view: a plain dict behind get/set."""

    def __init__(self) -> None:
        self.values: dict = {}

    def get(self, name):
        return self.values[name]

    def set(self, name, value) -> None:
        self.values[name] = value


@pytest.mark.parametrize("cls", PARTS)
def test_every_intraday_part_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", MODULES)
def test_every_intraday_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_scan_body_reads_only_what_it_is_handed():
    """The one body left in the shim module is still a part: every `self.` read is a constructor argument."""
    import ast
    tree = ast.parse(inspect.getsource(IntradayScanBody))
    reads = {n.attr for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"}
    handed = set(vars(_build(IntradayScanBody)))
    own = {n for n in dir(IntradayScanBody) if not n.startswith("__")}
    foreign = reads - handed - own
    assert not foreign, foreign


def test_report_persistence_runs_against_a_stub_db_alone():
    """Exercised, not just built: the report body runs to completion with only the stub db behind it."""
    part = _build(IntradaySession, db=MagicMock(name="db"), state=_State(), persist_intra_check_report=None)
    assert part._persist_intra_check_report({"status": "ok", "run_id": "r1"}) is None


def test_snapshot_health_tracking_runs_against_a_stub_db():
    db = MagicMock(name="db")
    part = _build(IntradayGating, db=db, state=_State())
    part._track_intraday_snapshot_ok("AAPL")
    assert db.method_calls, "a healthy snapshot must be recorded through the db it was handed"


def test_move_in_atr_is_static_and_needs_no_host():
    assert isinstance(inspect.getattr_static(IntradayCandidates, "_intraday_move_in_atr"), staticmethod)
    assert "self" not in inspect.signature(IntradayCandidates._intraday_move_in_atr).parameters


def test_assigned_host_state_lands_on_the_state_view_not_the_part():
    """A body that assigns `self._paid_scan_waited` writes the HOST's attribute (through the view), never a copy."""
    state = _State()
    part = _build(IntradayGating, state=state)
    part._paid_scan_waited = True
    part._paid_scan_waited_for = "morning"
    assert state.values == {"_paid_scan_waited": True, "_paid_scan_waited_for": "morning"}
    assert "_paid_scan_waited" not in vars(part)
    assert part._paid_scan_waited_for == "morning"


def test_session_is_handed_the_safety_preamble_not_owning_it():
    """`_run_intra_check_body` reads `_run_intra_safety_preamble`; the session part never defines it."""
    assert not hasattr(IntradaySession, "_run_intra_safety_preamble")
    assert hasattr(IntradaySafety, "_run_intra_safety_preamble")
    part = _build(IntradaySession, run_intra_safety_preamble=lambda *a, **k: "HANDED IN", state=_State())
    assert part._run_intra_safety_preamble() == "HANDED IN"


def test_candidates_part_is_handed_the_scan_body_and_the_gates():
    """The scan wrapper, the body and the gates are three parts; the wrapper owns none of the other two."""
    for name in ("_intraday_opportunity_scan_body", "_intraday_scan_process_lock",
                 "_recently_intraday_evaluated", "_blocking_owner_session"):
        assert not hasattr(IntradayCandidates, name), name
    part = _build(IntradayCandidates, intraday_opportunity_scan_body=lambda ctx: {"status": "HANDED IN"})
    assert part._intraday_opportunity_scan_body(None) == {"status": "HANDED IN"}
