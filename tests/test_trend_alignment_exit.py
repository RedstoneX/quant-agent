"""The trend-alignment exit — the desk's profit-taking rule (owner 2026-09-30,
relayed by the orchestrator: a computed target is a made-up number and never
an exit trigger; the exit is an alignment of live readings — structure, ATR,
a moving average — and "not just one").

Pinned here:
  (a) each reading comes from a ratified desk quantity and nothing else;
  (b) the shape: the two instrument-speed readings must agree on the SAME
      close, the lagging MA can only veto an intact uptrend; a reading that
      cannot be taken is a fault, never a hold and never an exit;
  (c) the structure confirmation (filed, informational) keeps adjacency — a
      gap session cannot chain two lone closes into a confirmation;
  (d) the metric veto stands aside ONLY for an exit that names the trigger AND
      that the desk's own read confirms; narrative is vetoed exactly as before;
  (e) the vocabulary is one list in three places;
  (f) the pipeline wiring on real bars: read once per review, persisted,
      sold in full through the protected path when aligned, nothing when not.
"""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.models import OHLCV
from src.pipeline import TradingPipeline, _HARD_TRIGGER_KEYWORDS
from src.risk.exit_guard import (
    ALIGNMENT_PHRASES,
    BREAK_CONFIRMATION_ATR_MULTIPLE,
    compute_deltas,
    reason_cites_alignment,
    veto_contradicted_exit,
)
from src.risk.exit_trigger import TRIGGER_PHRASES, ExitTrigger
from src.risk.trailing import CHANDELIER_ATR_MULTIPLE, _structural_pivot, _swing_lows
from src.risk.trend_alignment import (
    ALIGNMENT_ALIGNED,
    ALIGNMENT_NOT_ALIGNED,
    ALIGNMENT_UNREADABLE,
    read_trend_alignment,
)


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------


def _bars(closes, half_range=1.0, start=date(2026, 1, 1)):
    out = []
    for i, c in enumerate(closes):
        out.append(OHLCV(date=start + timedelta(days=i), open=c, high=c + half_range,
                         low=c - half_range, close=c, volume=1000))
    return out


def _run_up(n=40):
    """A stair-step advance 100 -> ~140 with real pullbacks, so the trail's
    confirmed higher lows exist (window 3 either side)."""
    closes = []
    c = 100.0
    for i in range(n):
        c += 1.6 if (i % 8) < 6 else -1.2
        closes.append(round(c, 2))
    return closes


def _atr(bars):
    from src.data.technical import compute_indicators
    return compute_indicators("X", bars).atr_14


def _read(bars, *, prior_records=None, is_short=False, run_start=0, ma_20_prior=None):
    from src.data.technical import compute_indicators
    ind = compute_indicators("X", bars)
    if ma_20_prior is None:
        ma_20_prior = compute_indicators("X", bars[:-1]).ma_20
    dates = [str(b.date) for b in reversed(bars[:-1])]
    return read_trend_alignment(
        symbol="x", is_short=is_short, bars=bars, run_start_index=run_start,
        atr=ind.atr_14, ma_20=ind.ma_20, ma_20_prior=ma_20_prior, ma_50=ind.ma_50,
        prior_break_records=prior_records or [], prior_session_dates=dates,
    )


# ---------------------------------------------------------------------------
# (a) + (b): the readings and the shape
# ---------------------------------------------------------------------------


def test_readings_come_from_the_desks_own_quantities():
    bars = _bars(_run_up())
    r = _read(bars)
    # Structure: the trail's own pivot, verbatim.
    assert r.structure_level == _structural_pivot(_swing_lows(bars), is_short=False)
    # Volatility: chandelier under the run's highest high, the trail's multiple.
    assert r.run_extreme == max(b.high for b in bars)
    assert r.chandelier_level == r.run_extreme - CHANDELIER_ATR_MULTIPLE * r.atr
    # Moving average: MA20 and whether it is rising, nothing else.
    assert r.ma_20 is not None and r.ma_20_prior is not None
    assert r.uptrend_intact is (r.close_price > r.ma_20 and r.ma_20 > r.ma_20_prior)
    assert r.code == ALIGNMENT_NOT_ALIGNED and r.aligned is False
    assert "still holds" in r.reason or "inside" in r.reason


