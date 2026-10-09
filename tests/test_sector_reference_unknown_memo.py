"""An Unknown sector answer is remembered for the ET trading day, not re-fetched per call."""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src import sector_reference as sr


@pytest.fixture(autouse=True)
def _clean():
    sr._sector_cache.clear()
    sr._sector_unknown_memo.clear()
    yield
    sr._sector_cache.clear()
    sr._sector_unknown_memo.clear()


@pytest.fixture(autouse=True)
def _real_lookup(monkeypatch):
    # conftest swaps in an offline stub; this file is ABOUT the real lookup.
    fn = sr._get_sector
    real = getattr(fn, "real_get_sector", fn)
    monkeypatch.setattr(sr, "_get_sector", real)


def _stub(info):
    m = MagicMock()
    m.return_value.info = info
    return m


def test_throttled_name_fetched_once_across_50_lookups():
    stub = _stub({})
    with patch.object(sr.yf, "Ticker", stub):
        results = {sr._get_sector("THRTL") for _ in range(50)}
    assert results == {"Unknown"}
    assert stub.call_count == 1


def test_rechecked_after_trading_day_boundary():
    stub = _stub({})
    with patch.object(sr.yf, "Ticker", stub):
        with patch.object(sr, "et_today", return_value=date(2026, 10, 9)):
            sr._get_sector("THRTL")
            sr._get_sector("THRTL")
        assert stub.call_count == 1
        with patch.object(sr, "et_today", return_value=date(2026, 10, 12)):
            sr._get_sector("THRTL")
    assert stub.call_count == 2


def test_known_sector_unaffected_and_recovery_after_boundary():
    stub = _stub({"sector": "Technology"})
    with patch.object(sr.yf, "Ticker", stub):
        assert sr._get_sector("AAPL") == "Technology"
        assert sr._get_sector("AAPL") == "Technology"
    assert stub.call_count == 1
    assert "AAPL" not in sr._sector_unknown_memo
    stub2 = _stub({})
    with patch.object(sr.yf, "Ticker", stub2):
        with patch.object(sr, "et_today", return_value=date(2026, 10, 9)):
            assert sr._get_sector("ZZ") == "Unknown"
        stub2.return_value.info = {"sector": "Energy"}
        with patch.object(sr, "et_today", return_value=date(2026, 10, 12)):
            assert sr._get_sector("ZZ") == "Energy"
