"""Board item 224: realised sector weights of REDUCING orders, and the counted
failure row when the recording itself faults.

RECORDING ONLY, as in tests/test_realised_sector_weights.py, whose `db`,
`_decision` and `_row` helpers these tests share.
"""

import json

import pytest

from tests.test_realised_sector_weights import _decision, _row, db  # noqa: F401


def test_a_reducing_only_run_records_the_sector_and_side_of_what_it_built(db):
    db.record_realised_sector_weights(
        decisions=[_decision("SELL", "AAA", 50.0), _decision("COVER", "BBB", 20.0)],
        sectors={"AAA": "Energy", "BBB": "Tech"},
        total_value=10_000.0,
        run_id="run-4b",
    )
    row = _row(db)[0]
    assert row["entry_orders_built"] == 0
    assert row["reducing_orders_built"] == 2
    stored = {(r["sector"], r["side"]): r for r in json.loads(row["weights_json"])}
    assert set(stored) == {("Energy", "long"), ("Tech", "short")}
    assert stored[("Energy", "long")]["kind"] == "reduce"
    assert stored[("Energy", "long")]["weight_pct"] == pytest.approx(50.0)


def test_constructor_resolves_sectors_for_reducing_orders(monkeypatch):
    import src.sector_reference as sector_reference
    from src.portfolio_constructor import PortfolioConstructor

    monkeypatch.setattr(
        sector_reference,
        "_get_sector",
        lambda s: {"AAA": "Energy"}.get(s, "Unknown"),
    )
    c = PortfolioConstructor()
    c._note_reducing_order_sectors([_decision("SELL", "AAA", 50.0), _decision("SELL", "ZZZ", 10.0)])
    assert c.last_order_sectors == {"AAA": "Energy", "ZZZ": None}


def test_a_missing_sector_source_is_counted_not_recorded_empty_and_not_raised(db):
    """The accessor still raises (no default), the recording boundary catches
    it, and the failure lands as a durable counted row."""
    from types import SimpleNamespace

    from src.pipeline_sector_weights import _record_realised_sector_weights

    pipeline = SimpleNamespace(db=db, portfolio_constructor=SimpleNamespace())
    with pytest.raises(AttributeError):
        _ = pipeline.portfolio_constructor.last_order_sectors
    _record_realised_sector_weights(pipeline, SimpleNamespace(run_id="r"), SimpleNamespace(decisions=[]), 1000)
    assert _row(db) == []
    rows = db.conn.execute(
        "SELECT agent_name, evidence_json FROM specialist_evidence WHERE agent_name = 'realised_sector_weights_failure'"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert payload["event"] == "realised_sector_weights_recording_failed"
    assert payload["error_type"] == "AttributeError"
    assert "last_order_sectors" in payload["error"]


def test_a_renamed_sector_source_does_not_abort_the_decision_stage():
    """A recording fault must never kill the money path: the stage completes,
    the orders are on the context, and the failure is counted."""
    from src.pipeline_context import RunContext
    from src.pipeline_stages import DecisionStage
    from src.portfolio_constructor import PortfolioConstructor
    from tests.test_pm_candidate_accounting import _analysis, _decision
    from tests.test_silent_gates_recorded import _decision_stage_pipeline

    class _Renamed(PortfolioConstructor):
        # Writers still assign it; any READER finds it gone, as after a rename.
        @property
        def last_order_sectors(self):
            raise AttributeError("last_order_sectors")

        @last_order_sectors.setter
        def last_order_sectors(self, value):
            pass

    def _run(constructor):
        pipeline = _decision_stage_pipeline(decision=_decision(), dropped=[], constructor=constructor)
        ctx = RunContext.start("intra_check")
        ctx.positions = []
        ctx.analyses = [_analysis("AAPL")]
        ctx.macro_analysis = None
        ctx.total_value = 100_000.0
        ctx.last_equity = 100_000.0
        ctx.cash = 50_000.0
        ctx.deployable_cash = 50_000.0
        ctx.admitted_symbols = set()
        return pipeline, DecisionStage(pipeline=pipeline).run(ctx)

    _, baseline = _run(PortfolioConstructor())
    pipeline, out = _run(_Renamed())  # must not raise

    # `ctx.portfolio_decision` is set AFTER the recording call, so reaching it
    # proves the stage was not aborted; the orders match an unaffected run.
    assert out.portfolio_decision is not None
    assert [d.model_dump() for d in out.portfolio_decision.decisions] == [
        d.model_dump() for d in baseline.portfolio_decision.decisions
    ]
    counted = [
        c
        for c in pipeline.db.insert_specialist_evidence.call_args_list
        if c.kwargs.get("agent_name") == "realised_sector_weights_failure"
    ]
    assert len(counted) == 1
    assert "last_order_sectors" in counted[0].kwargs["evidence_json"]
