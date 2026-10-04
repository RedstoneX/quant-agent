"""Board item 177: reuse a technical verdict only when no input has moved.

The rule under test is an INPUT-equality rule. There is deliberately no
timer, counter, cooldown or sampling rate anywhere in it, and no tolerance
band on any number — so these tests pin both the reuse and, more importantly,
every path that must fall back to asking the seat.
"""

import pytest

from src.data.tech_store import TechStore
from src.evidence_gate import READ_CARRIED
from src.models import TechAnalysisResult
from src.research_throttle import (
    carry_unchanged_tech_reads,
    tech_input_fingerprint,
)


class _Bar:
    def __init__(self, low, high, close):
        self.low, self.high, self.close = low, high, close


def _data(close=100.0):
    return {
        "symbol": "AAA",
        "bars": [_Bar(99.0, 101.0, close)],
        "indicators": {"atr_14": 2.0},
    }


def _fp(**kw):
    return tech_input_fingerprint("AAA", symbol_data=_data(**{
        k: v for k, v in kw.items() if k == "close"}), **{
        k: v for k, v in kw.items() if k != "close"})


def test_identical_inputs_give_the_same_fingerprint():
    assert _fp() == _fp()


def test_a_new_bar_changes_the_fingerprint():
    a = tech_input_fingerprint("AAA", symbol_data=_data())
    more = _data()
    more["bars"].append(_Bar(100.0, 102.0, 101.0))
    assert a != tech_input_fingerprint("AAA", symbol_data=more)


def test_a_moved_live_price_changes_the_fingerprint():
    """The one input that actually moves intraday must be watched."""
    a = tech_input_fingerprint(
        "AAA", symbol_data=_data(), intraday={"live_price": 100.0})
    b = tech_input_fingerprint(
        "AAA", symbol_data=_data(), intraday={"live_price": 100.01})
    assert a != b


def test_no_tolerance_band_on_price():
    """A one-ULP price move is a move. No rounding, no 'close enough'."""
    import math
    base = 100.0
    nudged = math.nextafter(base, math.inf)
    assert tech_input_fingerprint(
        "AAA", symbol_data=_data(), intraday={"live_price": base},
    ) != tech_input_fingerprint(
        "AAA", symbol_data=_data(), intraday={"live_price": nudged})


def test_changed_prior_rating_or_valuation_or_macro_changes_it():
    base = tech_input_fingerprint("AAA", symbol_data=_data())
    assert base != tech_input_fingerprint(
        "AAA", symbol_data=_data(), prior_rating={"rating": "buy"})
    assert base != tech_input_fingerprint(
        "AAA", symbol_data=_data(), valuation={"trailing_pe": 11.0})
    assert base != tech_input_fingerprint(
        "AAA", symbol_data=_data(), prior_macro_regime="risk_off")


@pytest.mark.parametrize("bad", [None, {}, {"symbol": "AAA"},
                                 {"symbol": "AAA", "bars": []}])
def test_missing_bars_fails_open(bad):
    assert tech_input_fingerprint("AAA", symbol_data=bad) is None


def test_unfingerprintable_input_fails_open():
    class Opaque:
        __slots__ = ()
    assert tech_input_fingerprint(
        "AAA", symbol_data=_data(), valuation={"x": Opaque()}) is None


def _entry(fp, rating="buy", conviction="high"):
    return {"input_fingerprint": fp,
            "last_result": {"symbol": "AAA", "rating": rating,
                            "conviction": conviction}}


def test_unchanged_inputs_are_carried():
    fp = tech_input_fingerprint("AAA", symbol_data=_data())
    assert carry_unchanged_tech_reads({"AAA": fp}, {"AAA": _entry(fp)})


def test_changed_inputs_are_not_carried():
    fp = tech_input_fingerprint("AAA", symbol_data=_data())
    assert carry_unchanged_tech_reads({"AAA": fp}, {"AAA": _entry("other")}) == {}


@pytest.mark.parametrize("entry", [
    None, {}, {"input_fingerprint": "fp"},                 # no stored verdict
    {"input_fingerprint": "fp", "last_result": "garbled"},  # unreadable
    {"input_fingerprint": "fp", "last_result": {"symbol": "AAA"}},  # no verdict
    {"input_fingerprint": "fp", "last_result": {"symbol": "BBB",
                                                "rating": "buy",
                                                "conviction": "high"}},
    {"input_fingerprint": None, "last_result": {"symbol": "AAA",
                                                "rating": "buy",
                                                "conviction": "high"}},
])
def test_an_unreadable_or_absent_prior_read_asks(entry):
    assert carry_unchanged_tech_reads({"AAA": "fp"}, {"AAA": entry}) == {}


