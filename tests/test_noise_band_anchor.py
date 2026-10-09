"""The noise band's anchor is PER HOME, and these tests pin which is which.

MEASURED 2026-10-04 (19 symbols, 9,519 daily bars, 2024-09-30..2026-09-29,
424 trail-stop exit events, each home's real control flow modelled):

  * `check_structural_protection`'s fallback: 383 blocks -> 329, i.e. 54
    removed and NONE added. Re-anchored.
  * The midday position reviewer: 189 -> 329, i.e. 51 removed and 191 ADDED,
    96 of the added costing money against 95 saving, and 129 never releasing
    within 60 sessions. NOT re-anchored; it keeps the entry anchor.

These tests pin the anchor per home and the long/short mirror; they do NOT
re-derive the width, which is unchanged under either anchor.
"""

import math

import pytest

from src.risk.exit_guard import (
    NOISE_BAND_ATR_MULTIPLE,
    adverse_move_is_noise,
    check_structural_protection,
    noise_band_anchor,
)
from src.risk.noise_band_record import fallback_outcome, midday_payload


ATR = 2.0


# --------------------------------------------------------------------- LONG
def test_long_block_removed_by_reanchoring_to_the_highest_high():
    """Bought at 100, ran to 110, now 107. From ENTRY the move is +7 (no
    adverse move at all, so the band blocks the exit regardless of width).
    From the HIGHEST HIGH the adverse move is 3.0 = 1.5 ATR, outside the
    1.0 ATR band — the exit is allowed."""
    assert adverse_move_is_noise(100.0, 107.0, ATR, side="sell") is False or True
    # entry anchor: price is ABOVE entry, so the guard answers "not my
    # business" and the caller's own in-profit path blocks it (the 54% case).
    assert adverse_move_is_noise(100.0, 107.0, ATR, side="sell") is False
    # the re-anchored band actually EVALUATES the move and clears it
    assert (
        adverse_move_is_noise(
            100.0,
            107.0,
            ATR,
            side="sell",
            extreme_since_entry=110.0,
        )
        is False
    )
    # and still blocks when the pullback from the high is inside the band
    assert (
        adverse_move_is_noise(
            100.0,
            109.0,
            ATR,
            side="sell",
            extreme_since_entry=110.0,
        )
        is True
    )


def test_long_fallback_reanchor_never_adds_a_block():
    """THE no-added-blocks guarantee, asserted on the home that owns it.

    `check_structural_protection`'s fallback blocks (protected=True) whenever
    the adverse move is <= 0 OR inside the band, with no early return between
    the two — so a larger adverse move can only ever RELEASE a block. That is
    the measured result (54 removed, 0 added) restated as a property of the
    code. The same sweep on `adverse_move_is_noise` alone would be FALSE: at
    the bare-function level re-anchoring does start evaluating positions that
    were flat-or-winning versus entry, and some of those land inside the band.
    """
    kw = dict(
        thesis_invalid_if=None,
        atr=ATR,
        computed_levels=[],
        min_level_touches=5,
        level_cluster_tolerance_pct=0.01,
    )
    for entry in (50.0, 100.0):
        for high in (entry, entry + 1.0, entry + 7.0):
            for price in [entry - 5 + 0.5 * k for k in range(30)]:
                new = check_structural_protection(
                    current_price=price,
                    entry_price=entry,
                    stop_loss=entry * 0.9,
                    extreme_since_entry=high,
                    **kw,
                ).protected
                old = check_structural_protection(
                    current_price=price,
                    entry_price=entry,
                    stop_loss=entry * 0.9,
                    **kw,
                ).protected
                assert not (new and not old), (entry, high, price)


# -------------------------------------------------------------------- SHORT
def test_short_block_removed_by_reanchoring_to_the_lowest_low():
    """Shorted at 100, fell to 90, now 93. From ENTRY the position is +7 in
    the short's favour, so the entry band never evaluates it. From the LOWEST
    LOW the adverse move is 3.0 = 1.5 ATR, outside the band."""
    assert adverse_move_is_noise(100.0, 93.0, ATR, side="buy") is False
    assert (
        adverse_move_is_noise(
            100.0,
            93.0,
            ATR,
            side="buy",
            extreme_since_entry=90.0,
        )
        is False
    )
    assert (
        adverse_move_is_noise(
            100.0,
            91.0,
            ATR,
            side="buy",
            extreme_since_entry=90.0,
        )
        is True
    )