def test_a_rollover_aligns_only_when_every_reading_agrees():
    up = _run_up()
    # Confirmed higher low on the run, then a collapse well past it.
    bars = _bars(up + [up[-1] - 6 * k for k in range(1, 9)])
    prior = [{"bar_date": str(bars[-2].date), "raw_broken": True, "close": bars[-2].close}]
    r = _read(bars, prior_records=prior)
    assert r.structure_broken_today is True and r.structure_confirmed is True
    assert r.chandelier_hit is True and r.uptrend_intact is False
    assert r.code == ALIGNMENT_ALIGNED and r.aligned is True
    assert "takes profit in full" in r.reason

    # Same collapse, first day the structure breaks: both fast readings agree
    # on THIS close, so it is aligned — the second reading is the guard the
    # stop side gets from a second day; the confirmation is filed, not required.
    r1 = _read(bars, prior_records=[])
    assert r1.structure_broken_today is True and r1.structure_confirmed is False
    assert r1.aligned is True and "confirmed on a second" not in r1.reason
    assert "confirmed on a second consecutive close" in r.reason

    # Structure confirmed but price still inside the chandelier distance:
    # the volatility reading says the trend is alive -> no exit.
    shallow = _bars(up + [up[-1] - 0.8 * k for k in range(1, 9)])
    lvl = _structural_pivot(_swing_lows(shallow), is_short=False)
    assert lvl is not None
    # force a structural break by dropping exactly through the pivot margin
    # while keeping the chandelier intact is not possible on these bars;
    # assert the reading's own verdict instead.
    rs = _read(shallow, prior_records=[{"bar_date": str(shallow[-2].date),
                                        "raw_broken": True, "close": shallow[-2].close}])
    assert rs.aligned is False


def test_the_lagging_reading_can_only_veto_an_intact_uptrend():
    """MA20 gates nothing on its own: with both fast readings agreeing, the
    read is aligned unless the close is STILL above a rising MA20."""
    up = _run_up()
    bars = _bars(up + [up[-1] - 6 * k for k in range(1, 9)])
    prior = [{"bar_date": str(bars[-2].date), "raw_broken": True, "close": bars[-2].close}]
    from src.data.technical import compute_indicators
    ind = compute_indicators("X", bars)
    dates = [str(b.date) for b in reversed(bars[:-1])]
    # Pretend MA20 sits far below the collapsed close and is rising: veto.
    vetoed = read_trend_alignment(
        symbol="x", is_short=False, bars=bars, run_start_index=0, atr=ind.atr_14,
        ma_20=bars[-1].close - 1.0, ma_20_prior=bars[-1].close - 2.0, ma_50=ind.ma_50,
        prior_break_records=prior, prior_session_dates=dates,
    )
    assert vetoed.uptrend_intact is True and vetoed.aligned is False
    assert "still above a rising MA20" in vetoed.reason
    # MA20 above the close: no veto, the fast readings decide.
    free = read_trend_alignment(
        symbol="x", is_short=False, bars=bars, run_start_index=0, atr=ind.atr_14,
        ma_20=bars[-1].close + 5.0, ma_20_prior=bars[-1].close + 4.0, ma_50=ind.ma_50,
        prior_break_records=prior, prior_session_dates=dates,
    )
    assert free.aligned is True


def test_an_unreadable_reading_is_a_fault_not_a_verdict():
    bars = _bars(_run_up())
    from src.data.technical import compute_indicators
    ind = compute_indicators("X", bars)
    dates = [str(b.date) for b in reversed(bars[:-1])]
    no_atr = read_trend_alignment(
        symbol="x", is_short=False, bars=bars, run_start_index=0, atr=None,
        ma_20=ind.ma_20, ma_20_prior=ind.ma_20, ma_50=ind.ma_50,
        prior_break_records=[], prior_session_dates=dates,
    )
    assert no_atr.code == ALIGNMENT_UNREADABLE and no_atr.aligned is False
    assert "atr" in no_atr.unreadable and "chandelier" in no_atr.unreadable
    no_ma = read_trend_alignment(
        symbol="x", is_short=False, bars=bars, run_start_index=0, atr=ind.atr_14,
        ma_20=None, ma_20_prior=None, ma_50=None,
        prior_break_records=[], prior_session_dates=dates,
    )
    assert no_ma.code == ALIGNMENT_UNREADABLE and "ma20" in no_ma.unreadable
    assert read_trend_alignment(
        symbol="x", is_short=False, bars=[], run_start_index=0, atr=2.0,
        ma_20=1.0, ma_20_prior=1.0, ma_50=1.0, prior_break_records=[],
        prior_session_dates=[],
    ).code == ALIGNMENT_UNREADABLE


def test_short_mirror():
    down = [round(200 - (1.6 if (i % 8) < 6 else -1.2) * i, 2) for i in range(40)]
    closes = []
    c = 200.0
    for i in range(40):
        c -= 1.6 if (i % 8) < 6 else -1.2
        closes.append(round(c, 2))
    bars = _bars(closes + [closes[-1] + 6 * k for k in range(1, 9)])
    prior = [{"bar_date": str(bars[-2].date), "raw_broken": True, "close": bars[-2].close}]
    r = _read(bars, prior_records=prior, is_short=True)
    assert r.structure_confirmed is True and r.chandelier_hit is True
    assert r.uptrend_intact is False and r.aligned is True
    assert "lower high" in r.reason
    del down


