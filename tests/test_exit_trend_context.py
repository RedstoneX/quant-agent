"""Trend-scaled structural-break confirmation (owner mandate 2026-09-24).

Exit speed scales with how strongly an adverse break aligns with the prevailing
trend, via three discrete, citation-backed regimes (see the module note in
`src/risk/exit_guard.py`):

  REGIME 1 — break AGAINST the trend, a dead/flat tape, or a measured-weak
    trend (ADX<20): exit FAST, on the FIRST confirmed close beyond 1.0 ATR.
  REGIME 2 — break WITH a MODERATE uptrend (ADX 20-25): two consecutive closes;
    a reclaim resets (spring).
  REGIME 3 — break WITH a STRONG uptrend (ADX>=25): two consecutive closes AND
    the prior structural swing-low must ALSO close broken.

The break MARGIN is always NOISE_BAND_ATR_MULTIPLE (1.0 ATR) — the regime never
changes it. These tests also cover the cross-day counting fixes (streak
adjacency, margin consistency) and the owner-facing voicing to both surfaces.

Every level/margin number below is hand-computed against the real formulas in
`src/risk/exit_guard.py`, never guessed. With atr=1.0 and a level at 90 the
long break margin is 1.0, so a long support breaks at a close <= 89.0.
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
    NOISE_BAND_ATR_MULTIPLE,
    TREND_CONFIRMING_CLOSES,
    REGIME_AGAINST_OR_WEAK,
    REGIME_WITH_TREND_MODERATE,
    REGIME_WITH_TREND_STRONG,
    REGIME_INSUFFICIENT_CONTEXT,
    check_structural_protection,
    classify_trend_context,
    render_owner_break_message,
)

MIN_TOUCHES = 5
_LEVELS = [90.0]
_TOUCHES = {90.0: 6}

# A rising 200-MA well below price, with 50>200 -> a genuine uptrend a dip
# through the 90 support sits inside (a shakeout candidate).
_UPTREND = dict(ma_50=100.0, ma_200=80.0, ma_200_prior=79.0)
# 50<200 and price below the 200-MA -> against/dead trend.
_DOWNTREND = dict(ma_50=95.0, ma_200=100.0, ma_200_prior=100.0)

# Consecutive trading sessions, most-recent first, with today = 2026-03-10.
_TODAY = "2026-03-10"
_SESSIONS = ["2026-03-09", "2026-03-06", "2026-03-05", "2026-03-04"]


def _common(**over):
    base = dict(
        thesis_invalid_if=None,
        entry_price=100.0,
        stop_loss=90.3,
        atr=1.0,
        is_short=False,
        computed_levels=list(_LEVELS),
        computed_level_touches=dict(_TOUCHES),
        min_level_touches=MIN_TOUCHES,
        level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
    )
    base.update(over)
    return base


def _rec(bar_date, close, broken=True):
    return {"bar_date": bar_date, "raw_broken": broken, "close": close}


# --------------------------------------------------------------------------
# ADX is computed from the bars (unchanged from the indicator work).
# --------------------------------------------------------------------------


def _trend_bars(n: int, step: float, *, start: float = 50.0) -> list[OHLCV]:
    bars = []
    price = start
    for i in range(n):
        prev = price
        price = max(1.0, price + step)
        bars.append(OHLCV(
            symbol="TST", date=dt.date(2026, 1, 1) + dt.timedelta(days=i),
            open=prev, high=max(prev, price) * 1.01,
            low=min(prev, price) * 0.99, close=price, volume=1000,
        ))
    return bars


def test_adx_is_computed_and_matches_direct_ta():
    import pandas as pd
    import ta

    bars = _trend_bars(60, step=0.5)
    ind = compute_indicators("TST", bars)
    assert ind.adx_14 is not None
    assert ind.di_plus_14 is not None and ind.di_minus_14 is not None
    assert ind.di_plus_14 > ind.di_minus_14
    assert ind.adx_14 >= ADX_STRONG_TREND_THRESHOLD
    df = pd.DataFrame([b.model_dump() for b in bars]).set_index("date").sort_index()
    direct = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=ADX_PERIOD)
    assert ind.adx_14 == round(float(direct.adx().iloc[-1]), 2)


def test_adx_none_when_too_few_bars():
    ind = compute_indicators("TST", _trend_bars(2 * ADX_PERIOD - 1, step=0.5))
    assert ind.adx_14 is None


def test_ma_200_slope_is_exposed():
    """ma_200_prior lands so the exit guard can read the 200-MA slope."""
    ind = compute_indicators("TST", _trend_bars(220, step=0.3))
    assert ind.ma_200 is not None and ind.ma_200_prior is not None
    assert ind.ma_200 > ind.ma_200_prior  # a rising 200-MA in an uptrend


# --------------------------------------------------------------------------
# classify_trend_context -> the sourced regimes.
# --------------------------------------------------------------------------


def test_classify_regimes_long():
    # Against/dead trend -> fast.
    assert classify_trend_context(
        is_short=False, current_price=88.5, adx=30.0, **_DOWNTREND,
    ) == REGIME_AGAINST_OR_WEAK
    # With uptrend, moderate strength.
    assert classify_trend_context(
        is_short=False, current_price=88.5, adx=22.0, **_UPTREND,
    ) == REGIME_WITH_TREND_MODERATE
    # With uptrend, strong.
    assert classify_trend_context(
        is_short=False, current_price=88.5, adx=30.0, **_UPTREND,
    ) == REGIME_WITH_TREND_STRONG
    # With uptrend structure but measured-weak ADX -> fast, per spec regime 1.
    assert classify_trend_context(
        is_short=False, current_price=88.5, adx=15.0, **_UPTREND,
    ) == REGIME_AGAINST_OR_WEAK
    # No MA data -> insufficient context (preserves the two-close default).
    assert classify_trend_context(
        is_short=False, current_price=88.5, ma_50=None, ma_200=None, adx=30.0,
    ) == REGIME_INSUFFICIENT_CONTEXT
    # Flat 200-MA (no slope) is not "with the trend".
    assert classify_trend_context(
        is_short=False, current_price=88.5, adx=30.0,
        ma_50=100.0, ma_200=80.0, ma_200_prior=80.0,
    ) == REGIME_AGAINST_OR_WEAK


def test_classify_short_mirrors_long():
    # A short is with-trend in a strong DOWNtrend: price below a FALLING 200MA
    # (well above the popped resistance) and 50<200. Adverse break = a pop to a
    # local resistance at 112 that is still far below the 130 200-MA.
    assert classify_trend_context(
        is_short=True, current_price=112.0, adx=30.0,
        ma_50=110.0, ma_200=130.0, ma_200_prior=131.0,
    ) == REGIME_WITH_TREND_STRONG
    # Rising 200MA / 50>200 against the short -> fast.
    assert classify_trend_context(
        is_short=True, current_price=112.0, adx=30.0,
        ma_50=140.0, ma_200=130.0, ma_200_prior=129.0,
    ) == REGIME_AGAINST_OR_WEAK


# --------------------------------------------------------------------------
# (a) Against-trend / weak break exits on ONE confirmed close.
# --------------------------------------------------------------------------


def test_against_trend_break_exits_on_one_close():
    r = check_structural_protection(
        **_common(current_price=88.5, adx=30.0, **_DOWNTREND),
        # No prior records at all: regime 1 needs only today's close.
    )
    assert r.trend_context == REGIME_AGAINST_OR_WEAK
    assert r.confirming_closes_needed == 1
    assert r.protected is False
    assert r.basis == "structural_level_broken"


def test_weak_trend_break_exits_on_one_close():
    r = check_structural_protection(
        **_common(current_price=88.5, adx=15.0, **_UPTREND),
    )
    assert r.trend_context == REGIME_AGAINST_OR_WEAK
    assert r.protected is False


# --------------------------------------------------------------------------
# (b) Moderate with-trend needs TWO consecutive closes; a reclaim resets.
# --------------------------------------------------------------------------


def test_moderate_with_trend_needs_two_consecutive_closes():
    kw = dict(current_price=88.5, adx=22.0, **_UPTREND)
    # One prior adjacent broken close -> today makes two -> confirmed.
    confirmed = check_structural_protection(
        **_common(**kw),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    assert confirmed.trend_context == REGIME_WITH_TREND_MODERATE
    assert confirmed.confirming_closes_needed == TREND_CONFIRMING_CLOSES == 2
    assert confirmed.protected is False
    assert confirmed.basis == "structural_level_broken"
    # No prior broken close -> only today -> still pending.
    pending = check_structural_protection(
        **_common(**kw),
        prior_break_records=[],
        prior_session_dates=_SESSIONS,
    )
    assert pending.protected is True
    assert pending.basis == "structural_level_pending_confirmation"
    assert (pending.confirming_closes_seen, pending.confirming_closes_needed) == (1, 2)


def test_moderate_with_trend_reclaim_resets():
    # Yesterday RECLAIMED (raw_broken False) -> the streak resets, so today's
    # break is a fresh first close -> pending, not confirmed (Wyckoff spring).
    r = check_structural_protection(
        **_common(current_price=88.5, adx=22.0, **_UPTREND),
        prior_break_records=[_rec("2026-03-09", 91.0, broken=False)],
        prior_session_dates=_SESSIONS,
    )
    assert r.protected is True
    assert r.basis == "structural_level_pending_confirmation"


# --------------------------------------------------------------------------
# (c) Strong with-trend holds until the PRIOR swing-low also breaks.
# --------------------------------------------------------------------------


def test_strong_with_trend_holds_until_prior_low_breaks():
    # Levels: a 90 support backing the stop and an 85 prior swing higher-low.
    levels = [85.0, 90.0]
    touches = {90.0: 6, 85.0: 6}
    two_closes_only = check_structural_protection(
        **_common(current_price=88.5, adx=30.0,
                  computed_levels=levels, computed_level_touches=touches,
                  **_UPTREND),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    # Two closes confirmed, BUT price (88.5) has not broken the 85 prior low by
    # a margin (needs <= 84.0), so protection is still held.
    assert two_closes_only.trend_context == REGIME_WITH_TREND_STRONG
    assert two_closes_only.protected is True
    assert two_closes_only.basis == "structural_level_pending_confirmation"

    prior_low_broken = check_structural_protection(
        **_common(current_price=83.5, adx=30.0,
                  computed_levels=levels, computed_level_touches=touches,
                  **_UPTREND),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    # Now price (83.5) has closed below the 85 prior low by the margin -> the
    # strong trend's structure has failed -> protection lifts.
    assert prior_low_broken.protected is False
    assert prior_low_broken.basis == "structural_level_broken"


def test_strong_with_trend_falls_back_to_two_closes_without_prior_low():
    # No structural level below the broken 90 -> the prior-low gate is vacuous,
    # so regime 3 behaves like regime 2 (two consecutive closes).
    r = check_structural_protection(
        **_common(current_price=88.5, adx=30.0, **_UPTREND),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    assert r.trend_context == REGIME_WITH_TREND_STRONG
    assert r.protected is False  # two closes, no prior low to wait on


# --------------------------------------------------------------------------
# (d) A gap in the sessions resets the streak.
# --------------------------------------------------------------------------


def test_gap_resets_the_streak():
    # A broken close two sessions ago (2026-03-06) but NOTHING on the
    # immediately-preceding session (2026-03-09) -> not consecutive -> the
    # streak resets, so today is only a first close -> pending.
    r = check_structural_protection(
        **_common(current_price=88.5, adx=22.0, **_UPTREND),
        prior_break_records=[_rec("2026-03-06", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    assert r.protected is True
    assert r.basis == "structural_level_pending_confirmation"
    assert r.confirming_closes_seen == 1


# --------------------------------------------------------------------------
# (e) Margin conflation cannot confirm early.
# --------------------------------------------------------------------------


def test_margin_conflation_cannot_confirm_early():
    # A prior row FLAGGED broken but whose close (89.2) did NOT clear today's
    # 1.0-ATR margin (needs <= 89.0). It must not count toward confirmation.
    stale = check_structural_protection(
        **_common(current_price=88.5),  # no MAs -> regime 2 (two closes)
        prior_break_records=[_rec("2026-03-09", 89.2)],
        prior_session_dates=_SESSIONS,
    )
    assert stale.trend_context == REGIME_INSUFFICIENT_CONTEXT
    assert stale.protected is True  # the stale close didn't clear -> still pending
    # Control: a prior close that DID clear the margin confirms.
    good = check_structural_protection(
        **_common(current_price=88.5),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    assert good.protected is False


# --------------------------------------------------------------------------
# (g) Trend context changes the REGIME but the margin stays 1.0 ATR.
# --------------------------------------------------------------------------


def test_margin_is_always_one_atr_across_regimes():
    assert NOISE_BAND_ATR_MULTIPLE == 1.0
    # 88.7 is beyond the 1.0-ATR margin (<= 89.0) -> raw_broken in EVERY regime.
    for kw in (dict(adx=30.0, **_DOWNTREND), dict(adx=22.0, **_UPTREND),
               dict(adx=30.0, **_UPTREND)):
        r = check_structural_protection(**_common(current_price=88.7, **kw))
        assert r.raw_broken is True
    # 89.5 is WITHIN 1.0 ATR of the 90 level -> NOT broken in any regime (a
    # wider 1.5-ATR margin would have been needed to change this).
    for kw in (dict(adx=30.0, **_DOWNTREND), dict(adx=30.0, **_UPTREND)):
        r = check_structural_protection(**_common(current_price=89.5, **kw))
        assert r.raw_broken is False
        assert r.basis == "structural_level_intact"


# --------------------------------------------------------------------------
# Voicing the why to BOTH surfaces, truthful wording.
# --------------------------------------------------------------------------


def test_owner_message_wording_exit_and_hold():
    exit_check = check_structural_protection(
        **_common(current_price=88.5, adx=30.0, **_DOWNTREND),
    )
    msg = render_owner_break_message("NVDA", exit_check)
    assert msg and "NVDA" in msg
    assert "clearing to exit" in msg.lower()
    assert "real breakdown" in msg.lower()
    assert "90 support" in msg

    hold_check = check_structural_protection(
        **_common(current_price=88.5, adx=30.0,
                  computed_levels=[85.0, 90.0],
                  computed_level_touches={90.0: 6, 85.0: 6}, **_UPTREND),
        prior_break_records=[_rec("2026-03-09", 88.5)],
        prior_session_dates=_SESSIONS,
    )
    hmsg = render_owner_break_message("AAPL", hold_check)
    assert hmsg and "AAPL" in hmsg
    assert "holding" in hmsg.lower()
    assert "shakeout" in hmsg.lower()
    assert "uptrend" in hmsg.lower()
    # Truthful: never claims a sale was placed.
    assert "sold" not in hmsg.lower()


def test_no_owner_message_on_intact_basis():
    intact = check_structural_protection(
        **_common(current_price=95.0, adx=30.0, **_UPTREND),
    )
    assert intact.owner_reason == ""
    assert render_owner_break_message("NVDA", intact) is None


def _voicing_pipeline():
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    return p


def _board_rows(pipeline):
    rows = []
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        kw = call.kwargs
        if kw.get("kind") != "structural_break_trend_context":
            continue
        rows.append((kw.get("symbol"), json.loads(kw["evidence_json"])))
    return rows


def _exit_check():
    return check_structural_protection(
        **_common(current_price=88.5, adx=30.0, **_DOWNTREND),
    )


def test_voicing_reaches_telegram_and_board():
    p = _voicing_pipeline()
    with patch("src.notifier.send_owner_alert") as alert:
        p._voice_structural_protection_break(
            symbol="NVDA", run_id="run-1", check=_exit_check(),
        )
    assert alert.call_count == 1
    telegram_text = alert.call_args.args[0]
    assert "NVDA" in telegram_text and "real breakdown" in telegram_text.lower()
    rows = _board_rows(p)
    assert len(rows) == 1
    sym, payload = rows[0]
    assert sym == "NVDA"
    assert payload["owner_reason"] == telegram_text  # SAME sentence on both
    assert payload["protected"] is False
    assert payload["trend_context"] == REGIME_AGAINST_OR_WEAK


def test_voicing_deduplicated_within_a_run():
    p = _voicing_pipeline()
    check = _exit_check()
    with patch("src.notifier.send_owner_alert") as alert:
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=check)
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=check)
    assert alert.call_count == 1
    assert len(_board_rows(p)) == 1


def test_voicing_survives_a_telegram_failure_and_keeps_board():
    p = _voicing_pipeline()
    with patch("src.notifier.send_owner_alert", side_effect=RuntimeError("down")):
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=_exit_check())
    # The board row still landed, so the why reached a surface -> dedup consumed.
    assert len(_board_rows(p)) == 1
    assert ("run-1", "NVDA", "structural_level_broken") in p._voiced_structural_breaks


# --------------------------------------------------------------------------
# (f) If BOTH surface writes fail, the dedup slot is NOT consumed (retry).
# --------------------------------------------------------------------------


def test_both_surface_failures_do_not_consume_dedup_slot():
    p = _voicing_pipeline()
    p.db.insert_specialist_evidence.side_effect = RuntimeError("board down")
    check = _exit_check()
    with patch("src.notifier.send_owner_alert", side_effect=RuntimeError("tg down")):
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=check)
    # Neither surface succeeded -> the desk must retry, so the slot stays free.
    assert getattr(p, "_voiced_structural_breaks", set()) == set()

    # A later cycle whose board write now succeeds voices it (not suppressed) —
    # the retry happened (a second board attempt) and the slot is now consumed.
    p.db.insert_specialist_evidence.side_effect = None
    with patch("src.notifier.send_owner_alert", side_effect=RuntimeError("tg down")):
        p._voice_structural_protection_break(symbol="NVDA", run_id="run-1", check=check)
    assert p.db.insert_specialist_evidence.call_count == 2  # first failed, retry ran
    assert ("run-1", "NVDA", "structural_level_broken") in p._voiced_structural_breaks
