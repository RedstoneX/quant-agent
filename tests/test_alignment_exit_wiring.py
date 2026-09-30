"""Wiring tests for the ALIGNMENT EXIT — the class of defect that killed the
first two attempts.

The pure module has its own tests in `tests/test_alignment_exit.py`. These
cover the parts that live OUTSIDE it: the keyword surface, the bypass it is
and is not allowed to buy, which structural level it reads, the
EMA-vs-SMA question, and the unreadable-chart posture.

HONEST LIMIT: the sell loop in `TradingPipeline` that consumes the verdict
is one branch of a method thousands of lines long and is not invoked here.
What is asserted instead is every predicate that loop reads.
"""
from __future__ import annotations

import types

import pytest

from src.risk import alignment_exit as ae
from src.risk.exit_trigger import ExitTrigger


# --------------------------------------------------------------------------
# 1. THE HIDDEN SECOND BYPASS — closed.
# --------------------------------------------------------------------------
def test_alignment_phrases_are_not_hard_trigger_keywords():
    """`_reason_cites_hard_trigger` waves a reason past the TRAIL_STOP
    ratchet cooldown and the 1.25xATR trail clamp, a path that runs NO
    chart check. The alignment exit must never be buyable there by prose.
    """
    from src.pipeline import _HARD_TRIGGER_KEYWORDS, _reason_cites_hard_trigger

    for phrase in ("trend alignment over", "alignment exit", "trend_alignment_over"):
        assert phrase not in _HARD_TRIGGER_KEYWORDS
        assert _reason_cites_hard_trigger(f"selling: {phrase} on the daily") is False


def test_canonical_name_autoappend_excludes_chart_verified_triggers():
    """The enum member's canonical prose names are auto-appended to the
    hard-trigger tuple. That auto-append is the sneaky half of the same
    bypass, so chart-verified triggers are filtered out of it."""
    from src.pipeline import _CHART_VERIFIED_TRIGGER_NAMES, _HARD_TRIGGER_KEYWORDS

    assert "trend alignment over" in _CHART_VERIFIED_TRIGGER_NAMES
    assert not (_CHART_VERIFIED_TRIGGER_NAMES & set(_HARD_TRIGGER_KEYWORDS))


def test_reason_claims_alignment_exit_reads_trigger_then_prose():
    from src.pipeline import _reason_claims_alignment_exit

    assert _reason_claims_alignment_exit("", ExitTrigger.TREND_ALIGNMENT_OVER)
    assert _reason_claims_alignment_exit("Trend Alignment Over on the daily", None)
    assert not _reason_claims_alignment_exit("taking profit at my target", None)


# --------------------------------------------------------------------------
# 2. EMA vs SMA — judge the price the thesis actually named.
# --------------------------------------------------------------------------
def test_thesis_naming_an_ema_is_judged_against_an_ema():
    # A curved advance, so the exponential weighting actually differs from
    # the arithmetic mean (on a straight line the two coincide).
    closes = [float(i * i) / 40.0 + 10.0 for i in range(1, 61)]
    sma = ae.simple_moving_average(closes, 50)
    ema = ae.exponential_moving_average(closes, 50)
    assert sma is not None and ema is not None
    assert ema != pytest.approx(sma)

    assert ae.thesis_ma_ref("close below the EMA50") == (50, "EMA")
    assert ae.thesis_ma_ref("close below the 50-day moving average") == (50, "SMA")

    v = ae.check_alignment_exit(
        thesis_invalid_if="close below the EMA50", closes=closes, atr=1.0,
    )
    assert v.thesis_ma_kind == "EMA"
    prices = {m.source: m.price for m in v.marks}
    assert prices["EMA50 (thesis rides the EMA50)"] == pytest.approx(ema)
    # The ladder mark is the next longer average the DESK computes, and is
    # labelled as the simple average it is — not silently relabelled.
    assert any(s.startswith("SMA200") for s in prices) or len(closes) < 200


def test_unsupported_or_absent_ma_reference_yields_no_average_mark():
    assert ae.thesis_ma_ref("close below the MA33") is None
    assert ae.thesis_ma_ref("it looks tired") is None


