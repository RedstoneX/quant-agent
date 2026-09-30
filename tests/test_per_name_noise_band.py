"""Board item 70: the exit noise band is read off the instrument itself.

Owner ruling 2026-09-30 — risk tolerance is never a global dial. These tests
pin that the band comes from THIS name's own bars, that it is not a multiple
of anything, and that the old global path survives for callers that cannot
make the read.
"""
import math

from src.risk.exit_guard import (
    NOISE_BAND_ATR_MULTIPLE, adverse_move_is_noise,
    instrument_adverse_noise_band, noise_band_atr,
)


def _bars(closes, span):
    """Daily bars whose every session travels `span` below its own close."""
    return [{"close": c, "high": c + span / 2, "low": c - span} for c in closes]


def test_band_is_the_median_worst_adverse_excursion_of_this_name():
    band, windows = instrument_adverse_noise_band(_bars([100.0] * 11, 2.0), 1)
    assert math.isclose(band, 2.0)
    assert windows == 10


def test_a_calmer_name_gets_a_tighter_band_than_a_wilder_one():
    calm, _ = instrument_adverse_noise_band(_bars([100.0] * 21, 1.0), 3)
    wild, _ = instrument_adverse_noise_band(_bars([100.0] * 21, 6.0), 3)
    assert calm < wild


def test_a_longer_hold_reads_a_wider_band_off_the_same_name():
    closes = [100.0 - i * 0.5 for i in range(31)]
    short_hold, _ = instrument_adverse_noise_band(_bars(closes, 1.0), 1)
    long_hold, _ = instrument_adverse_noise_band(_bars(closes, 1.0), 10)
    assert long_hold > short_hold


def test_no_read_is_possible_without_a_full_window():
    assert instrument_adverse_noise_band(_bars([100.0, 100.5], 1.0), 10) is None
    assert instrument_adverse_noise_band(None, 1) is None


def test_a_flat_series_yields_no_band_rather_than_a_zero_one():
    flat = [{"close": 100.0, "high": 100.0, "low": 100.0}] * 10
    assert instrument_adverse_noise_band(flat, 1) is None


def test_the_short_side_measures_adverse_travel_upward():
    bars = [{"close": 100.0, "high": 104.0, "low": 99.5}] * 10
    long_band, _ = instrument_adverse_noise_band(bars, 1, side="sell")
    short_band, _ = instrument_adverse_noise_band(bars, 1, side="buy")
    assert math.isclose(long_band, 0.5) and math.isclose(short_band, 4.0)


def test_bars_replace_the_global_multiple_entirely():
    """A move the old 1.0xATR band called noise is NOT noise on a name whose
    own ordinary adverse travel is smaller than one ATR."""
    bars = _bars([100.0] * 21, 1.0)          # this name travels $1 against you
    atr = 4.0                                 # ATR says $4
    assert adverse_move_is_noise(100.0, 98.0, atr, days_held=1) is True
    assert adverse_move_is_noise(100.0, 98.0, atr, days_held=1, bars=bars) is False


def test_the_old_global_path_is_still_there_when_no_bars_are_given():
    atr = 2.0
    assert adverse_move_is_noise(100.0, 99.0, atr, days_held=1) is True
    assert noise_band_atr(1) == NOISE_BAND_ATR_MULTIPLE
    # An unreadable series falls back rather than failing open.
    assert adverse_move_is_noise(100.0, 99.0, atr, days_held=1, bars=[]) is True


def test_profit_and_missing_data_still_never_manufacture_a_block():
    bars = _bars([100.0] * 21, 1.0)
    assert adverse_move_is_noise(100.0, 105.0, 2.0, days_held=1, bars=bars) is False
    assert adverse_move_is_noise(100.0, 99.0, None, days_held=1, bars=bars) is False
