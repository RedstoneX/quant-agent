"""by_conviction is a per-POSITION record keyed on the OPENING entry row."""

from src.storage.analytics.calibration import conviction_positions


def _row(sym, action, qty, price, conv=None, pnl=None, ts="2026-10-01 10:00:00"):
    return {
        "symbol": sym,
        "action": action,
        "qty": qty,
        "price": price,
        "fill_qty": qty,
        "fill_price": price,
        "fill_status": "filled",
        "conviction": conv,
        "realized_pnl": pnl,
        "timestamp": ts,
    }


def _run(rows):
    return conviction_positions(rows, lookback_days=36500)


def test_opening_row_conviction_wins_over_add():
    rows = [
        _row("A", "BUY", 10, 10, "high", ts="2026-10-01 10:00:00"),
        _row("A", "BUY", 10, 12, "low", ts="2026-10-02 10:00:00"),
        _row("A", "SELL", 20, 13, pnl=50.0, ts="2026-10-03 10:00:00"),
    ]
    out = _run(rows)
    assert [(p["conviction"], p["pnl_known"], p["pnl"]) for p in out] == [("high", True, 50.0)]


def test_partial_close_not_counted():
    rows = [
        _row("A", "BUY", 10, 10, "high", ts="2026-10-01 10:00:00"),
        _row("A", "SELL", 4, 11, pnl=4.0, ts="2026-10-02 10:00:00"),
    ]
    assert _run(rows) == []


def test_missing_pnl_is_unknown_not_zero():
    rows = [
        _row("A", "BUY", 10, 10, "medium", ts="2026-10-01 10:00:00"),
        _row("A", "SELL", 10, 11, pnl=None, ts="2026-10-02 10:00:00"),
    ]
    out = _run(rows)
    assert len(out) == 1
    assert out[0]["pnl_known"] is False


def test_short_uses_opening_short_row():
    rows = [
        _row("S", "SHORT", 5, 20, "low", ts="2026-10-01 10:00:00"),
        _row("S", "COVER", 5, 18, pnl=10.0, ts="2026-10-02 10:00:00"),
    ]
    out = _run(rows)
    assert (out[0]["side"], out[0]["conviction"], out[0]["pnl"]) == ("short", "low", 10.0)
