"""Board item 177 — the portfolio manager is not paid to decide nothing.

Every test here is about ONE property: the gate skips only when every
decision the seat is the decider for is provably empty, and ANY doubt
produces a call.
"""

from dataclasses import dataclass

import pytest

from src.pm_gate import evaluate_pm_gate


@dataclass
class _Precheck:
    opportunity: object = None
    telemetry_available: bool = True
    held_below_entry_bar: tuple = ()


def _empty(**over):
    base = dict(
        blocked={"AAA": ["r2_rating_not_actionable"]},
        ranked=[],
        precheck=_Precheck(),
        held_symbols={"MSFT"},
        evidence_registry={"MSFT": {"technical": "bullish"}},
        pending_soft_exit_heals=None,
    )
    base.update(over)
    return base


def test_skips_only_when_everything_is_empty():
    v = evaluate_pm_gate(**_empty())
    assert v.skip is True
    assert all(c.empty for c in v.conditions)
    assert {c.name for c in v.conditions} == {
        "open_or_add", "close_held_failing_entry_bar", "trim_held",
        "rotation", "rejection_bookkeeping", "soft_exit_heals",
    }


def test_eligible_buy_candidate_forces_a_call():
    v = evaluate_pm_gate(**_empty(blocked={"AAA": []}))
    assert v.skip is False
    assert "open_or_add" in v.reason


def test_holding_below_the_entry_bar_forces_a_call():
    """The owner's 2026-10-01 mandate: a holding that fails the desk's own
    fresh-entry bar must be SOLD. A gate keyed on buy candidates alone would
    suppress the call in exactly this session."""
    v = evaluate_pm_gate(**_empty(precheck=_Precheck(held_below_entry_bar=("MSFT",))))
    assert v.skip is False
    assert "close_held_failing_entry_bar" in v.reason


def test_bearish_read_on_a_holding_forces_a_call():
    v = evaluate_pm_gate(**_empty(
        evidence_registry={"MSFT": {"news": "bearish"}},
    ))
    assert v.skip is False
    assert "trim_held" in v.reason


def test_unknown_stance_word_on_a_holding_forces_a_call():
    """An allow-list, not a block-list: a stance word this desk has not seen
    before must never be assumed harmless."""
    v = evaluate_pm_gate(**_empty(
        evidence_registry={"MSFT": {"news": "deteriorating"}},
    ))
    assert v.skip is False


def test_rotation_opportunity_forces_a_call():
    v = evaluate_pm_gate(**_empty(precheck=_Precheck(opportunity=object())))
    assert v.skip is False
    assert "rotation" in v.reason


def test_candidate_shown_forces_a_call_for_the_bookkeeping():
    v = evaluate_pm_gate(**_empty(ranked=[object()]))
    assert v.skip is False
    assert "rejection_bookkeeping" in v.reason


def test_queued_soft_exit_heal_forces_a_call():
    v = evaluate_pm_gate(**_empty(pending_soft_exit_heals={"MSFT": {"x": "y"}}))
    assert v.skip is False


@pytest.mark.parametrize("over", [
    {"precheck": None},
    {"precheck": _Precheck(telemetry_available=False)},
    {"blocked": None},
    {"blocked": {"AAA": "not-a-list"}},
    {"ranked": None},
    {"evidence_registry": None},
    {"evidence_registry": {"MSFT": "not-a-dict"}},
    {"held_symbols": 7},
    {"pending_soft_exit_heals": 3},
    {"precheck": _Precheck(held_below_entry_bar=None)},
])
def test_any_ambiguous_state_results_in_a_call(over):
    """FAIL OPEN. A skipped call costs about 16 cents; a missed sell costs
    real money."""
    assert evaluate_pm_gate(**_empty(**over)).skip is False


def test_an_exception_anywhere_results_in_a_call():
    class _Boom:
        @property
        def telemetry_available(self):
            raise RuntimeError("boom")

    assert evaluate_pm_gate(**_empty(precheck=_Boom())).skip is False


def test_the_record_names_every_condition():
    rec = evaluate_pm_gate(**_empty()).as_record()
    assert rec["outcome"] == "skipped"
    assert len(rec["conditions"]) == 6
    assert all(c["detail"] for c in rec["conditions"])


def test_gate_holds_no_numbers():
    """No minimum interval, no skip-count, no cost threshold — a pure
    emptiness test."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "pm_gate.py").read_text()
    body = src.split('"""', 2)[2]
    assert not re.search(r"(?<![\w.])\d+\.\d+", body)