def test_a_none_fingerprint_never_matches():
    assert carry_unchanged_tech_reads({"AAA": None}, {"AAA": _entry(None)}) == {}


def _result():
    return TechAnalysisResult(
        symbol="AAA", rating="buy", conviction="high",
        thesis_invalid_if="loses 99", reasoning="because",
        entry_price=100.0, stop_loss=95.0, reference_target=120.0,
        setup_type="range", expected_horizon_sessions=10,
        support_levels=[95.0], resistance_levels=[120.0],
        reasoning_chain={"trend": "up", "momentum": "firm",
                         "volatility": "normal", "support_resistance": "mid", "volume": "average"},
    )


def test_store_round_trips_fingerprint_and_verdict(tmp_path):
    store = TechStore(data_dir=str(tmp_path))
    a = _result()
    a.input_fingerprint = "fp-1"
    store.update([a])
    entry = store.load()["AAA"]
    assert entry["input_fingerprint"] == "fp-1"
    assert entry["last_result"]["rating"] == "buy"
    carried = carry_unchanged_tech_reads({"AAA": "fp-1"}, store.load())
    assert carried["AAA"]["conviction"] == "high"


def test_a_carried_verdict_is_recorded_as_carried_not_refreshed():
    """Item 227's stamp must not read a reuse as a fresh read."""
    a = _result()
    assert a.read_state == "refreshed_this_session"
    a.read_state = READ_CARRIED
    assert a.read_state == "carried_forward"
    assert a.model_dump()["read_state"] == READ_CARRIED


# ---------------------------------------------------------------------------
# End to end through `analyze_batch` and the store. These pin the property the
# unit tests above cannot: that a fingerprint STORED beside a verdict can be
# matched by the NEXT run, i.e. that the key does not hash the store's own
# bookkeeping (history, the stored fingerprint, the stored verdict).
# ---------------------------------------------------------------------------

from src.agents.tech_analyst import TechAnalystAgent  # noqa: E402


class _Spy:
    def __init__(self, agent, verdicts):
        self.calls, self.verdicts = [], verdicts
        agent._analyze_batch_uncached = self

    def __call__(self, symbols, **kw):
        self.calls.append([s["symbol"] for s in symbols])
        return {s["symbol"]: _result() for s in symbols}, None


def _agent_and_spy():
    agent = TechAnalystAgent.__new__(TechAnalystAgent)
    return agent, _Spy(agent, {"AAA": _result()})


def test_a_second_identical_run_is_carried_through_the_store(tmp_path):
    agent, spy = _agent_and_spy()
    store = TechStore(data_dir=str(tmp_path))
    first, _ = agent.analyze_batch([_data()], prior_ratings=store.load())
    store.update([a for a in first.values() if a])
    second, res = agent.analyze_batch([_data()], prior_ratings=store.load())
    # Run 2 may legitimately ask once more (the prior line it reads changed
    # from "no prior" to the stored rating); run 3 must then be carried.
    store.update([a for a in second.values() if a])
    third, res3 = agent.analyze_batch([_data()], prior_ratings=store.load())
    assert third["AAA"].read_state == READ_CARRIED
    assert res3 is None and len(spy.calls) == 2


def test_a_moved_input_asks_the_seat_even_with_a_stored_verdict(tmp_path):
    agent, spy = _agent_and_spy()
    store = TechStore(data_dir=str(tmp_path))
    for _ in range(2):
        out, _r = agent.analyze_batch([_data()], prior_ratings=store.load())
        store.update([a for a in out.values() if a])
    n = len(spy.calls)
    out, _r = agent.analyze_batch([_data(close=100.01)], prior_ratings=store.load())
    assert len(spy.calls) == n + 1 and out["AAA"].read_state != READ_CARRIED


def test_an_input_that_cannot_be_compared_asks_the_seat(tmp_path):
    agent, spy = _agent_and_spy()
    store = TechStore(data_dir=str(tmp_path))
    for _ in range(2):
        out, _r = agent.analyze_batch([_data()], prior_ratings=store.load())
        store.update([a for a in out.values() if a])
    n = len(spy.calls)
    bad = _data()
    bad["indicators"] = {"atr_14": object()}
    out, _r = agent.analyze_batch([bad], prior_ratings=store.load())
    assert len(spy.calls) == n + 1 and out["AAA"].read_state != READ_CARRIED


def test_agent_inherits_no_mixin_and_the_shim_reads_the_instance_spy():
    from src.agents.base import BaseAgent
    assert TechAnalystAgent.__bases__ == (BaseAgent,)
    agent, spy = _agent_and_spy()
    agent.analyze_batch([{"symbol": "AAA"}])
    assert spy.calls == [["AAA"]]
