"""Both admission paths (side door and weekly run) refuse a name that did not
trade every session of its year, through the one shared bar check."""

from __future__ import annotations

from pathlib import Path

from tests.test_admission_service import _bars, _service


def _flat_day_bars():
    bars = _bars(300)
    flat = bars[-30].close
    bars[-30] = bars[-30].model_copy(update={"high": flat, "low": flat})
    return bars


def test_side_door_admission_refuses_a_flat_day_name(tmp_path):
    svc, _ = _service(tmp_path, enabled=True)
    svc.market.get_ohlcv.return_value = _flat_day_bars()
    ok, reason, _ = svc._evaluate_screened_admission("ACME", context="test")
    assert ok is False and "not_traded_every_session" in reason


def test_weekly_run_refuses_a_flat_day_name(tmp_path):
    svc, _ = _service(tmp_path, enabled=True)
    svc.market.get_ohlcv_batch.side_effect = lambda chunk, days: {s: _flat_day_bars() for s in chunk}
    svc._run_universe_screen("evening-x")
    text = "".join(p.read_text() for p in Path(tmp_path).rglob("*") if p.is_file())
    assert "not_traded_every_session" in text
