"""Eight more money-path swallows record a counted row plus a traceback.

Each test forces the real exception and asserts the recorder ran with the
site's name; the full-list test fails if a handler goes back to a bare log line.
Return values are asserted unchanged: loud only, no control-flow change.
"""
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.silent_swallow_guard import violations

CONVERTED = {
    "src/execution/broker_parts/market_data.py": {"get_intraday_chart_bars", "_fetch_batch"},
        "src/data/macro.py": {"_write_cache"},
    "src/alert_watchdog.py": {"record_check", "verify_alert_channel"},
    "src/notifier/transport.py": {"_json_body"},
}


def test_converted_sites_are_not_silent_again():
    back = [(s[0], s[1]) for s, _ in violations()
            if s[0] in CONVERTED and s[1] in CONVERTED[s[0]]]
    assert not back, f"swallow went silent again: {back}"


@pytest.fixture
def seen(monkeypatch):
    rows = []

    def _rec(where, exc, **kw):
        rows.append((where, exc))

    for mod in ("src.execution.broker_parts.market_data", "src.data.macro", "src.alert_watchdog", "src.notifier.transport"):
        monkeypatch.setattr(f"{mod}.record_swallowed", _rec)
    return rows


def _boom(*a, **k):
    raise RuntimeError("boom")


def test_intraday_chart_bars_failure_is_recorded(seen):
    from src.execution.broker_parts.market_data import MarketData
    me = SimpleNamespace(_data_client=SimpleNamespace(get_stock_bars=_boom),
                         api_key="k", secret_key="s")
    assert MarketData.get_intraday_chart_bars(me, "AAA", "5m", 1) == []
    assert [w for w, _ in seen] == ["broker.intraday_chart_bars"]


def test_single_symbol_snapshot_failure_is_recorded(seen):
    from src.execution.broker_parts.market_data import MarketData
    me = SimpleNamespace(_data_client=SimpleNamespace(get_stock_snapshot=_boom))
    assert MarketData.get_intraday_snapshots(me, ["AAA"]) == {}
    assert [w for w, _ in seen] == ["broker.intraday_snapshots_single"]


def test_bulk_snapshot_failure_is_recorded(seen):
    from src.execution.broker_parts.market_data import MarketData
    me = SimpleNamespace(_data_client=SimpleNamespace(get_stock_snapshot=_boom))
    assert MarketData.get_intraday_snapshots(me, ["AAA", "BBB"]) == {}
    assert [w for w, _ in seen] == ["broker.intraday_snapshots_bulk"]


def test_series_cache_serialise_failure_is_recorded(seen):
    from src.data.macro import MacroDataProvider
    bad = SimpleNamespace(items=_boom)
    me = SimpleNamespace(series_cache=SimpleNamespace(save=_boom))
    MacroDataProvider._write_cache(me, "S", {}, bad, None)
    assert [w for w, _ in seen] == ["data.macro.series_cache_serialise"]


def test_watchdog_open_failure_is_recorded(seen, monkeypatch, tmp_path):
    import src.alert_watchdog as w
    monkeypatch.setattr(w, "_connect_rw", _boom)
    assert w.record_check(ok=True, stage="x", db_path=tmp_path / "d.db") is False
    assert [x for x, _ in seen] == ["alert_watchdog.open_db"]


def test_watchdog_record_failure_is_recorded(seen, monkeypatch, tmp_path):
    import sqlite3
    import src.alert_watchdog as w
    monkeypatch.setattr(w, "_connect_rw", lambda p: sqlite3.connect(":memory:"))
    monkeypatch.setattr(w, "ensure_schema", _boom)
    assert w.record_check(ok=True, stage="x", db_path=tmp_path / "d.db") is False
    assert [x for x, _ in seen] == ["alert_watchdog.record_check"]


def test_watchdog_probe_failure_is_recorded(seen, monkeypatch):
    import src.alert_watchdog as w
    import src.notifier as n
    monkeypatch.setattr(n, "build_default_notifier", _boom)
    assert w.verify_alert_channel() is None
    assert [x for x, _ in seen] == ["alert_watchdog.probe"]


def test_telegram_non_json_body_is_recorded(seen):
    from src.notifier.transport import TelegramNotifier
    resp = SimpleNamespace(json=_boom)
    assert TelegramNotifier._json_body(resp) == {}
    assert [w for w, _ in seen] == ["notifier.transport.json_body"]
