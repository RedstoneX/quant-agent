"""Clause-5 witness for src.pipeline_sizing: exercised with stand-ins, no trading pipeline built."""

from types import SimpleNamespace

from src.pipeline_sizing import _fmt_shares, _size_shares


def test_size_shares_whole_mode_floors_without_any_pipeline():
    assert _size_shares(None, 6.9, fractional=False) == 6.0


def test_size_shares_fractional_uses_explicit_config_stand_in():
    stand_in = SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace(fractional_share_decimals=2)))
    assert _size_shares(stand_in, 1.239, fractional=True) == 1.23


def test_fmt_shares():
    assert _fmt_shares(3.0) == "3"
    assert _fmt_shares(0.5) == "0.5"
