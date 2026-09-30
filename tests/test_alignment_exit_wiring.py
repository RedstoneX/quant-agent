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


# --------------------------------------------------------------------------
# 6. THE SCAN — the alignment exit can now INITIATE a sale.
#
# Until this landed, `check_alignment_exit` ran only on positions the review
# had already named, and only GATED the ones whose prose already claimed the
# alignment exit. Nothing ever asked the question of a position the models
# were silent about, so the owner-ratified rule could never start a sale.
# --------------------------------------------------------------------------
def _scan_pipeline(verdicts: dict):
    """A pipeline whose only real behaviour is the scan; the chart read is
    replaced by a per-symbol canned verdict."""
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)
    seen = []

    def _chart(*, symbol, **kw):
        seen.append(symbol)
        return verdicts[symbol]

    p._alignment_exit_for_holding = _chart
    p._seen = seen
    return p


def _verdict(status):
    return ae.AlignmentExitCheck(
        status, "c", (), None, None, None, "detail",
        owner_reason="Trend alignment over: the last line ...",
    )


def _pos(symbol, qty=10.0):
    from src.models import Position

    return Position(
        symbol=symbol, qty=qty, avg_entry=100.0, current_price=90.0,
        market_value=900.0, unrealized_pnl=-100.0, sector="Tech",
    )


_PRIORITY = {"SELL": 0, "COVER": 0, "REDUCE": 1, "TRAIL_STOP": 2, "HOLD": 3}


def test_scan_raises_a_sale_on_a_position_no_model_mentioned():
    """THE POINT OF THE WHOLE BUILD."""
    p = _scan_pipeline({"AAA": _verdict("EXIT")})
    best: dict = {}
    p._alignment_exit_scan(
        [_pos("AAA")], best, run_id="r", position_facts=None,
        priority=_PRIORITY,
    )
    assert best["AAA"]["action"] == "SELL"
    assert best["AAA"]["exit_trigger"] == ExitTrigger.TREND_ALIGNMENT_OVER.value
    # The reason names the trigger in the accepted wording, so the confirmer
    # downstream recognises the claim and gates the sale on the verdict.
    from src.pipeline import _reason_claims_alignment_exit

    assert _reason_claims_alignment_exit(best["AAA"]["reason"], None)


def test_scan_raises_nothing_on_hold_or_unreadable_chart():
    for status in ("HOLD", "UNPARSEABLE"):
        p = _scan_pipeline({"AAA": _verdict(status)})
        best: dict = {}
        p._alignment_exit_scan(
            [_pos("AAA")], best, run_id="r", position_facts=None,
            priority=_PRIORITY,
        )
        assert best == {}, status


def test_scan_failure_holds_and_does_not_stop_the_other_names():
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)

    def _chart(*, symbol, **kw):
        if symbol == "BOOM":
            raise RuntimeError("no bars")
        return _verdict("EXIT")

    p._alignment_exit_for_holding = _chart
    best: dict = {}
    p._alignment_exit_scan(
        [_pos("BOOM"), _pos("AAA")], best, run_id="r", position_facts=None,
        priority=_PRIORITY,
    )
    assert "BOOM" not in best
    assert best["AAA"]["action"] == "SELL"


def test_scan_covers_a_short_and_never_sells_it():
    p = _scan_pipeline({"SHT": _verdict("EXIT")})
    best: dict = {}
    p._alignment_exit_scan(
        [_pos("SHT", qty=-10.0)], best, run_id="r", position_facts=None,
        priority=_PRIORITY,
    )
    assert best["SHT"]["action"] == "COVER"


def test_scan_never_overwrites_an_exit_the_review_asked_for():
    p = _scan_pipeline({"AAA": _verdict("EXIT")})
    mine = {"symbol": "AAA", "action": "SELL", "reason": "earnings miss"}
    best = {"AAA": mine}
    p._alignment_exit_scan(
        [_pos("AAA")], best, run_id="r", position_facts=None,
        priority=_PRIORITY,
    )
    assert best["AAA"] is mine
    assert p._seen == []  # not even read: the model's exit already stands


def test_scan_supersedes_hold_because_the_chart_decides_not_the_prose():
    p = _scan_pipeline({"AAA": _verdict("EXIT")})
    best = {"AAA": {"symbol": "AAA", "action": "HOLD", "reason": "still like it"}}
    p._alignment_exit_scan(
        [_pos("AAA")], best, run_id="r", position_facts=None,
        priority=_PRIORITY,
    )
    assert best["AAA"]["action"] == "SELL"


def test_verdict_is_read_once_per_position_per_run():
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)
    calls = []

    def _chart(*, symbol, **kw):
        calls.append(symbol)
        return _verdict("EXIT")

    p._alignment_exit_for_holding = _chart
    kw = dict(
        symbol="AAA", thesis_invalid_if=None, is_short=False,
        entry_price=1.0, stop_loss=None, run_id="r",
    )
    assert p._alignment_exit_cached(**kw).status == "EXIT"
    assert p._alignment_exit_cached(**kw).status == "EXIT"
    assert calls == ["AAA"]


def test_scanned_sale_reaches_the_real_sell_path_with_the_reason_voiced():
    """End to end through `_midday_execute_llm_actions`: a review that names
    NOTHING, a chart that says the move is over, and the desk's ordinary
    protected-sell path carrying the chart's own owner-facing sentence."""
    from unittest.mock import MagicMock

    from src.models import PositionReasoningChain, PositionReview
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.db = MagicMock()
    p.db.get_trades.return_value = []
    p.db.get_acted_exit_triggers_today.return_value = []
    p._format_qty = lambda q: str(q)
    p._atr_for_symbol = lambda s: 2.0
    p._record_exit_refusal = lambda **kw: None
    p._alignment_exit_for_holding = lambda **kw: _verdict("EXIT")
    p._submit_protected_sell = lambda **kw: (
        {"id": "o-1", "symbol": kw["symbol"], "status": "accepted"}, None,
    )

    review = PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="stable",
            thesis_progress_check="on pace",
            thesis_integrity_check="intact",
            winners_discipline_check="no flags",
            session_disposition_check="patient",
            execution_rationale="n/a",
        ),
        actions=[],
        overall_assessment="nothing to do", risk_level="low",
    )
    orders = p._midday_execute_llm_actions(
        positions=[_pos("AAA")], review=review, run_id="r-scan",
    )
    assert orders, "the chart said the move was over and nothing was sold"
    # The sale states WHY, in the desk's existing owner-facing wording: the
    # scan's own line plus the chart's own sentence appended by the confirmer.
    reason = (p.db.insert_trade.call_args.kwargs.get("reasoning") or "")
    assert "Trend alignment over" in reason
    assert "the last line" in reason