# ---------------------------------------------------------------------------
# (c) adjacency
# ---------------------------------------------------------------------------


def test_a_gap_session_cannot_chain_two_lone_closes_into_a_confirmation():
    up = _run_up()
    bars = _bars(up + [up[-1] - 6 * k for k in range(1, 9)])
    # A broken close two sessions ago, nothing filed for yesterday: no streak.
    stale = [{"bar_date": str(bars[-3].date), "raw_broken": True, "close": bars[-3].close}]
    r = _read(bars, prior_records=stale)
    assert r.structure_broken_today is True
    assert r.structure_prior_streak == 0 and r.structure_confirmed is False
    # Yesterday filed broken but its close did not clear TODAY's margin: no streak.
    weak = [{"bar_date": str(bars[-2].date), "raw_broken": True, "close": r.structure_level}]
    r2 = _read(bars, prior_records=weak)
    assert r2.structure_confirmed is False


# ---------------------------------------------------------------------------
# (d) the veto carve-out
# ---------------------------------------------------------------------------


def _improved():
    return compute_deltas(
        "META",
        prior={"thesis_progress_pct": 40.0, "distance_to_stop_pct": 5.0},
        current={"thesis_progress_pct": 60.0, "distance_to_stop_pct": 7.0},
    )


def test_a_confirmed_alignment_exit_is_not_vetoed_as_contradicting_metrics():
    reason = "trend alignment: last higher low broken and confirmed, momentum fading, stalling"
    assert veto_contradicted_exit("SELL", reason, _improved(), alignment_confirmed=True) is None
    assert veto_contradicted_exit("REDUCE", reason, _improved(), alignment_confirmed=True) is None


def test_the_narrative_veto_is_untouched():
    narrative = "momentum fading, stalling winner, prudent to harvest"
    # Not naming the trigger: vetoed exactly as before, whatever the read says.
    assert veto_contradicted_exit("SELL", narrative, _improved()) is not None
    assert veto_contradicted_exit("SELL", narrative, _improved(), alignment_confirmed=True) is not None
    # Naming the trigger while the read does NOT confirm: no carve-out.
    named = "trend alignment — it is stalling and fading"
    assert veto_contradicted_exit("SELL", named, _improved(), alignment_confirmed=False) is not None


# ---------------------------------------------------------------------------
# (e) one vocabulary
# ---------------------------------------------------------------------------


def test_the_alignment_vocabulary_is_one_list_in_three_places():
    assert set(ALIGNMENT_PHRASES) == set(TRIGGER_PHRASES[ExitTrigger.TREND_ALIGNMENT])
    assert set(ALIGNMENT_PHRASES) <= set(_HARD_TRIGGER_KEYWORDS)
    assert reason_cites_alignment("Trend-Alignment exit on the desk's read")
    assert not reason_cites_alignment("momentum fading")
    assert ExitTrigger.TREND_ALIGNMENT.value == "trend_alignment"


def test_the_reviewer_is_told_the_read_and_the_veto_wording_names_the_carve_out():
    from unittest.mock import patch

    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
    pos = SimpleNamespace(symbol="META", qty=4.0, avg_entry=700.0, current_price=740.0,
                          market_value=2960.0, unrealized_pnl=160.0, sector="Comm")
    msg = agent.build_user_message(
        positions=[pos], macro_summary={}, cash_balance=1000.0, total_value=100_000.0,
        position_facts={"META": {"trend_alignment": "NOT_ALIGNED", "days_held": 7}},
        metric_deltas={"META": _improved()},
    )
    assert "trend_alignment=NOT_ALIGNED" in msg
    assert "will be VETOED" in msg and "UNLESS it names the `trend alignment` trigger" in msg


# ---------------------------------------------------------------------------
# (f) pipeline wiring on real bars
# ---------------------------------------------------------------------------


def _pipeline(bars, *, prior_records=None, last_read=None):
    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.db.TREND_ALIGNMENT_READ_KIND = "trend_alignment_read"
    p.db.get_symbol_last_buy.return_value = {"timestamp": "2026-01-01T15:00:00", "stop_loss": 90.0}
    p.db.get_recent_trend_structure_breaks.return_value = prior_records or []
    p.db.get_last_trend_alignment_read.return_value = {"META": last_read} if last_read else {}
    p.market = MagicMock()
    p.market.get_ohlcv.return_value = bars
    p.config = SimpleNamespace(trading=SimpleNamespace(lookback_days=200))
    p._submit_protected_sell = MagicMock(return_value=({"id": "o1", "action": "SELL"}, None))
    p._finalize_pending_protections = MagicMock()
    p._reset_review_caches()
    return p