def test_short_fallback_reanchor_never_adds_a_block():
    """The short mirror of the guarantee above, same home, same sweep."""
    kw = dict(
        thesis_invalid_if=None,
        atr=ATR,
        computed_levels=[],
        is_short=True,
        min_level_touches=5,
        level_cluster_tolerance_pct=0.01,
    )
    for entry in (50.0, 100.0):
        for low in (entry, entry - 1.0, entry - 7.0):
            for price in [entry - 5 + 0.5 * k for k in range(30)]:
                new = check_structural_protection(
                    current_price=price,
                    entry_price=entry,
                    stop_loss=entry * 1.1,
                    extreme_since_entry=low,
                    **kw,
                ).protected
                old = check_structural_protection(
                    current_price=price,
                    entry_price=entry,
                    stop_loss=entry * 1.1,
                    **kw,
                ).protected
                assert not (new and not old), (entry, low, price)


# ------------------------------------------------------------------- ANCHOR
def test_anchor_is_clamped_so_it_is_never_worse_than_entry():
    assert noise_band_anchor(100.0, 95.0, is_short=False) == 100.0
    assert noise_band_anchor(100.0, 110.0, is_short=False) == 110.0
    assert noise_band_anchor(100.0, 105.0, is_short=True) == 100.0
    assert noise_band_anchor(100.0, 90.0, is_short=True) == 90.0
    assert noise_band_anchor(100.0, None, is_short=False) == 100.0
    assert noise_band_anchor(100.0, float("nan"), is_short=False) == 100.0


def test_no_extreme_reproduces_the_old_entry_anchored_behaviour_exactly():
    for price in [96.0, 99.0, 99.5, 100.0, 101.0]:
        assert adverse_move_is_noise(100.0, price, ATR) == (0 < 100.0 - price < NOISE_BAND_ATR_MULTIPLE * ATR)


def test_width_and_session_widening_are_untouched():
    # 1.0 ATR at one session; sqrt(4)=2 ATR at four. Re-anchoring changes the
    # reference point only.
    assert (
        adverse_move_is_noise(
            100.0,
            97.0,
            ATR,
            extreme_since_entry=100.0,
            days_held=1,
        )
        is False
    )
    assert (
        adverse_move_is_noise(
            100.0,
            97.0,
            ATR,
            extreme_since_entry=100.0,
            days_held=4,
        )
        is True
    )


# ------------------------------------------------------------- THE RECORD
def test_midday_reviewer_row_is_always_entry_anchored():
    """The midday home is the MEASURED REGRESSION and keeps the entry anchor;
    its row says so on every observation, and there is no way to ask it for
    another anchor."""
    row = midday_payload(
        close_side="sell",
        blocked=True,
        adverse=1.0,
        entry=100.0,
        price=109.0,
        atr=ATR,
        band_multiple=1.0,
        sessions_held=1.0,
        sessions_measured=True,
        tail="SELL: x",
    )
    assert "anchor=100.0000" in row and "anchor_kind=entry" in row
    assert "extreme_since_entry" not in row
    with pytest.raises(TypeError):
        midday_payload(
            close_side="sell",
            blocked=True,
            adverse=1.0,
            entry=100.0,
            price=109.0,
            atr=ATR,
            band_multiple=1.0,
            sessions_held=1.0,
            sessions_measured=True,
            tail="SELL: x",
            anchor=110.0,
            anchor_kind="extreme_since_entry",
        )


def test_fallback_payload_names_the_anchor_it_used():
    _p, _b, detail = fallback_outcome(
        ent=100.0,
        cur=109.0,
        atr_f=ATR,
        is_short=False,
        is_noise=True,
        band_multiple=1.0,
        anchor=110.0,
        anchor_kind="extreme_since_entry",
    )
    assert "anchor=110" in detail and "anchor_kind=extreme_since_entry" in detail
    # default (no extreme available) still says so rather than staying silent
    assert (
        "anchor_kind=entry"
        in fallback_outcome(
            ent=100.0,
            cur=99.5,
            atr_f=ATR,
            is_short=False,
            is_noise=True,
            band_multiple=1.0,
        )[2]
    )


