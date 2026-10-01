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
