"""The daily universe build refuses a name that did not trade every session."""

from __future__ import annotations

from tests.test_universe_daily import _asset, _run
from tests.test_universe_screen import _good_bars


def _failures(run, symbol):
    return run.records[symbol]["failures"]


def test_daily_build_refuses_a_flat_day_name(tmp_path):
    bars = _good_bars()
    flat = bars[-30].close
    bars[-30] = bars[-30].model_copy(update={"high": flat, "low": flat})
    run, _ = _run([_asset("ACME"), _asset("FLAT")], tmp_path, bars={"FLAT": bars})
    assert "not_traded_every_session" in _failures(run, "FLAT")
    assert "not_traded_every_session" not in _failures(run, "ACME")


def test_daily_build_refuses_a_name_missing_a_session(tmp_path):
    full = _good_bars()
    run, _ = _run([_asset("ACME"), _asset("GAPPY")], tmp_path, bars={"GAPPY": full[:-40] + full[-39:]})
    assert "not_traded_every_session" in _failures(run, "GAPPY")
