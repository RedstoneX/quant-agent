"""Board item 78 — the never-blank falsifier path, end to end.

Three behaviours, one test each plus their units:
  1. a falsifier blanked by a later wipe is healed back from the
     sentence the model already wrote (and the desk does NOT pay for a
     sentence it already has);
  2. the seat is re-asked exactly once, and that re-ask is paid;
  3. a still-blank name is REFUSED before the book, never invented, and
     the refusal is COUNTED with its reason rather than skipped
     silently.

Hermetic: no network, no provider call. The paid re-ask is a stub that
records that it was called.
"""

from __future__ import annotations

import pytest

from src.soft_exit_never_blank import (
    REFUSAL_COUNT_REASON, REFUSAL_COUNT_STAGE,
    heal_targets_from_raw, refusal_tally,
)


class _Target:
    def __init__(self, symbol, thesis_invalid_if="", risk_allocation_pct=1.0):
        self.symbol = symbol
        self.thesis_invalid_if = thesis_invalid_if
        self.catalyst = ""
        self.risk_allocation_pct = risk_allocation_pct
        self.target_weight_pct = 5.0
        self.action = "BUY"


# ---------------------------------------------------------------- unit: heal

def test_raw_heal_restores_the_sentence_the_model_wrote():
    targets = [_Target("AAPL"), _Target("MSFT", "closes below the 200-day")]
    raw = {"targets": [
        {"symbol": "AAPL", "thesis_invalid_if": "loses the 50-day on volume"},
        {"symbol": "MSFT", "thesis_invalid_if": "something else entirely"},
    ]}
    out, filled = heal_targets_from_raw(targets, raw)
    assert filled == ["AAPL"]
    assert out[0].thesis_invalid_if == "loses the 50-day on volume"
    # never overwrites a sentence that was already stated
    assert out[1].thesis_invalid_if == "closes below the 200-day"


@pytest.mark.parametrize("raw_val", [None, "", "   ", "unknown", "UNKNOWN", 7])
def test_raw_heal_never_invents(raw_val):
    targets = [_Target("AAPL")]
    out, filled = heal_targets_from_raw(
        targets, {"targets": [{"symbol": "AAPL", "thesis_invalid_if": raw_val}]},
    )
    assert filled == []
    from src.models.base import missing_stated_falsifier
    assert missing_stated_falsifier(out[0].thesis_invalid_if)


def test_raw_heal_survives_rubbish_payloads():
    targets = [_Target("AAPL")]
    for raw in (None, {}, {"targets": "nope"}, {"targets": [None, 3]}, []):
        out, filled = heal_targets_from_raw(targets, raw)
        assert filled == []
        assert out is not None


# ------------------------------------------------------------- unit: counted

def test_refusal_tally_counts_every_refusal_with_its_reason():
    tally = refusal_tally(
        ["aapl", "AAPL", "MSFT", "NVDA"],
        {"AAPL": {"outcome": "failed"}, "MSFT": {"outcome": "cap_blocked"}},
    )
    assert tally["refused_count"] == 3
    assert tally["refused_symbols"] == ["AAPL", "MSFT", "NVDA"]
    # a name with no heal record is COUNTED, not dropped
    assert tally["by_heal_outcome"] == {
        "cap_blocked": 1, "failed": 1, "none_recorded": 1,
    }


def test_refusal_tally_of_nothing_is_zero_not_missing():
    assert refusal_tally([], {}) == {
        "refused_count": 0, "refused_symbols": [], "by_heal_outcome": {},
    }


# ------------------------------------- behaviour 1+2: heal before paying

class _StubPM:
    """Just enough PortfolioManagerAgent to drive the heal method."""

    def __init__(self, retry_result=None):
        self._soft_exit_retry_used = False
        self.heals: list[tuple] = []
        self.executed = 0
        self._retry_result = retry_result

    def _record_soft_exit_heal(self, symbols, outcome, detail):
        self.heals.append((list(symbols or []), outcome, detail))

    @staticmethod
    def _target_intent(target, held, total_value, existing_risk_pct=None):
        return "buy"

    def _execute(self, message, retry_kind=None, optional_retry=False):
        self.executed += 1
        return self._retry_result


