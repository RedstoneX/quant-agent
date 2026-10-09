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


# --- one source of truth: the credit line, staleness, alert ---------------
from datetime import datetime, timedelta, timezone

from src import llm_balance_runway as rw

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _snap(hours_old, remaining=10.0):
    return {
        "remaining_usd": remaining,
        "as_of_day": "2026-10-01",
        "fetched_at": (NOW - timedelta(hours=hours_old)).isoformat(),
    }


def test_average_skips_zero_days_and_uses_last_seven_spend_days():
    days = {f"2026-09-{d:02d}": 1.0 for d in range(1, 11)}  # ten spend days at 1.0
    days.update({"2026-09-20": 0.0, "2026-09-21": 0.0, "2026-09-22": 3.0})
    s = compute_state(days, snapshot=_snap(1, 9.0), topup_usd=None, topup_date=None, now=NOW)
    assert s["avg_window_days"] == 7 and s["mean_day_usd"] == round((6 * 1.0 + 3.0) / 7, 3)
    assert s["days_left"] == round(9.0 / ((6 * 1.0 + 3.0) / 7), 1)


def test_line_states_window_age_and_never_says_of_total():
    s = compute_state(DAYS, snapshot=_snap(3, 20.0), topup_usd=None, topup_date=None, now=NOW)
    assert s["message"].startswith("AI credit: $")
    assert "avg $" in s["message"] and "/day over last 4 trading days run" in s["message"]
    assert "balance 3h old" in s["message"] and "STALE" not in s["message"]
    assert " of $" not in s["message"]


def test_old_snapshot_is_marked_stale():
    s = compute_state(DAYS, snapshot=_snap(13), topup_usd=None, topup_date=None, now=NOW)
    assert s["stale"] is True and "STALE" in s["message"]


def test_snapshot_without_a_timestamp_is_stale_not_fresh():
    snap = {"remaining_usd": 10.0, "as_of_day": "2026-10-01"}
    s = compute_state(DAYS, snapshot=snap, topup_usd=None, topup_date=None, now=NOW)
    assert s["stale"] is True and "STALE" in s["message"]


def test_unknown_states_why_and_is_loud():
    s = compute_state(DAYS, snapshot=None, topup_usd=None, topup_date=None)
    assert s["message"].startswith("AI credit UNKNOWN:") and "no top-up" in s["message"]
    assert compute_state({}, snapshot=None, topup_usd=1, topup_date="2026-10-01")["message"].startswith(
        "AI credit UNKNOWN:"
    )


def test_low_line_is_loud():
    s = compute_state(DAYS, snapshot=_snap(1, 5.0), topup_usd=None, topup_date=None, now=NOW)
    assert s["status"] == "low" and s["message"].startswith("LOW AI credit:")


def test_alert_fires_once_on_change_and_rearms_after_recovery(tmp_path):
    sent = []
    path = tmp_path / "state.json"
    low = {"status": "low", "message": "LOW AI credit: $1"}
    ok = {"status": "ok", "message": "AI credit: $50"}

    def send(m):
        sent.append(m)
        return True

    assert rw.alert_on_state_change(low, path=path, send=send) is True
    assert rw.alert_on_state_change(low, path=path, send=send) is False
    assert rw.alert_on_state_change(ok, path=path, send=send) is False
    assert rw.alert_on_state_change({"status": "unknown", "message": "AI credit UNKNOWN: x"}, path=path, send=send)
    assert len(sent) == 2


def test_failed_send_is_retried_next_time(tmp_path):
    path = tmp_path / "state.json"
    low = {"status": "low", "message": "LOW"}
    assert rw.alert_on_state_change(low, path=path, send=lambda m: False) is False
    assert rw.alert_on_state_change(low, path=path, send=lambda m: True) is True


def test_footer_credit_line_is_morning_only(monkeypatch):
    from src.trader_feed.decision import _append_footer

    monkeypatch.setattr(rw, "balance_line", lambda: "AI credit: $8.81 left")
    monkeypatch.setattr(rw, "alert_on_state_change", lambda *a, **k: False)
    for mode, expected in (("morning", True), ("once", True), ("midday", False), ("evening", False), ("", False)):
        lines: list[str] = []
        _append_footer(lines, {"cost": 0.1}, 5.0, mode)
        assert any("AI credit:" in ln for ln in lines) is expected, mode


def test_old_live_call_helper_is_gone():
    import src.notifier.costs as costs

    assert not hasattr(costs, "_openrouter_balance_line")
