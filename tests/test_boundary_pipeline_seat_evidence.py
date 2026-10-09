"""Clause-5 witness for src.pipeline_seat_evidence: exercised with stand-ins, no trading pipeline built."""

from types import SimpleNamespace

import src.pipeline_seat_evidence as seat_evidence
from src.pipeline_seat_evidence import (
    _collect_seat_nominations,
    _link_nominations_to_decision,
    _probe_sale_census,
    _risk_edit_snapshot,
)


def test_link_nominations_joins_through_the_stand_in_db_once_a_decision_id_exists():
    calls = []
    db = SimpleNamespace(link_nominations_to_decision=lambda **kw: calls.append(kw) or 2)
    ctx = SimpleNamespace(run_id="run-1", decision_id="dec-1")
    _link_nominations_to_decision(SimpleNamespace(db=db), ctx)
    assert calls == [{"run_id": "run-1", "decision_id": "dec-1"}]


def test_link_nominations_does_nothing_and_touches_no_db_without_a_decision_id():
    _link_nominations_to_decision(SimpleNamespace(db=None), SimpleNamespace(run_id="run-1", decision_id=None))


def test_link_nominations_swallows_a_failing_db():
    def boom(**_kw):
        raise RuntimeError("db down")

    _link_nominations_to_decision(
        SimpleNamespace(db=SimpleNamespace(link_nominations_to_decision=boom)),
        SimpleNamespace(run_id="run-1", decision_id="dec-1"),
    )


def test_collect_seat_nominations_always_returns_all_three_seats_from_bare_inputs():
    seats = _collect_seat_nominations(None, {"carried": "forward"}, [{"analysis": {"nominations": [{"bad": "shape"}]}}])
    assert seats == {"news_analyst": [], "macro_analyst": [], "earnings_analyst": []}


def test_risk_edit_snapshot_reads_the_editable_fields_off_bare_decisions():
    d = SimpleNamespace(
        symbol=" abc ", action="BUY", allocation_pct=5.0, entry_price=10.0, stop_loss=9.0, take_profit=None
    )
    assert _risk_edit_snapshot([d, None]) == {
        ("ABC", "BUY"): {"allocation_pct": 5.0, "entry_price": 10.0, "stop_loss": 9.0, "take_profit": None},
    }


def test_probe_sale_census_finds_a_census_on_a_nested_stand_in_provider():
    inner = SimpleNamespace(last_sale_census={"sale_rows": 3})
    assert _probe_sale_census(SimpleNamespace(providers=[inner])) == {"sale_rows": 3}
    assert _probe_sale_census(SimpleNamespace(last_sale_census={"sale_rows": 0})) is None


def test_every_moved_name_is_the_same_object_through_the_old_import_path():
    import src.pipeline_stages as stages

    for name in (
        "_link_nominations_to_decision",
        "_record_seat_stances",
        "_collect_seat_nominations",
        "_macro_analysis_as_dict",
        "_stash_macro_parse_failure",
        "_RISK_EDITABLE_FIELDS",
        "_risk_edit_snapshot",
        "_risk_event_for",
        "_record_scale_advisory",
        "_probe_sale_census",
    ):
        assert getattr(stages, name) is getattr(seat_evidence, name)