class _Decision:
    def __init__(self, targets):
        self.targets = targets


class _Result:
    def __init__(self, payload, user_message="original prompt"):
        self._payload = payload
        self.user_message = user_message

    def parse_json(self):
        return self._payload


def _run_heal(pm, decision, result):
    from src.agents.portfolio_manager import PortfolioManagerAgent
    return PortfolioManagerAgent._fill_missing_open_falsifiers(
        pm, decision, result, positions=[], total_value=100000.0,
    )


def test_blank_target_is_healed_from_raw_without_paying():
    """Behaviour 1: the sentence is already written — do not buy it again."""
    pm = _StubPM()
    decision = _Decision([_Target("AAPL")])
    result = _Result({"targets": [
        {"symbol": "AAPL", "thesis_invalid_if": "loses the 50-day on volume"},
    ]})
    out, _ = _run_heal(pm, decision, result)
    assert out.targets[0].thesis_invalid_if == "loses the 50-day on volume"
    assert pm.executed == 0, "the desk paid for a sentence it already had"
    assert pm._soft_exit_retry_used is False, "the one paid retry was spent"
    assert any(o == "mechanical" for _, o, _ in pm.heals), pm.heals


def test_genuinely_blank_target_still_buys_exactly_one_retry():
    """Behaviour 2: raw is blank too, so the seat IS re-asked, once, paid."""
    retry = _Result({"targets": [
        {"symbol": "AAPL", "thesis_invalid_if": "breaks 180 intraday"},
    ]})
    pm = _StubPM(retry_result=retry)
    decision = _Decision([_Target("AAPL")])
    result = _Result({"targets": [{"symbol": "AAPL", "thesis_invalid_if": ""}]})
    out, _ = _run_heal(pm, decision, result)
    assert pm.executed == 1
    assert out.targets[0].thesis_invalid_if == "breaks 180 intraday"
    # and a second call buys nothing more
    pm2_decision = _Decision([_Target("MSFT")])
    _run_heal(pm, pm2_decision, _Result({"targets": []}))
    assert pm.executed == 1
    assert pm2_decision.targets[0].thesis_invalid_if == ""


# ---------------------------------- behaviour 3: refused, counted, not silent

def test_refusal_before_the_book_is_counted_with_its_reason():
    from src.pipeline_stages import _record_soft_exit_refusal_count

    events: list[dict] = []

    class _Pipeline:
        db = None

    class _Ctx:
        run_id = "r1"
        decision_id = None
        soft_exit_heals = {"AAPL": {"outcome": "failed"}}

    import src.pipeline_stages as ps
    original = ps._record_pipeline_event
    ps._record_pipeline_event = lambda p, c, s, stage, outcome, reason="", **d: (
        events.append({"stage": stage, "outcome": outcome,
                       "reason": reason, "details": d})
    )
    try:
        _record_soft_exit_refusal_count(_Pipeline(), _Ctx(), ["AAPL", "MSFT"])
    finally:
        ps._record_pipeline_event = original

    assert len(events) == 1, "the refusal count was not recorded"
    event = events[0]
    assert event["stage"] == REFUSAL_COUNT_STAGE
    assert event["reason"] == REFUSAL_COUNT_REASON
    assert event["outcome"] == "2"
    assert event["details"]["refused_count"] == 2
    assert event["details"]["by_heal_outcome"] == {
        "failed": 1, "none_recorded": 1,
    }


def test_no_refusals_records_nothing():
    from src.pipeline_stages import _record_soft_exit_refusal_count
    import src.pipeline_stages as ps

    events: list = []
    original = ps._record_pipeline_event
    ps._record_pipeline_event = lambda *a, **k: events.append(a)
    try:
        _record_soft_exit_refusal_count(object(), object(), [])
    finally:
        ps._record_pipeline_event = original
    assert events == []


def test_decision_stage_counts_the_refusals_it_makes():
    """The primary gate must call the counter — not only log a warning."""
    import inspect
    import src.stage_decision as sd

    source = inspect.getsource(sd)
    assert "_record_soft_exit_refusal_count" in source, (
        "the before-the-book refusal is still uncounted"
    )