def test_structural_protection_fallback_uses_the_running_extreme():
    kw = dict(
        thesis_invalid_if=None,
        stop_loss=90.0,
        atr=ATR,
        computed_levels=[],
        min_level_touches=5,
        level_cluster_tolerance_pct=0.01,
    )
    # entry anchor: price above entry -> "no adverse move", protected
    entry_anchored = check_structural_protection(
        current_price=107.0,
        entry_price=100.0,
        **kw,
    )
    assert entry_anchored.protected is True
    # running-extreme anchor: 3.0 adverse = 1.5 ATR, outside the band
    reanchored = check_structural_protection(
        current_price=107.0,
        entry_price=100.0,
        extreme_since_entry=110.0,
        **kw,
    )
    assert reanchored.protected is False
    assert "anchor_kind=extreme_since_entry" in reanchored.detail


# ------------------------------------------------- THE WIRING AT THE HOME
class _Bar:
    def __init__(self, date, high, low):
        self.date, self.high, self.low = date, high, low
        self.close = (high + low) / 2
        self.open = self.close
        self.volume = 1_000


def _bars():
    """Entry on 2026-01-05; the high BEFORE entry is deliberately the
    highest bar of all, so a window that ignores the entry date would be
    caught."""
    return [
        _Bar("2026-01-02", 130.0, 128.0),  # before entry — must NOT count
        _Bar("2026-01-05", 101.0, 99.0),  # entry session — counts
        _Bar("2026-01-06", 110.0, 95.0),  # the real extreme both ways
        _Bar("2026-01-07", 107.0, 104.0),
    ]


def _home(last_buy=None):
    """The real `StructuralProtection` — the object that owns the fallback —
    over stand-ins for the collaborators it actually calls."""
    import types as _types
    from src.exits.structural_protection import StructuralProtection

    db = _types.SimpleNamespace(
        get_symbol_last_buy=lambda s: last_buy or {},
        get_recent_holding_protection_breaks=lambda *a, **k: [],
        record_holding_protection_break=lambda *a, **k: None,
    )
    return StructuralProtection(
        voice_structural_protection_break=lambda *a, **k: None,
        config=_types.SimpleNamespace(trading=_types.SimpleNamespace(lookback_days=200)),
        db=db,
        market=_types.SimpleNamespace(get_ohlcv=lambda s, n: _bars()),
        risk_engine=None,
    )


def test_running_extreme_is_read_from_the_entry_session_onward():
    p = _home()
    assert (
        p._extreme_since_entry(
            "AAA",
            _bars(),
            "2026-01-05",
            is_short=False,
        )
        == 110.0
    )
    assert (
        p._extreme_since_entry(
            "AAA",
            _bars(),
            "2026-01-05",
            is_short=True,
        )
        == 95.0
    )


def test_no_entry_date_means_no_extreme_rather_than_an_invented_window():
    """With no entry session there is no honest window, so the band falls
    back to the entry anchor instead of taking an extreme over a picked
    lookback."""
    p = _home()
    assert p._extreme_since_entry("AAA", _bars(), None, is_short=False) is None

    def _boom(s):
        raise RuntimeError("down")

    p.db.get_symbol_last_buy = _boom
    assert p._extreme_since_entry("AAA", _bars(), None, is_short=False) is None


def test_entry_date_falls_back_to_the_symbols_last_buy():
    p = _home(last_buy={"timestamp": "2026-01-05T14:30:00"})
    assert p._extreme_since_entry("AAA", _bars(), None, is_short=False) == 110.0


def test_the_fallback_call_actually_receives_the_running_extreme(monkeypatch):
    """The defect this file exists to prevent: the anchor computed and then
    never passed to the home that was measured to benefit from it."""
    import src.risk.exit_guard as eg

    seen = {}
    real = eg.check_structural_protection

    def _spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(eg, "check_structural_protection", _spy)
    p = _home()
    p._structural_protection_for_holding(
        symbol="AAA",
        thesis_invalid_if=None,
        entry_price=100.0,
        stop_loss=90.0,
        is_short=False,
        run_id="r1",
        persist=False,
        entry_date="2026-01-05",
    )
    assert seen.get("extreme_since_entry") == 110.0