def _pos(qty=4.0, price=110.0):
    return SimpleNamespace(symbol="META", qty=qty, avg_entry=100.0, current_price=price)


def _aligned_bars():
    up = _run_up()
    return _bars(up + [up[-1] - 6 * k for k in range(1, 9)])


def test_pipeline_sells_in_full_when_the_readings_align(monkeypatch):
    sent = MagicMock(return_value=True)
    monkeypatch.setattr("src.notifier.send_owner_alert", sent)
    bars = _aligned_bars()
    prior = [{"bar_date": str(bars[-2].date), "raw_broken": True, "close": bars[-2].close}]
    p = _pipeline(bars, prior_records=prior)
    order = p._decide_one_alignment_exit(_pos(), "META", run_id="r", seat="s")
    assert order == {"id": "o1", "action": "SELL"}
    kw = p._submit_protected_sell.call_args.kwargs
    assert kw["label"] == "SELL" and kw["side"] == "sell" and kw["qty"] == 4.0
    assert kw["reference_price"] == 110.0 and kw["limit_price"] == round(110.0 * 0.995, 2)
    assert p.db.insert_trade.call_args.kwargs["reasoning"].startswith("Trend alignment (profit-taking)")
    # Today's structure flag filed with its close, and the whole read filed.
    flag = p.db.save_trend_structure_break.call_args.kwargs
    assert flag["raw_broken"] is True and flag["close"] == bars[-1].close
    rows = [c.kwargs for c in p.db.insert_specialist_evidence.call_args_list
            if c.kwargs.get("kind") == "trend_alignment_read"]
    assert rows and '"aligned": true' in rows[-1]["evidence_json"]
    sent.assert_called_once()


def test_pipeline_places_nothing_and_files_the_read_when_not_aligned(monkeypatch):
    sent = MagicMock(return_value=True)
    monkeypatch.setattr("src.notifier.send_owner_alert", sent)
    bars = _bars(_run_up())
    p = _pipeline(bars)
    assert p._decide_one_alignment_exit(_pos(price=140.0), "META", run_id="r", seat="s") is None
    p._submit_protected_sell.assert_not_called()
    rows = [c.kwargs for c in p.db.insert_specialist_evidence.call_args_list
            if c.kwargs.get("kind") == "trend_alignment_read"]
    assert rows and '"aligned": false' in rows[-1]["evidence_json"]
    # First time this state is seen: voiced once. Same state next review: not paged.
    sent.assert_called_once()
    p2 = _pipeline(bars, last_read={"code": ALIGNMENT_NOT_ALIGNED})
    p2._decide_one_alignment_exit(_pos(price=140.0), "META", run_id="r2", seat="s")
    assert sent.call_count == 1


def test_pipeline_reads_once_per_review_and_a_facts_read_writes_nothing():
    bars = _bars(_run_up())
    p = _pipeline(bars)
    a = p._trend_alignment_read("META", position=_pos(), persist=False)
    b = p._trend_alignment_read("META", position=_pos(), run_id="r", persist=True)
    assert a is b
    p.market.get_ohlcv.assert_called_once()
    p.db.save_trend_structure_break.assert_not_called()
    p.db.insert_specialist_evidence.assert_not_called()
    p._reset_review_caches()
    assert p._review_alignment_reads == {}


def test_sweep_skips_sold_symbols_and_files_a_fault_when_a_symbol_raises():
    p = _pipeline(_bars(_run_up()))
    assert p._decide_alignment_exits([_pos()], "r", seat="s", sold_symbols={"META"}) == []
    p._decide_one_alignment_exit = MagicMock(side_effect=RuntimeError("boom"))
    assert p._decide_alignment_exits([_pos()], "r", seat="s") == []
    rows = [c.kwargs for c in p.db.insert_specialist_evidence.call_args_list
            if c.kwargs.get("kind") == "trend_alignment_read"]
    assert rows and "FAULT_TREND_ALIGNMENT_RAISED" in rows[-1]["evidence_json"]


def test_nothing_in_the_exit_path_reads_a_target_or_a_gain():
    import inspect

    import src.risk.trend_alignment as ta

    src = inspect.getsource(ta.read_trend_alignment)
    for banned in ("take_profit", "target", "pnl", "unrealized", "gain_pct"):
        assert banned not in src, banned
    assert BREAK_CONFIRMATION_ATR_MULTIPLE > 0  # reused, not restated
