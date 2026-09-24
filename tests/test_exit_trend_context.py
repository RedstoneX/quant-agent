"""Item 70 — TREND-CONTEXT-aware structural break confirmation.

Covers the three things item 70 adds on top of the existing
`check_structural_protection` confirmation gate (whose own tests live in
`tests/test_structural_protection.py` and stay green unchanged):

  1. ADX (+DI/-DI) is computed from the OHLCV bars and lands on
     `TechnicalIndicators`.
  2. The break confirmation is trend-context-aware: a with-trend breakdown
     exits on the standard 2 closes / 1.0x ATR; a break AGAINST a strong trend
     is held until 3 closes / 1.5x ATR; a weak/rangebound tape (or missing
     ADX) uses the standard settings.
  3. The DECISIVE outcomes (a confirmed break that clears the desk to exit, and
     a break held pending confirmation) emit a plain-language reason that
     reaches BOTH owner surfaces — the Telegram alert and the board journal —
     via the existing durable-reason trail.

Every level/margin number below is hand-computed against the real formulas in
`src/risk/exit_guard.py`, never guessed.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import MagicMock, patch

from src.data.levels import CLUSTER_TOLERANCE_PCT
from src.data.technical import ADX_PERIOD, compute_indicators
from src.models import OHLCV
from src.risk.exit_guard import (
    ADX_STRONG_TREND_THRESHOLD,
    COUNTER_TREND_BREAK_ATR_MULTIPLE,
    NOISE_BAND_ATR_MULTIPLE,
    check_structural_protection,
    classify_trend_context,
    render_owner_break_message,
)

# Shared, already-ratified level/touch bars (docs/RESEARCH_FINDINGS.md §7).
MIN_TOUCHES = 5
# A level at 90, touched enough to qualify, with the stop sitting inside its
# own CLUSTER_TOLERANCE_PCT zone (90 * 1% = 0.9, so a 90.3 stop is 0.3 away).
_LEVELS = [90.0]
_TOUCHES = {90.0: 6}


def _common(**over):
    base = dict(
        thesis_invalid_if=None,
        entry_price=100.0,
        stop_loss=90.3,
        atr=1.0,
        is_short=False,
        computed_levels=_LEVELS,
        computed_level_touches=_TOUCHES,
        min_level_touches=MIN_TOUCHES,
        level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
    )
    base.update(over)
    return base


# --------------------------------------------------------------------------
# 1. ADX is computed from the bars.
# --------------------------------------------------------------------------


def _trend_bars(n: int, step: float, *, start: float = 50.0) -> list[OHLCV]:
    """`n` daily bars trending by `step` per bar (step<0 for a downtrend)."""
    bars = []
    price = start
    for i in range(n):
        prev = price
        price = max(1.0, price + step)
        hi = max(prev, price) * 1.01
        lo = min(prev, price) * 0.99
        bars.append(OHLCV(
            symbol="TST", date=dt.date(2026, 1, 1) + dt.timedelta(days=i),
            open=prev, high=hi, low=lo, close=price, volume=1000,
        ))
    return bars


def test_adx_is_computed_and_matches_direct_ta():
    """ADX/+DI/-DI land on the model and equal a direct `ta` computation over
    the same OHLC — proving the block feeds the indicator the right columns."""
    import pandas as pd
    import ta

    bars = _trend_bars(60, step=0.5)  # steady uptrend
    ind = compute_indicators("TST", bars)
    assert ind.adx_14 is not None
    assert ind.di_plus_14 is not None and ind.di_minus_14 is not None
    # A clean uptrend: +DI dominates -DI, and ADX reads a strong trend.
    assert ind.di_plus_14 > ind.di_minus_14
    assert ind.adx_14 >= ADX_STRONG_TREND_THRESHOLD

    df = pd.DataFrame([b.model_dump() for b in bars]).set_index("date").sort_index()
    direct = ta.trend.ADXIndicator(
        df["high"], df["low"], df["close"], window=ADX_PERIOD,
    )
    assert ind.adx_14 == round(float(direct.adx().iloc[-1]), 2)
    assert ind.di_plus_14 == round(float(direct.adx_pos().iloc[-1]), 2)
    assert ind.di_minus_14 == round(float(direct.adx_neg().iloc[-1]), 2)


def test_adx_none_when_too_few_bars():
    ind = compute_indicators("TST", _trend_bars(2 * ADX_PERIOD - 1, step=0.5))
    assert ind.adx_14 is None


# --------------------------------------------------------------------------
# 2a. WITH-trend breakdown: exits at 2 closes / 1.0x ATR (standard).
# --------------------------------------------------------------------------


def test_with_trend_breakdown_exits_at_two_closes():
    """Long, strong DOWNtrend regime (ma_50<ma_200) with price below ma_200 and
    strong ADX. Close 88.5 is exactly one ATR below the 90 level (1.0x margin).
    With one prior confirming close, the second close lifts protection."""
    r = check_structural_protection(
        **_common(current_price=88.5, ma_50=95.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=1),
    )
    assert r.trend_context == "with_trend"
    assert r.confirming_closes_needed == 2
    assert r.protected is False
    assert r.basis == "structural_level_broken"


def test_with_trend_single_close_still_pending():
    r = check_structural_protection(
        **_common(current_price=88.5, ma_50=95.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=0),
    )
    assert r.trend_context == "with_trend"
    assert r.protected is True
    assert r.basis == "structural_level_pending_confirmation"
    assert (r.confirming_closes_seen, r.confirming_closes_needed) == (1, 2)


def test_with_trend_uses_one_atr_margin():
    """At the 1.0x margin a close of 88.9 breaks (<= 90-1.0=89.0); 89.5 does
    not, so it stays intact even with a long prior streak."""
    broke = check_structural_protection(
        **_common(current_price=88.9, ma_50=95.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=1),
    )
    assert broke.protected is False
    intact = check_structural_protection(
        **_common(current_price=89.5, ma_50=95.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=5),
    )
    assert intact.protected is True
    assert intact.basis == "structural_level_intact"


# --------------------------------------------------------------------------
# 2b. COUNTER-trend break: held until 3 closes / 1.5x ATR.
# --------------------------------------------------------------------------


def test_counter_trend_break_held_until_third_close():
    """Long, strong UPtrend regime (ma_50>ma_200) and strong ADX. Close 88.4 is
    beyond the wider 1.5x margin (90-1.5=88.5). Two prior confirming closes are
    NOT enough — a counter-trend break needs a THIRD close."""
    two_closes = check_structural_protection(
        **_common(current_price=88.4, ma_50=110.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=1),  # today + 1 prior = 2 closes
    )
    assert two_closes.trend_context == "counter_trend"
    assert two_closes.confirming_closes_needed == 3
    assert two_closes.protected is True
    assert two_closes.basis == "structural_level_pending_confirmation"
    assert two_closes.confirming_closes_seen == 2

    three_closes = check_structural_protection(
        **_common(current_price=88.4, ma_50=110.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=2),  # today + 2 prior = 3 closes
    )
    assert three_closes.protected is False
    assert three_closes.basis == "structural_level_broken"
    assert three_closes.confirming_closes_seen == 3


def test_counter_trend_requires_wider_15_atr_margin():
    """A close at 88.8 is beyond the standard 1.0x margin (89.0) but NOT the
    counter-trend 1.5x margin (88.5). The SAME close breaks a with-trend read
    but is held intact under the wider counter-trend margin."""
    counter = check_structural_protection(
        **_common(current_price=88.8, ma_50=110.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=5),
    )
    assert counter.trend_context == "counter_trend"
    assert counter.protected is True
    assert counter.basis == "structural_level_intact"

    with_trend = check_structural_protection(
        **_common(current_price=88.8, ma_50=95.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=5),
    )
    assert with_trend.trend_context == "with_trend"
    assert with_trend.protected is False  # same close, narrower margin breaks it
    assert COUNTER_TREND_BREAK_ATR_MULTIPLE > NOISE_BAND_ATR_MULTIPLE


# --------------------------------------------------------------------------
# 2c. Weak / rangebound and missing ADX both use the standard settings.
# --------------------------------------------------------------------------


def test_weak_trend_uses_standard_settings():
    """ADX below the strong threshold: no trend to be with or against, so the
    standard 2-close / 1.0x settings apply even in an up-regime."""
    r = check_structural_protection(
        **_common(current_price=88.5, ma_50=110.0, ma_200=100.0, adx=15.0,
                  prior_break_streak=1),
    )
    assert r.trend_context == "weak"
    assert r.confirming_closes_needed == 2
    assert r.protected is False  # 2 closes at 1.0x margin


def test_missing_adx_is_unchanged_from_pre_item_70():
    """No ADX at all -> 'unknown' -> standard settings, byte-for-byte the old
    two-close / 1.0x behaviour that every pre-item-70 caller relied on."""
    r = check_structural_protection(
        **_common(current_price=88.5, ma_50=110.0, ma_200=100.0,
                  break_seen_prior_close=True),
    )
    assert r.trend_context == "unknown"
    assert r.confirming_closes_needed == 2
    assert r.protected is False


def test_classify_trend_context_short_side_mirrors_long():
    # Short adverse break is UP through resistance. Down-regime = counter.
    assert classify_trend_context(
        is_short=True, current_price=112.0, ma_50=90.0, ma_200=100.0, adx=30.0,
    ) == "counter_trend"
    # Up-regime with price above ma_200 = with-trend (real) for a short.
    assert classify_trend_context(
        is_short=True, current_price=112.0, ma_50=110.0, ma_200=100.0, adx=30.0,
    ) == "with_trend"


# --------------------------------------------------------------------------
# 3. The plain-language reason reaches BOTH owner surfaces.
# --------------------------------------------------------------------------


def test_owner_message_present_for_exit_and_hold():
    exit_check = check_structural_protection(
        **_common(current_price=88.5, ma_50=95.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=1),
    )
    exit_msg = render_owner_break_message("NVDA", exit_check)
    assert exit_msg and "NVDA" in exit_msg
    assert "real breakdown" in exit_msg.lower()
    assert "90 support" in exit_msg  # the level that broke, in plain words

    hold_check = check_structural_protection(
        **_common(current_price=88.4, ma_50=110.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=1),
    )
    hold_msg = render_owner_break_message("AAPL", hold_check)
    assert hold_msg and "AAPL" in hold_msg
    assert "shakeout" in hold_msg.lower()
    assert "uptrend" in hold_msg.lower()
    assert "3rd confirming close" in hold_msg  # confirmation state, in words


def test_no_owner_message_on_non_decisive_bases():
    intact = check_structural_protection(
        **_common(current_price=95.0, ma_50=95.0, ma_200=100.0, adx=30.0),
    )
    assert intact.owner_reason == ""
    assert render_owner_break_message("NVDA", intact) is None


def _voicing_pipeline():
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.market = MagicMock()
    p.risk_engine = MagicMock()
    p.risk_engine.config.min_level_touches_for_stop_honor = MIN_TOUCHES
    return p


def _board_rows(pipeline):
    rows = []
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        kw = call.kwargs
        if kw.get("kind") != "structural_break_trend_context":
            continue
        rows.append((kw.get("symbol"), json.loads(kw["evidence_json"])))
    return rows


def _run_voicing(pipeline, symbol, check):
    with patch("src.notifier.send_owner_alert") as alert:
        pipeline._voice_structural_protection_break(
            symbol=symbol, run_id="run-1", check=check,
        )
    return alert


def test_voicing_reaches_telegram_and_board_for_an_exit():
    p = _voicing_pipeline()
    exit_check = check_structural_protection(
        **_common(current_price=88.5, ma_50=95.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=1),
    )
    alert = _run_voicing(p, "NVDA", exit_check)

    # Telegram
    assert alert.call_count == 1
    telegram_text = alert.call_args.args[0]
    assert "NVDA" in telegram_text and "real breakdown" in telegram_text.lower()

    # Board journal (specialist_evidence)
    rows = _board_rows(p)
    assert len(rows) == 1
    symbol, payload = rows[0]
    assert symbol == "NVDA"
    assert payload["owner_reason"] == telegram_text  # SAME sentence on both
    assert payload["protected"] is False
    assert payload["trend_context"] == "with_trend"


def test_voicing_reaches_telegram_and_board_for_a_hold():
    p = _voicing_pipeline()
    hold_check = check_structural_protection(
        **_common(current_price=88.4, ma_50=110.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=1),
    )
    alert = _run_voicing(p, "AAPL", hold_check)

    assert alert.call_count == 1
    telegram_text = alert.call_args.args[0]
    assert "AAPL" in telegram_text and "shakeout" in telegram_text.lower()

    rows = _board_rows(p)
    assert len(rows) == 1
    symbol, payload = rows[0]
    assert symbol == "AAPL"
    assert payload["owner_reason"] == telegram_text
    assert payload["protected"] is True
    assert payload["trend_context"] == "counter_trend"


def test_voicing_deduplicated_within_a_run():
    p = _voicing_pipeline()
    exit_check = check_structural_protection(
        **_common(current_price=88.5, ma_50=95.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=1),
    )
    with patch("src.notifier.send_owner_alert") as alert:
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=exit_check)
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=exit_check)
    assert alert.call_count == 1  # second call suppressed
    assert len(_board_rows(p)) == 1


def test_voicing_survives_a_telegram_failure():
    p = _voicing_pipeline()
    exit_check = check_structural_protection(
        **_common(current_price=88.5, ma_50=95.0, ma_200=100.0, adx=30.0,
                  prior_break_streak=1),
    )
    with patch("src.notifier.send_owner_alert", side_effect=RuntimeError("down")):
        # Must not raise — a voicing failure never affects the verdict.
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=exit_check)
    # The board row was still written before the Telegram attempt.
    assert len(_board_rows(p)) == 1
