"""The trend exit at the 30-minute tick, on DAILY closes only (owner ruling 2026-10-09).

Pins: one check per new completed bar; a refused sale is retried next tick;
the trail skips a name being sold; only the completed-bar date and the
completed-bar feed are read; the sale goes through the review's guarded path.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.exits_parts.midday_state import SKIP
from src.intraday import trend_exit_tick
from src.intraday.tick_trail import trail_on_tick
from src.intraday.trend_exit_tick import OUTCOME_REFUSED, OUTCOME_SOLD, trend_exit_on_tick, wire_trend_exit
from tests.pipeline_factory import build_pipeline
from tests.test_phase3_exit_rework import _position

EXIT = SimpleNamespace(exit_cleared=True, status="EXIT", reason="trend over")
HOLD = SimpleNamespace(exit_cleared=False, status="HOLD", reason="intact")


class _Desk:
    """In-memory marker store and recording fakes for `trend_exit_on_tick`."""

    def __init__(self, verdict=EXIT, sells=True):
        self.markers: dict = {}
        self.verdict = verdict
        self.sells = sells
        self.verdict_reads: list = []
        self.executed: list = []

    def read(self, symbol):
        return self.markers.get(symbol)

    def write(self, symbol, *, bar_date, outcome, run_id=None):
        self.markers[symbol] = {"bar_date": bar_date, "outcome": outcome}

    def cached(self, **kw):
        self.verdict_reads.append(kw["symbol"])
        return self.verdict

    def execute(self, firing, *, run_id, position_facts):
        self.executed.append([p.symbol for p in firing])
        return [{"symbol": p.symbol} for p in firing] if self.sells else []

    def tick(self, positions, bar_date, run_id="r"):
        return trend_exit_on_tick(
            positions=positions,
            run_id=run_id,
            completed_bar_date=bar_date,
            read_marker=self.read,
            write_marker=self.write,
            alignment_exit_cached=self.cached,
            position_facts_for=lambda due: {},
            execute_guarded_exits=self.execute,
        )


def test_fires_once_per_new_completed_bar_not_again_later_same_day():
    desk = _Desk()
    first = desk.tick([_position("AAA")], "2026-10-09", run_id="t0945")
    assert first["sold"] == ["AAA"]
    assert desk.markers["AAA"] == {"bar_date": "2026-10-09", "outcome": OUTCOME_SOLD}
    later = desk.tick([_position("AAA")], "2026-10-09", run_id="t1015")
    assert later["checked"] == [] and desk.verdict_reads == ["AAA"] and len(desk.executed) == 1
    desk.tick([_position("AAA")], "2026-10-12", run_id="next_day")
    assert desk.verdict_reads == ["AAA", "AAA"]


def test_hold_is_not_rechecked_on_the_same_bar():
    desk = _Desk(verdict=HOLD)
    desk.tick([_position("AAA")], "2026-10-09")
    desk.tick([_position("AAA")], "2026-10-09")
    assert desk.verdict_reads == ["AAA"] and desk.executed == []


def test_refused_sale_is_retried_next_tick():
    desk = _Desk(sells=False)
    first = desk.tick([_position("AAA")], "2026-10-09", run_id="t0945")
    assert first["refused"] == ["AAA"] and desk.markers["AAA"]["outcome"] == OUTCOME_REFUSED
    desk.sells = True
    second = desk.tick([_position("AAA")], "2026-10-09", run_id="t1015")
    assert second["sold"] == ["AAA"] and desk.executed == [["AAA"], ["AAA"]]


@contextmanager
def _lock():
    yield True


def test_trend_runs_before_trail_and_sold_name_is_not_trailed():
    calls: list = []

    def trend(positions, *, run_id, total_value=None):
        calls.append("trend")
        return {"status": "ran", "sold": ["AAA"], "refused": [], "checked": ["AAA", "BBB"]}

    def trail(ready, *, run_id):
        calls.append(("trail", [p.symbol for p in ready]))
        return []

    summary = trail_on_tick(
        positions=[_position("AAA"), _position("BBB")],
        run_id="r",
        apply_deterministic_trails=trail,
        atr_for_symbol=lambda s: 2.0,
        process_lock=_lock,
        blocking_owner_session=lambda: None,
        trend_exit=trend,
    )
    assert calls == ["trend", ("trail", ["BBB"])]
    assert summary["trend_exit"]["sold"] == ["AAA"]


def _wired_pipeline(monkeypatch, completed=date(2026, 10, 9)):
    p = build_pipeline(broker=MagicMock())
    p._record_alignment_reading = MagicMock()
    monkeypatch.setattr("src.trading_calendar.last_completed_bar_date", lambda *a, **k: completed)
    return p


def test_sale_goes_through_the_guarded_review_path(monkeypatch):
    p = _wired_pipeline(monkeypatch)
    p._alignment_exit_for_holding = MagicMock(return_value=EXIT)
    gates = MagicMock(return_value=SKIP)
    monkeypatch.setattr("src.pipeline_exits.midday_pre_gates", gates)
    run = wire_trend_exit(lambda name: getattr(p, name, None))
    result = run([_position("AAA")], run_id="tick-1", total_value=10_000.0)
    gates.assert_called_once()
    assert gates.call_args.args[2] == "SELL" and gates.call_args.args[3] == "AAA"
    # The gate refused it: no order, marked for retry, and the chart was read once (memo).
    assert result["refused"] == ["AAA"] and p._alignment_exit_for_holding.call_count == 1
    assert p.broker.submit_order.call_count == 0
    from src.storage.trades import trend_tick_store

    assert trend_tick_store.get(p.db._trades(), "AAA")["outcome"] == OUTCOME_REFUSED


def test_only_the_completed_daily_bar_is_read(monkeypatch):
    p = _wired_pipeline(monkeypatch, completed=date(2026, 10, 9))
    p.market = MagicMock()
    p.market.get_ohlcv.return_value = []
    run = wire_trend_exit(lambda name: getattr(p, name, None))
    result = run([_position("AAA")], run_id="tick-1", total_value=10_000.0)
    assert result["bar_date"] == "2026-10-09"
    called = {c[0].split(".")[0] for c in p.market.method_calls}
    assert called <= {"get_ohlcv"}, called
    assert trend_exit_tick.OUTCOME_HOLD == "hold" and result["refused"] == [] and result["sold"] == []


def test_trend_failure_is_recorded_durably_and_trail_still_runs(monkeypatch):
    p = _wired_pipeline(monkeypatch)
    p._alignment_exit_cached = MagicMock(side_effect=RuntimeError("feed down"))
    recorded = MagicMock()
    monkeypatch.setattr(trend_exit_tick, "record_site", recorded)
    trailed: list = []
    summary = trail_on_tick(
        positions=[_position("AAA")],
        run_id="r",
        apply_deterministic_trails=lambda ready, run_id: trailed.extend(p.symbol for p in ready) or [],
        atr_for_symbol=lambda s: 2.0,
        process_lock=_lock,
        blocking_owner_session=lambda: None,
        trend_exit=wire_trend_exit(lambda name: getattr(p, name, None)),
    )
    assert summary["trend_exit"]["status"] == "error" and trailed == ["AAA"]
    recorded.assert_called_once()
