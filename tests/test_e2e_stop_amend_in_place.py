"""Hermetic end-to-end: a protective stop is MOVED by an in-place amend.

An existing position already rests behind a protective stop. The scripted
reviewer asks to trail it up. The desk must send the broker ONE amend that
carries the new price, aimed at the order that was resting, and must cancel
nothing (cancel-then-resubmit on a false premise is a known root cause: the
broker amends in place). tests/test_e2e_midday_existing_book.py checks the
resting book afterwards; this file checks the amend CALL itself.

NOT COVERED: refused or unknown amend answers, fractional or multi-leg stops.
"""

from __future__ import annotations

from tests.test_e2e_midday_existing_book import BARS, HOUR, SYMBOL, _midday
from tests.test_e2e_close_existing_book import (
    INITIAL_STOP,
    QTY,
    _assert_hermetic,
    _resting_sell_stops,
)


def test_trailing_a_resting_stop_amends_it_in_place_and_cancels_nothing(
    tmp_path,
    monkeypatch,
):
    new_stop = INITIAL_STOP + 2.0
    assert INITIAL_STOP < new_stop < BARS[-1].close
    result, trace, trading, attempts, _ = _midday(
        tmp_path,
        monkeypatch,
        [
            {
                "action": "TRAIL_STOP",
                "symbol": SYMBOL,
                "reason": "lock part of the gain",
                "new_stop_price": new_stop,
            }
        ],
    )
    _assert_hermetic(result, trace, trading, attempts)
    assert trading.cancelled == [], f"a cancel was issued: {trading.cancelled}"
    assert len(trading.amended) == 1, trading.amended
    old_id, new_id, fields = trading.amended[0]
    assert fields["stop_price"] == new_stop, fields
    assert old_id != new_id
    stops = _resting_sell_stops(trading)
    assert [(s.order_id, s.stop_price, s.qty) for s in stops] == [(new_id, new_stop, QTY)], [
        s.as_plain() for s in stops
    ]
