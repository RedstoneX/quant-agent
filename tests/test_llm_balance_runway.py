import json
import logging
import sqlite3

import pytest

from src import cost_table, openrouter_balance
from src.openrouter_balance import OpenRouterBalanceUnavailable, fetch_openrouter_balance, record_openrouter_balance
from src.llm_balance_runway import compute_state, read_state
from src.trading_calendar import et_today

DAYS = {"2026-09-21": 2.648, "2026-09-24": 2.422, "2026-09-30": 4.0, "2026-10-01": 1.0}


def test_low_when_under_two_worst_days():
    s = compute_state(DAYS, snapshot=None, topup_usd=10.0, topup_date="2026-10-01")
    assert s["source"] == "derived" and s["remaining_usd"] == 9.0
    assert s["warn_below_usd"] == 8.0 and s["status"] == "ok"
    s = compute_state({**DAYS, "2026-10-02": 2.0}, snapshot=None, topup_usd=10.0, topup_date="2026-10-01")
    assert s["status"] == "low" and "Top up OpenRouter" in s["message"]


def test_provider_snapshot_wins_and_falls_with_later_spend():
    s = compute_state(
        DAYS, snapshot={"remaining_usd": 20.0, "as_of_day": "2026-09-30"}, topup_usd=1.0, topup_date="2026-10-01"
    )
    assert s["source"] == "provider" and s["remaining_usd"] == 19.0


def test_unknown_is_never_ok():
    assert compute_state(DAYS, snapshot=None, topup_usd=None, topup_date=None)["status"] == "unknown"
    assert compute_state({}, snapshot=None, topup_usd=10, topup_date="2026-10-01")["status"] == "unknown"


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def test_fetch_refuses_by_name_without_a_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(OpenRouterBalanceUnavailable) as exc:
        fetch_openrouter_balance(tmp_path / "bal.json")
    assert "OPENROUTER_API_KEY" in str(exc.value) and openrouter_balance.OPENROUTER_CREDITS_URL in str(exc.value)
    assert not (tmp_path / "bal.json").exists()


def test_fetch_writes_the_snapshot_on_the_exchange_day(monkeypatch, tmp_path):
    import requests

    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder-key")
    seen = {}

    def _get(url, **kwargs):
        seen["url"] = url
        return _Resp({"data": {"total_credits": "12.5", "total_usage": "2.25"}})

    monkeypatch.setattr(requests, "get", _get)
    snap = fetch_openrouter_balance(tmp_path / "bal.json")
    assert seen["url"] == openrouter_balance.OPENROUTER_CREDITS_URL
    assert snap["remaining_usd"] == 10.25
    assert snap["as_of_day"] == et_today().isoformat()
    assert json.loads((tmp_path / "bal.json").read_text()) == snap


def test_a_refused_call_is_named_and_logged_not_swallowed(monkeypatch, tmp_path, caplog):
    import requests

    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder-key")

    def _get(url, **kwargs):
        raise ConnectionError("blocked by the rehearsal wall")

    monkeypatch.setattr(requests, "get", _get)
    with caplog.at_level(logging.ERROR, logger="src.openrouter_balance"):
        out = record_openrouter_balance(tmp_path / "bal.json")
    assert isinstance(out, str) and "blocked by the rehearsal wall" in out
    assert openrouter_balance.OPENROUTER_CREDITS_URL in out
    assert any("NOT recorded" in r.getMessage() for r in caplog.records)
    assert not (tmp_path / "bal.json").exists()


def test_an_unreadable_snapshot_is_unknown_not_derived(tmp_path):
    db = tmp_path / "desk.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE llm_budget_days (day TEXT, baseline_cost_usd REAL, incremental_cost_usd REAL)")
    conn.executemany("INSERT INTO llm_budget_days VALUES (?,?,?)", [(d, c, 0.0) for d, c in DAYS.items()])
    conn.commit()
    conn.close()
    good = read_state(str(db), topup_usd=10.0, topup_date="2026-10-01", snapshot_path=tmp_path / "none.json")
    assert good["source"] == "derived"
    (tmp_path / "bad.json").write_text("{not json")
    bad = read_state(str(db), topup_usd=10.0, topup_date="2026-10-01", snapshot_path=tmp_path / "bad.json")
    assert bad["status"] == "unknown" and "bad.json" in bad["message"] and "source" not in bad
