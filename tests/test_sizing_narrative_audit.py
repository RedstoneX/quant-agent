"""The sizing-narrative cross-check must leave a trace that it RAN.

Zero rows previously meant three different things (agreed / never reached /
raised and discarded). These tests pin all three apart.

Every symbol and every number below is invented. The two mismatch cases
reproduce the NUMERIC SHAPE of the two largest findings in the production
record -- a narrated 3.75% against an emitted 0.5%, and a narrated 2.5%
against an emitted 0.5% -- without copying any real ticker, size or desk
prose into the repository.
"""

from __future__ import annotations

import json
import types

import pytest

import src.pipeline_candidate_records as records
from src.sizing_narrative_audit import (
    SIZING_NARRATIVE_STAGE,
    audit_sizing_narrative,
)


class _Target:
    def __init__(self, symbol: str, risk_allocation_pct: float | None) -> None:
        self.symbol = symbol
        self.risk_allocation_pct = risk_allocation_pct


def _decision(sizing_logic: str, targets: list[_Target]):
    return types.SimpleNamespace(
        reasoning_chain=types.SimpleNamespace(sizing_logic=sizing_logic),
        targets=targets,
    )


@pytest.fixture()
def recorded(monkeypatch):
    """Capture every evidence row the audit writes, as the recorder sees it."""
    rows: list[dict] = []
    monkeypatch.setattr(
        records,
        "_persist_evidence",
        lambda db, **kwargs: rows.append(kwargs),
    )
    return rows


def _events(rows: list[dict]) -> list[dict]:
    return [json.loads(r["evidence_json"]) for r in rows]


def _pipeline_and_ctx():
    pipeline = types.SimpleNamespace(db=object())
    ctx = types.SimpleNamespace(run_id="run-test", decision_id="dec-test")
    return pipeline, ctx


def test_largest_real_shape_fires_and_the_hit_is_recorded(recorded):
    """3.75% narrated against an emitted 0.5% is flagged AND written down."""
    pipeline, ctx = _pipeline_and_ctx()
    decision = _decision(
        "ZZA is a starter, so we are risking 3.75% on it this session.",
        [_Target("ZZA", 0.5)],
    )

    audit_sizing_narrative(pipeline, ctx, decision)

    events = _events(recorded)
    mismatches = [e for e in events if e["outcome"] == "mismatch"]
    assert len(mismatches) == 1, events
    assert mismatches[0]["stage"] == SIZING_NARRATIVE_STAGE
    assert mismatches[0]["prose_pct"] == 3.75
    assert mismatches[0]["field_pct"] == 0.5
    assert "ZZA" in mismatches[0]["reason"]
    # The symbol-scoped row must carry the symbol, or the hit is unreadable.
    assert any(r["symbol"] == "ZZA" for r in recorded)
    checked = [e for e in events if e["outcome"] == "checked"]
    assert len(checked) == 1 and checked[0]["mismatches"] == 1


def test_second_largest_real_shape_fires_and_is_recorded(recorded):
    """2.5% narrated against an emitted 0.5%, one sentence, two symbols."""
    pipeline, ctx = _pipeline_and_ctx()
    decision = _decision(
        "ZZB and ZZC each take 2.5% risk on this entry.",
        [_Target("ZZB", 0.5), _Target("ZZC", 2.5)],
    )

    audit_sizing_narrative(pipeline, ctx, decision)

    events = _events(recorded)
    mismatches = [e for e in events if e["outcome"] == "mismatch"]
    # ZZC's field agrees with the narrative; only ZZB disagrees.
    assert [m["reason"].split(":")[0] for m in mismatches] == ["ZZB"]
    assert mismatches[0]["prose_pct"] == 2.5
    assert mismatches[0]["field_pct"] == 0.5


def test_a_clean_session_still_records_that_the_check_ran(recorded):
    """THE ACTUAL DEFECT: agreement used to write nothing at all.

    Without this row, an evidence stream holding no mismatch is
    indistinguishable from a call site that never executed -- which is
    exactly the confusion that made five real findings look like a silent
    check.
    """
    pipeline, ctx = _pipeline_and_ctx()
    decision = _decision(
        "ZZD is sized at 1.0% risk, in line with the book.",
        [_Target("ZZD", 1.0)],
    )

    audit_sizing_narrative(pipeline, ctx, decision)

    events = _events(recorded)
    assert [e["outcome"] for e in events] == ["checked"]
    assert events[0]["mismatches"] == 0
    assert events[0]["checkable_targets"] == 1
    assert events[0]["prose_chars"] > 0


def test_targets_without_an_emitted_risk_field_are_not_counted(recorded):
    """The recorded denominator is the pairs the detector could judge."""
    pipeline, ctx = _pipeline_and_ctx()
    decision = _decision(
        "ZZE carries 1.0% risk; ZZF is a legacy weight-only line.",
        [_Target("ZZE", 1.0), _Target("ZZF", None)],
    )

    audit_sizing_narrative(pipeline, ctx, decision)

    events = _events(recorded)
    assert events[-1]["checkable_targets"] == 1


def test_a_raising_check_is_recorded_instead_of_swallowed(
    recorded,
    monkeypatch,
    caplog,
):
    """A failure used to vanish into `logger.debug`; it must leave a row."""
    import src.risk_narrative_check as check_module

    def _boom(_decision):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(check_module, "check_sizing_narrative", _boom)
    pipeline, ctx = _pipeline_and_ctx()
    decision = _decision("ZZG takes 1.0% risk.", [_Target("ZZG", 1.0)])

    with caplog.at_level("ERROR"):
        audit_sizing_narrative(pipeline, ctx, decision)

    events = _events(recorded)
    assert [e["outcome"] for e in events] == ["error"]
    assert events[0]["reason"] == "RuntimeError"
    assert "sizing_narrative_check raised" in caplog.text


def test_no_targets_still_records_a_checked_row(recorded):
    """Even a session the detector short-circuits must prove it ran."""
    pipeline, ctx = _pipeline_and_ctx()
    audit_sizing_narrative(pipeline, ctx, _decision("No new names today.", []))

    events = _events(recorded)
    assert [e["outcome"] for e in events] == ["checked"]
    assert events[0]["checkable_targets"] == 0
