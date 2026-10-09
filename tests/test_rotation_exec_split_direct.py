"""rotation_projection and rotation_buy_leg_post exercised directly (no pipeline)."""

from types import SimpleNamespace

from src import rotation_buy_leg_post as post
from src import rotation_projection as proj


def test_projected_entry_cost_is_allocation_of_equity():
    d = SimpleNamespace(action="BUY", allocation_pct=10.0)
    assert proj._projected_entry_cost(d, 1000.0, budget_is_gross=False) == 100.0


def test_projected_entry_cost_short_on_cash_pool_is_free_and_bad_input_is_zero():
    short = SimpleNamespace(action="SHORT", allocation_pct=10.0)
    assert proj._projected_entry_cost(short, 1000.0, budget_is_gross=False) == 0.0
    assert proj._projected_entry_cost(short, 1000.0, budget_is_gross=True) == 100.0
    bad = SimpleNamespace(action="BUY", allocation_pct="x")
    assert proj._projected_entry_cost(bad, 1000.0, budget_is_gross=False) == 0.0


def test_alert_buy_leg_missing_goes_through_the_notifier(monkeypatch):
    from src import notifier

    sent = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda body, symbols=None: sent.append((body, symbols)))
    post._alert_rotation_buy_leg_missing(rotation={"held_symbol": "AAA", "new_symbol": "BBB"}, detail="why")
    assert len(sent) == 1 and sent[0][1] == ["AAA", "BBB"] and "why" in sent[0][0]


def test_alert_buy_leg_missing_never_raises():
    post._alert_rotation_buy_leg_missing(rotation={}, detail="x")
