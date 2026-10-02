from src.llm_balance_runway import compute_state

DAYS = {"2026-09-21": 2.648, "2026-09-24": 2.422, "2026-09-30": 4.0, "2026-10-01": 1.0}


def test_low_when_under_two_worst_days():
    s = compute_state(DAYS, snapshot=None, topup_usd=10.0, topup_date="2026-10-01")
    assert s["source"] == "derived" and s["remaining_usd"] == 9.0
    assert s["warn_below_usd"] == 8.0 and s["status"] == "ok"
    s = compute_state({**DAYS, "2026-10-02": 2.0}, snapshot=None, topup_usd=10.0, topup_date="2026-10-01")
    assert s["status"] == "low" and "Top up OpenRouter" in s["message"]


def test_provider_snapshot_wins_and_falls_with_later_spend():
    s = compute_state(DAYS, snapshot={"remaining_usd": 20.0, "as_of_day": "2026-09-30"},
                      topup_usd=1.0, topup_date="2026-10-01")
    assert s["source"] == "provider" and s["remaining_usd"] == 19.0


def test_unknown_is_never_ok():
    assert compute_state(DAYS, snapshot=None, topup_usd=None, topup_date=None)["status"] == "unknown"
    assert compute_state({}, snapshot=None, topup_usd=10, topup_date="2026-10-01")["status"] == "unknown"
