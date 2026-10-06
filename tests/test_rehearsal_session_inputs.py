"""One-session provider ledger: real call outcomes, then no-wire replay."""

from datetime import date
from types import SimpleNamespace
from urllib.error import HTTPError

import pandas as pd
import pytest

from ops.rehearsal.session_inputs import (
    SessionInputError,
    SessionInputs,
    _revive,
    _value,
    session_inputs,
)


class _Fred:
    def get_series(self, series_id, **_kwargs):
        return pd.Series([1.2, 2.3], index=pd.to_datetime(["2026-10-01", "2026-10-02"]))

    def get_series_info(self, series_id, **_kwargs):
        return pd.Series({"observation_end": "2026-10-02", "last_updated": "2026-10-03"})


class _RawResponse:
    status = 200
    headers = {"content-type": "text/xml"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b"<rss><item>real</item></rss>"


def test_frame_and_series_round_trip_without_pickle():
    idx = pd.to_datetime(["2026-10-01", "2026-10-02"])
    cols = pd.MultiIndex.from_tuples([("Close", "SPY"), ("Volume", "SPY")])
    frame = pd.DataFrame([[1.25, 100], [float("nan"), 200]], index=idx, columns=cols)
    restored = _revive(_value(frame))
    pd.testing.assert_frame_equal(restored, frame, check_freq=False)
    series = pd.Series([1.1, 2.2], index=idx)
    pd.testing.assert_series_equal(_revive(_value(series)), series, check_freq=False)
    assert _revive(_value(date(2026, 10, 2))) == date(2026, 10, 2)


def test_live_provider_calls_replay_without_calling_originals(monkeypatch):
    import yfinance as yf
    from src.data import news

    counters = {"download": 0, "ticker": 0, "url": 0}

    def download(*_args, **_kwargs):
        counters["download"] += 1
        return pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-10-02"]))

    class RawTicker:
        def __init__(self, *_args, **_kwargs):
            counters["ticker"] += 1

        @property
        def info(self):
            return {"sector": "Technology", "irrelevant": "not recorded"}

    def urlopen(*_args, **_kwargs):
        counters["url"] += 1
        return _RawResponse()

    monkeypatch.setattr(yf, "download", download)
    monkeypatch.setattr(yf, "Ticker", RawTicker)
    monkeypatch.setattr(news, "urlopen", urlopen)
    pipe = SimpleNamespace(macro=SimpleNamespace(fred=_Fred()))

    def exercise():
        first = yf.download("SPY", period="5d")
        sector = yf.Ticker("MSFT").info["sector"]
        series = pipe.macro.fred.get_series("DGS10")
        with news.urlopen("https://example.test/feed?api_key=secret&month=10") as response:
            body = response.read()
        return first, sector, series, body

    with session_inputs(pipe) as recording:
        before = exercise()
    payload = recording.payload()
    assert len(payload["entries"]) == 4
    assert "secret" not in str(payload)
    assert "irrelevant" not in str(payload)
    assert counters == {"download": 1, "ticker": 1, "url": 1}
    with session_inputs(pipe, payload) as replay:
        after = exercise()
        replay.assert_consumed()
    pd.testing.assert_frame_equal(after[0], before[0], check_freq=False)
    pd.testing.assert_series_equal(after[2], before[2], check_freq=False)
    assert after[1] == before[1]
    assert after[3] == before[3]
    assert counters == {"download": 1, "ticker": 1, "url": 1}


def test_http_failure_type_replays_and_missing_calls_fail_closed(monkeypatch):
    from src.data import news

    def bad(request, *_args, **_kwargs):
        raise HTTPError(str(request), 503, "unavailable", {}, None)

    monkeypatch.setattr(news, "urlopen", bad)
    pipe = SimpleNamespace(macro=SimpleNamespace(fred=_Fred()))
    with session_inputs(pipe) as recorder:
        with pytest.raises(HTTPError) as error:
            news.urlopen("https://example.test/outage")
        assert error.value.code == 503
    with session_inputs(pipe, recorder.payload()) as replay:
        with pytest.raises(HTTPError) as replayed:
            news.urlopen("https://example.test/outage")
        assert replayed.value.code == 503
        with pytest.raises(SessionInputError, match="missing recorded"):
            news.urlopen("https://example.test/absent")
        with pytest.raises(SessionInputError, match="strict provider replay violation"):
            replay.assert_consumed()


def test_real_market_provider_fallback_still_runs_during_replay(monkeypatch):
    import yfinance as yf
    from src.data.market import MarketDataProvider
    from src.models import OHLCV

    counts = {"download": 0, "fallback": 0}

    def empty_download(*_args, **_kwargs):
        counts["download"] += 1
        return pd.DataFrame()

    def fallback(*_args):
        counts["fallback"] += 1
        return [OHLCV(date=date(2026, 9, 1), open=10, high=11,
                      low=9, close=10, volume=100)]

    monkeypatch.setattr(yf, "download", empty_download)
    market = MarketDataProvider(fallback_bars=fallback)
    pipe = SimpleNamespace(macro=SimpleNamespace(fred=_Fred()))
    with session_inputs(pipe) as recorder:
        live = market.get_ohlcv("SPY", lookback_days=30)
    with session_inputs(pipe, recorder.payload()) as replay:
        offline = market.get_ohlcv("SPY", lookback_days=30)
        replay.assert_consumed()
    assert offline == live
    assert counts == {"download": 1, "fallback": 2}


def test_unconsumed_provider_answer_fails_closed():
    ledger = SessionInputs({"schema": 1, "entries": [
        {"kind": "FRED.get_series", "key": "DGS10", "value": 42},
    ]})
    with pytest.raises(SessionInputError, match="not consumed"):
        ledger.assert_consumed()


def test_capture_byte_bound_does_not_change_live_provider_answer():
    ledger = SessionInputs(max_bytes=8)
    actual = ledger.call("feed", "name", lambda: "real answer")
    assert actual == "real answer"
    with pytest.raises(SessionInputError, match="byte bound exceeded"):
        ledger.payload()
