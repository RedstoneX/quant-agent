"""Clause-5 witness for src.pipeline_risk_gate (conversion step 7, MONEY):
RiskGate is built from explicit stand-ins and exercised with no trading
pipeline anywhere in this file. One refusal, one resize, one durable record."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
from boundary_harness import check_boundary  # noqa: E402

from src.models import RiskModification, TradeDecision
from src.pipeline_context import RunContext
from src.pipeline_risk_gate import RiskGate


class _AgentLogSink:
    """Stand-in for the one `db` method the gate calls."""

    def __init__(self):
        self.rows = []

    def insert_agent_log(self, **row):
        self.rows.append(row)


class _RefusingEngine:
    """Stand-in risk engine: refuses every BUY with one hard-block violation."""

    def __init__(self, rule_id):
        self.rule_id = rule_id
        self.calls = []

    def check(self, **kw):
        self.calls.append(kw)
        return [SimpleNamespace(rule=self.rule_id, message="blocked by stand-in")]


def _buy(symbol, alloc=10.0, entry=100.0, stop=95.0, tp=120.0):
    return TradeDecision(
        symbol=symbol,
        action="BUY",
        allocation_pct=alloc,
        entry_price=entry,
        stop_loss=stop,
        take_profit=tp,
        reasoning="boundary test",
    )


def _ctx():
    return RunContext(run_id="run-boundary", session="morning")


def _gate(engine=None, db=None):
    return RiskGate(risk_engine=engine, db=db, sweeper=lambda: None, config=None)


def test_module_passes_the_boundary_harness():
    v = check_boundary("src.pipeline_risk_gate")
    assert v.passed, v.failures


def test_refusal_without_a_pipeline():
    from src.risk.rules import HARD_BLOCK_RULES

    rule = sorted(HARD_BLOCK_RULES)[0]
    engine = _RefusingEngine(rule)
    allowed, violations, blocked = _gate(engine)._filter_hard_risk_decisions(
        [_buy("AAA")],
        positions=[],
        total_value=10_000.0,
        cash=10_000.0,
    )
    assert allowed == [] and violations == [] and len(blocked) == 1  # hard blocks are not "remaining"
    assert engine.calls[0]["total_value"] == 10_000.0


def test_resize_without_a_pipeline():
    mod = RiskModification(symbol="AAA", field="alloc", original_value=10.0, new_value=4.0, reason="stand-in")
    updated, rejected = _gate()._apply_risk_modifications([_buy("AAA", alloc=10.0)], [mod])
    assert rejected == []
    assert [d.allocation_pct for d in updated] == [4.0]


def test_hard_block_is_recorded_through_the_injected_db():
    db = _AgentLogSink()
    ctx = _ctx()
    _gate(db=db)._persist_hard_risk_block(ctx, "R1: stand-in", stage="pre_rm")
    assert db.rows[0]["agent_name"] == "risk_gate"
    assert db.rows[0]["status"] == "hard_risk_block"
    assert db.rows[0]["run_id"] == "run-boundary"


def test_recording_failure_never_raises():
    class _Broken:
        def insert_agent_log(self, **row):
            raise RuntimeError("disk full")

    ctx = _ctx()
    _gate(db=_Broken())._persist_hard_risk_block(ctx, "R1", stage="pre_rm")


def test_unread_filing_refusal_without_a_pipeline():
    kept = RiskGate._refuse_queued_earnings_buys(
        [_buy("AAA"), _buy("BBB")],
        [{"symbol": "aaa", "queued": True}],
    )
    assert [d.symbol for d in kept] == ["BBB"]


def test_risk_gate_is_reachable_without_importing_the_pipeline():
    """Step 7 is a real boundary, not a renamed shim.

    `RiskGateMixin` was deleted on 2026-10-05; `build_risk_gate` takes its
    collaborators by value, so a process that builds and drives the gate must
    never load `src.pipeline`. Checked in a SUBPROCESS: an in-process check
    would have to evict modules from `sys.modules`.
    """
    import os
    import subprocess

    program = (
        "import sys\n"
        "from src.risk_gate_build import build_risk_gate\n"
        "gate = build_risk_gate(config=None)\n"
        "updated, rejected = gate._apply_risk_modifications([], [])\n"
        "assert (updated, rejected) == ([], [])\n"
        "leaked = [m for m in sys.modules if m == 'src.pipeline']\n"
        "assert not leaked, leaked\n"
        "print('CLEAN')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env={**os.environ, "PYTHONPATH": "."},
    )
    assert done.returncode == 0, done.stderr
    assert "CLEAN" in done.stdout
