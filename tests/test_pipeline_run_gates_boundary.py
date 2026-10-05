"""Witness: the run gates are boundaries — built and exercised from stubs, no pipeline.

`src.pipeline_cost_gate` (paid-analysis gate) and `src.pipeline_halt_gates`
(kill switch + evidence gate) take the pipeline as a duck-typed first argument.
A `SimpleNamespace` carrying only the attributes each function reads is enough
to drive them, which is what docs/ARCHITECTURE.md section 3 calls a boundary.
This file deliberately never names the pipeline class or module.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boundary_harness import check_boundary  # noqa: E402
from src import evidence_gate, pipeline_cost_gate, pipeline_halt_gates  # noqa: E402
from src.cost_circuit import PaidAnalysisSuspended  # noqa: E402


@pytest.mark.parametrize("module", ["src.pipeline_cost_gate", "src.pipeline_halt_gates"])
def test_harness_passes_run_gate_modules(module):
    v = check_boundary(module)
    assert v.passed, v.failures


class _Circuit:
    def __init__(self, *, suspended=False):
        self.suspended = suspended
        self.sessions = []

    def activate_session(self, run_id, mode):
        self.sessions.append((run_id, mode))

    def require_paid_analysis(self, agent_name):
        if self.suspended:
            raise PaidAnalysisSuspended("open", {"suspended": True})

    def status(self):
        return {"enabled": True, "suspended": self.suspended}


class _Seat:
    def __init__(self):
        self.circuit = None

    def set_cost_circuit(self, circuit):
        self.circuit = circuit


def _stub(circuit):
    return SimpleNamespace(
        cost_circuit=circuit, tech_analyst=_Seat(), portfolio_manager=_Seat(),
        _attach_cost_circuit_to_agents=lambda: None,
    )


def test_cost_gate_activates_and_preflights_from_a_stub():
    circuit = _Circuit()
    stub = _stub(circuit)
    pipeline_cost_gate._activate_cost_session(stub, "run-1", "morning")
    assert stub._active_cost_run_context == ("run-1", "morning")
    assert circuit.sessions == [("run-1", "morning")]
    pipeline_cost_gate._require_paid_analysis(stub, "tech_analyst")  # does not raise
    assert pipeline_cost_gate._cost_circuit_status(stub) == {"enabled": True, "suspended": False}


def test_cost_gate_refuses_paid_analysis_when_the_circuit_is_open():
    stub = _stub(_Circuit(suspended=True))
    with pytest.raises(PaidAnalysisSuspended):
        pipeline_cost_gate._require_paid_analysis(stub, "portfolio_manager")


def test_cost_gate_attaches_the_circuit_to_every_seat_it_finds():
    circuit = _Circuit()
    stub = _stub(circuit)
    pipeline_cost_gate._attach_cost_circuit_to_agents(stub)
    assert stub.tech_analyst.circuit is circuit
    assert stub.portfolio_manager.circuit is circuit


def test_suspension_payload_carries_orders_and_waiting_filings():
    payload = pipeline_cost_gate._paid_suspended_payload(
        "run-2", orders=[{"symbol": "X"}], error=RuntimeError("boom"),
        filings_waiting=[{"a": 1}, {"b": 2}],
    )
    assert payload["status"] == "paid_analysis_suspended"
    assert payload["orders"] == [{"symbol": "X"}]
    assert payload["error"] == "boom"
    assert payload["filings_waiting_count"] == 2


def test_late_safety_suspension_marks_morning_only(monkeypatch):
    from src import decision_checkpoint as dc
    written = []
    monkeypatch.setattr(dc, "write_status", lambda *a: written.append(a))
    stub = SimpleNamespace(_paid_suspended_payload=pipeline_cost_gate._paid_suspended_payload)
    out = pipeline_cost_gate._paid_suspension_after_late_safety(
        stub, "run-3", session="morning", error=RuntimeError("x"), where="pm", extra={"k": 1},
    )
    assert out["k"] == 1 and out["paid_analysis_suspended"] is True
    assert written == [("morning", "paid_analysis_suspended")]
    pipeline_cost_gate._paid_suspension_after_late_safety(
        stub, "run-4", session="intra_check", error=RuntimeError("x"), where="pm",
    )
    assert len(written) == 1


def test_kill_switch_halts_only_while_the_flag_file_exists(tmp_path):
    flag = tmp_path / "HALT"
    stub = SimpleNamespace(_kill_switch_path=flag)
    assert pipeline_halt_gates._kill_switch_halt_result(stub, "run-5") is None
    flag.touch()
    out = pipeline_halt_gates._kill_switch_halt_result(stub, "run-5", session="morning")
    assert out["status"] == "kill_switch_halted"
    assert out["orders"] == [] and out["session"] == "morning"
    assert pipeline_halt_gates._kill_switch_halt_result(SimpleNamespace(_kill_switch_path=None), "r") is None


def test_evidence_gate_proceeds_from_a_stub_when_every_seat_answered():
    recorded = []
    stub = SimpleNamespace(
        db=None,
        _record_name_coverage=lambda ctx, record: recorded.append("coverage"),
    )
    ctx = SimpleNamespace(
        data_status={"technical": "ok"}, analyses=[], run_id="run-6",
        session="morning", evidence_freshness=None, positions=[],
    )
    assert pipeline_halt_gates._evidence_gate_skip(stub, ctx, "run-6") is None
    assert recorded == ["coverage"]
    assert isinstance(stub._last_evidence_freshness, dict)
    assert stub._last_decision_data_status == {"technical": "ok"}


def test_evidence_gate_boundary_propagates_an_unknown_verdict(monkeypatch, caplog):
    def broken_evaluate(_data_status):
        raise RuntimeError("gate implementation failed")

    monkeypatch.setattr(evidence_gate, "evaluate", broken_evaluate)
    stub = SimpleNamespace()
    ctx = SimpleNamespace(data_status={"tech": "ok"})
    with caplog.at_level("ERROR", logger=pipeline_halt_gates.logger.name):
        with pytest.raises(RuntimeError, match="gate implementation failed"):
            pipeline_halt_gates._evidence_gate_skip(stub, ctx, "run-7")
    assert "REFUSING the decision" in caplog.text
    assert "PROCEEDING" not in caplog.text
