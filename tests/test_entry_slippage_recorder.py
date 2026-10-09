"""The slippage recorder must count every check and change no price."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src import entry_slippage_bound as mod
from src.entry_slippage_bound import entry_bound, latest_daily_range_pct


class _Ctx(SimpleNamespace):
    pass


@pytest.fixture()
def recorded(monkeypatch):
    rows: list[dict] = []

    def fake_persist(db, **kw):
        rows.append(kw)

    import src.pipeline_stages as stages

    monkeypatch.setattr(stages, "_persist_evidence", fake_persist)
    return rows


def _ctx(bars=None):
    return _Ctx(run_id="r1", decision_id="d1", approved_entry_ceiling={}, symbols_bars={"AAA": bars or []})


def test_buy_ceiling_is_the_same_arithmetic_as_before(recorded):
    out = entry_bound(SimpleNamespace(db=None), _ctx(), "AAA", 100.0, 100.5, 40.0, is_short=False)
    assert out.bound_price == pytest.approx(100.4)
    assert out.limit_price == 100.4
    assert out.observed_bps == pytest.approx(50.0)


def test_short_floor_is_the_mirror(recorded):
    out = entry_bound(SimpleNamespace(db=None), _ctx(), "AAA", 100.0, 99.0, 40.0, is_short=True)
    assert out.bound_price == pytest.approx(99.6)
    assert out.observed_bps == pytest.approx(100.0)


def test_a_pinned_ceiling_still_wins(recorded):
    ctx = _ctx()
    ctx.approved_entry_ceiling = {"AAA": 101.25}
    out = entry_bound(SimpleNamespace(db=None), ctx, "AAA", 100.0, 100.5, 40.0, is_short=False)
    assert out.bound_price == 101.25
    assert json.loads(recorded[0]["evidence_json"])["cap_was_pinned"] is True


def test_every_check_writes_one_counted_row(recorded):
    bars = [{"high": 106.0, "low": 100.0, "close": 100.0}]
    entry_bound(SimpleNamespace(db=None), _ctx(bars), "AAA", 100.0, 100.5, 40.0, is_short=False)
    assert len(recorded) == 1
    row = recorded[0]
    assert row["kind"] == "entry_slippage_check"
    body = json.loads(row["evidence_json"])
    assert body["cap_bound"] is True  # 50bp observed against a 40bp cap
    assert body["cap_bps"] == 40.0
    assert body["daily_range_pct"] == pytest.approx(6.0)


def test_a_check_inside_the_cap_is_recorded_as_not_binding(recorded):
    entry_bound(SimpleNamespace(db=None), _ctx(), "AAA", 100.0, 100.1, 40.0, is_short=False)
    assert json.loads(recorded[0]["evidence_json"])["cap_bound"] is False


def test_a_failed_write_never_breaks_the_order(monkeypatch):
    import src.pipeline_stages as stages

    def boom(db, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(stages, "_persist_evidence", boom)
    out = entry_bound(SimpleNamespace(db=None), _ctx(), "AAA", 100.0, 100.5, 40.0, is_short=False)
    assert out.limit_price == 100.4


@pytest.mark.parametrize("bars", [None, [], [{"high": 1.0}], [{"high": 2.0, "low": 1.0, "close": 0.0}]])
def test_an_unreadable_bar_stores_no_guess(bars):
    assert latest_daily_range_pct(bars) is None


def test_the_recorder_is_actually_wired_into_the_entry_path():
    """A recorder nothing calls would pass its own tests vacuously."""
    import os

    src = os.path.join(os.path.dirname(mod.__file__), "stage_execution_parts", "entry_quote.py")
    text = open(src, encoding="utf-8").read()
    assert "from src.entry_slippage_bound import entry_bound" in text
    assert text.count("entry_bound(") >= 2