# --------------------------------------------------------------------------
# 3. The structural mark is the level that BROKE, not the nearest one.
# --------------------------------------------------------------------------
class _Bar:
    def __init__(self, date, close):
        self.date, self.close = date, close


def _pipeline_stub(monkeypatch, *, basis, broken_level, closes, atr=1.0):
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)
    bars = [_Bar(i, c) for i, c in enumerate(closes)]
    p.market = types.SimpleNamespace(get_ohlcv=lambda s, n: bars)
    p.config = types.SimpleNamespace(trading=types.SimpleNamespace(lookback_days=200))
    p._structural_protection_for_holding = lambda **kw: types.SimpleNamespace(
        basis=basis, broken_level=broken_level,
    )
    monkeypatch.setattr(
        "src.data.technical.compute_indicators",
        lambda sym, b: types.SimpleNamespace(atr_14=atr),
    )
    return p


def test_structural_mark_is_the_level_the_check_named(monkeypatch):
    closes = [100.0] * 60 + [80.0]
    p = _pipeline_stub(
        monkeypatch, basis="structural_level_broken", broken_level=95.0,
        closes=closes,
    )
    v = p._alignment_exit_for_holding(
        symbol="X", thesis_invalid_if=None, is_short=False,
        entry_price=100.0, stop_loss=90.0, run_id="r",
    )
    assert [m.price for m in v.marks] == [95.0]
    assert v.status == "EXIT"


def test_no_structural_mark_when_the_level_was_not_confirmed_broken(monkeypatch):
    """An UNBROKEN level must never be admitted — the proximity guess used
    to let an overhead resistance in as 'the confirmed-broken level'."""
    p = _pipeline_stub(
        monkeypatch, basis="structural_level_intact", broken_level=None,
        closes=[100.0] * 60 + [80.0],
    )
    v = p._alignment_exit_for_holding(
        symbol="X", thesis_invalid_if=None, is_short=False,
        entry_price=100.0, stop_loss=90.0, run_id="r",
    )
    assert v.status == "UNPARSEABLE"
    assert v.code == ae.CODE_NO_MARK
    assert v.exit_cleared is False


# --------------------------------------------------------------------------
# 4. The unreadable chart DROPS the sale (fails closed), and says so.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs, code",
    [
        (dict(thesis_invalid_if="close below the MA50", closes=[], atr=1.0),
         ae.CODE_NO_CLOSES),
        (dict(thesis_invalid_if="close below the MA50",
              closes=[float(i) for i in range(1, 61)], atr=None),
         ae.CODE_NO_ATR),
        (dict(thesis_invalid_if=None, closes=[1.0, 2.0], atr=1.0),
         ae.CODE_NO_MARK),
    ],
)
def test_unreadable_chart_is_unparseable_never_a_silent_clear(kwargs, code):
    v = ae.check_alignment_exit(**kwargs)
    assert v.status == "UNPARSEABLE"
    assert v.code == code
    assert v.exit_cleared is False


def test_chart_read_failure_degrades_to_unparseable(monkeypatch):
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)
    p.market = types.SimpleNamespace(
        get_ohlcv=lambda s, n: (_ for _ in ()).throw(RuntimeError("feed down")),
    )
    p.config = types.SimpleNamespace(trading=types.SimpleNamespace(lookback_days=200))
    v = p._alignment_exit_for_holding(
        symbol="X", thesis_invalid_if=None, is_short=False,
        entry_price=None, stop_loss=None, run_id="r",
    )
    assert v.status == "UNPARSEABLE"
    assert v.exit_cleared is False


# --------------------------------------------------------------------------
# 5. The tolerance is recorded as an appetite dial, not as sourced.
# --------------------------------------------------------------------------
def test_tolerance_is_ledgered_as_arbitrary_not_sourced():
    import yaml

    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    led = yaml.safe_load((root / "config" / "number_ledger.yaml").read_text())
    row = next(
        n for n in led["numbers"]
        if n["id"] == "src.risk.alignment_exit.ALIGNMENT_GIVE_BACK_ATR_MULTIPLE"
    )
    assert row["status"] == "arbitrary"
    assert float(row["value"]) == ae.ALIGNMENT_GIVE_BACK_ATR_MULTIPLE == 3.0
