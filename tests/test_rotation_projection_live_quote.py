"""The rotation projection credits an exit at the LIVE quote side it fills at.

Every ordinary exit is a plain DAY MARKET order (src/exit_quote.py): a SELL
fills at the bid, a COVER at the ask. The projection used to credit fixed
0.995 / 1.005 cuts of the last price instead. With no live side it refuses,
with a recorded reason, rather than guessing.
"""

from types import SimpleNamespace

import pytest

from src.rotation_projection import (
    ProjectionRefused,
    _projected_post_sale_book,
    _projected_post_sale_cash,
)

MARK = 100.0
BID = 97.25
ASK = 103.40


def _position(symbol, qty):
    return SimpleNamespace(symbol=symbol, qty=qty, current_price=MARK, market_value=qty * MARK)


def _exit(symbol, action):
    return SimpleNamespace(symbol=symbol, action=action, allocation_pct=100.0, entry_price=0.0, stop_loss=0.0)


def test_a_sale_brings_in_qty_times_the_live_bid():
    positions = [_position("OLD", 10.0)]
    quotes = {"OLD": {"bid": BID, "ask": ASK}}
    cash = _projected_post_sale_cash(1_000.0, positions, [_exit("OLD", "SELL")], [], quotes=quotes)
    assert cash == pytest.approx(1_000.0 + 10 * BID)
    _book, equity = _projected_post_sale_book(positions, 50_000.0, [_exit("OLD", "SELL")], [], quotes=quotes)
    assert equity == pytest.approx(50_000.0 - 10 * (MARK - BID))


def test_a_cover_spends_qty_times_the_live_ask():
    positions = [_position("SHRT", -10.0)]
    quotes = {"SHRT": {"bid": BID, "ask": ASK}}
    cash = _projected_post_sale_cash(5_000.0, positions, [], [_exit("SHRT", "COVER")], quotes=quotes)
    assert cash == pytest.approx(5_000.0 - 10 * ASK)
    _book, equity = _projected_post_sale_book(positions, 50_000.0, [], [_exit("SHRT", "COVER")], quotes=quotes)
    assert equity == pytest.approx(50_000.0 - 10 * (ASK - MARK))


@pytest.mark.parametrize(
    ("action", "quote", "side"),
    [
        ("SELL", {"bid": None, "ask": ASK}, "bid"),
        ("COVER", {"bid": BID, "ask": None}, "ask"),
        ("SELL", None, "bid"),
    ],
)
def test_a_missing_quote_side_refuses_with_a_reason(action, quote, side):
    qty = -10.0 if action == "COVER" else 10.0
    positions = [_position("XYZ", qty)]
    sells = [_exit("XYZ", action)] if action == "SELL" else []
    covers = [_exit("XYZ", action)] if action == "COVER" else []
    quotes = {"XYZ": quote} if quote is not None else {}
    for project in (
        lambda: _projected_post_sale_cash(0.0, positions, sells, covers, quotes=quotes),
        lambda: _projected_post_sale_book(positions, 50_000.0, sells, covers, quotes=quotes),
    ):
        with pytest.raises(ProjectionRefused) as refused:
            project()
        assert f"no live {side} for XYZ" in str(refused.value)
