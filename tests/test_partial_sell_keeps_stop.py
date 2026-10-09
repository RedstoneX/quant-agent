"""A trimming exit shrinks its stop in place; the kept shares are never naked.

Against the HoldingBroker fake (tests/fakes/holding_broker.py), which holds
shares behind a resting stop exactly as the sandbox measured: a sell into a
held stop is refused, a whole-share quantity PATCH answers with a new id and
marks the old order replaced, a fractional quantity PATCH is refused.

Offline: no network, no real broker.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.execution.broker import AlpacaBroker
from src.protection.protected_sell import ProtectedSell
from src.protection.sell_finalization import SellFinalization
from src.execution.broker_parts.trim_book import trim_keeps_shares
from src.protection.trim_amend import finalize_trim_amend
from tests.fakes.holding_broker import OLD, SYM, HoldingBroker


class _State:
    def __init__(self):
        self.d = {}

    def get(self, key):
        return self.d.get(key)

    def set(self, key, value):
        self.d[key] = value


def _desk(fake):
    with patch("src.execution.broker.TradingClient", return_value=MagicMock()):
        b = AlpacaBroker(api_key="t", secret_key="t", paper=True)
    b.client = fake
    b._list_open_stop_orders_by_side = fake.by_side
    b._list_open_protective_stop_orders = lambda symbol, side="sell", errors=None: fake.by_side(symbol)[
        0 if side == "sell" else 1
    ]
    b.get_positions = fake.positions
    b._submit_stop_limit_order = fake.submit_stop
    b.wait_for_order_terminal = fake.wait_terminal
    b.cancel_open_entry_orders = lambda symbol: None
    b.submit_order = lambda symbol, qty, side, limit_price, reference_price: fake.sell(symbol, qty, side)
    return b


def _trace(fake):
    """Record the LIVE closing-stop quantity before every broker call."""
    seen: list[tuple[str, float]] = []
    for name in ("replace_order_by_id", "cancel_order_by_id", "sell", "submit_stop", "get_order_by_id"):
        real = getattr(fake, name)

        def wrapped(*a, _real=real, _name=name, **kw):
            seen.append((_name, fake.live_stop_qty()))
            return _real(*a, **kw)

        setattr(fake, name, wrapped)
    return seen


def _sell(fake, b, *, declined=None, write_ahead=None):
    state = _State()
    ps = ProtectedSell(
        broker=b,
        db=MagicMock(),
        alert_owner_exit_declined=lambda symbol, **kw: (declined if declined is not None else []).append(kw),
        order_accepted=lambda order, symbol, side: True,
        write_ahead_protection_restore=write_ahead or (lambda *a, **k: 11),
        state=state,
    )
    return ps, state


def _finalizer(b):
    return SimpleNamespace(
        broker=b,
        db=MagicMock(),
        _cancel_stray_stops_on_flat=MagicMock(),
        _finalize_protection_after_sell=MagicMock(return_value=(True, [])),
    )


@pytest.fixture
def alerts(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr("src.notifier.send_owner_alert", lambda text, **kw: sent.append(text))
    return sent


def _trim(ps, qty, held, label="REDUCE", side="sell"):
    return ps._submit_protected_sell(
        symbol=SYM,
        qty=qty,
        limit_price=None,
        reference_price=50.0,
        position_qty_before_sell=held,
        label=label,
        side=side,
    )


def _cancels(fake):
    return [c[1] for c in fake.calls if c[0] == "cancel"]


def _qty_amends(fake):
    return [(c[1], c[2]["qty"]) for c in fake.calls if c[0] == "replace" and "qty" in c[2]]


def _sells(fake):
    return [c[1] for c in fake.calls if c[0] == "sell"]


# ------------------------------------------------------------ the four trims


def test_the_four_trims_keep_shares_and_full_exits_do_not():
    for label in ("REDUCE", "PARTIAL_SELL", "TAKE_PROFIT", "SWEEP_SELL"):
        assert trim_keeps_shares(label, 10, 4)
        assert not trim_keeps_shares(label, 10, 10)
    for label in ("SELL", "EMERGENCY_SELL", "EMERGENCY_COVER", "FORCE_DELEVER"):
        assert not trim_keeps_shares(label, 10, 4)


def test_whole_share_trim_never_has_zero_stop_at_any_broker_call(alerts):
    fake = HoldingBroker(10.0)
    fake.rest(10)
    seen = _trace(fake)
    b = _desk(fake)
    ps, _ = _sell(fake, b)

    sale = _trim(ps, 4, 10.0)

    assert sale is not None
    order, prot = sale
    assert _qty_amends(fake) == [("o1", 6)]
    assert _cancels(fake) == []
    assert _sells(fake) == [4.0]
    assert min(q for _, q in seen) >= 6.0, seen
    assert fake.orders["o1"].status == "replaced"
    assert prot["kept_leg"]["id"] == "o2" and prot["kept_leg"]["replaced"] == "o1"
    assert prot["specs"] == []
    # the sell was confirmed AFTER the replace landed: the old-order wait and
    # the new-order read both precede the sell
    names = [n for n, _ in seen]
    assert names.index("get_order_by_id") < names.index("sell")

    assert finalize_trim_amend(_finalizer(b), prot) is True
    assert fake.book() == [(6.0, OLD)]
    assert alerts == []


def test_fractional_trim_leaves_only_the_sliver_uncovered(alerts):
    fake = HoldingBroker(10.37)
    fake.rest(10)
    fake.rest(0.37)
    seen = _trace(fake)
    b = _desk(fake)
    ps, _ = _sell(fake, b)

    sale = _trim(ps, 4.2, 10.37)  # keeps 6.17: six whole shares and a 0.17 sliver

    assert sale is not None
    _, prot = sale
    assert _qty_amends(fake) == [("o1", 6)]
    assert _cancels(fake) == ["o2"]  # the sliver only; the GTC leg is never cancelled
    assert [s["id"] for s in prot["specs"]] == ["o2"]
    assert min(q for _, q in seen) >= 6.0, seen  # the whole shares stayed covered throughout
    assert _sells(fake) == [4.2]

    assert finalize_trim_amend(_finalizer(b), prot) is True
    assert fake.book() == [(0.17, OLD), (6.0, OLD)]
    assert alerts == []


def test_patch_refusal_after_a_stop_fired_does_not_sell(alerts):
    fake = HoldingBroker(10.0)
    fake.rest(10)
    fake.refuse_qty_amend = True
    b = _desk(fake)
    b.get_positions = lambda: [SimpleNamespace(symbol=SYM, qty=6.0)]  # the broker now shows fewer shares
    declined: list[dict] = []
    ps, state = _sell(fake, b, declined=declined)

    assert _trim(ps, 4, 10.0) is None
    assert _sells(fake) == []
    assert _cancels(fake) == []
    assert state.get("_last_stop_clear_refusal") == "held_changed"
    assert declined and "may have fired" in declined[0]["why"]


def test_patch_refusal_out_of_hours_refuses_and_cancels_nothing(alerts):
    fake = HoldingBroker(10.0)
    fake.rest(10)
    fake.refuse_qty_amend = True
    fake.get_clock = lambda: SimpleNamespace(is_open=False)
    b = _desk(fake)
    declined: list[dict] = []
    ps, state = _sell(fake, b, declined=declined)

    assert _trim(ps, 4, 10.0) is None
    assert _sells(fake) == []
    assert _cancels(fake) == []
    assert state.get("_last_stop_clear_refusal") == "market_closed"
    assert declined and "market is closed" in declined[0]["why"]
    assert fake.book() == [(10.0, OLD)]


def test_patch_refusal_with_an_unreadable_clock_also_refuses(alerts):
    fake = HoldingBroker(10.0)
    fake.rest(10)
    fake.refuse_qty_amend = True

    def boom():
        raise RuntimeError("clock down")

    fake.get_clock = boom
    b = _desk(fake)
    ps, state = _sell(fake, b, declined=[])

    assert _trim(ps, 4, 10.0) is None
    assert _cancels(fake) == [] and _sells(fake) == []
    assert state.get("_last_stop_clear_refusal") == "market_closed"


def test_patch_refusal_in_regular_hours_falls_back_to_cancel_all(alerts):
    fake = HoldingBroker(10.0)
    fake.rest(10)
    fake.refuse_qty_amend = True
    b = _desk(fake)
    ps, _ = _sell(fake, b)

    sale = _trim(ps, 4, 10.0)

    assert sale is not None
    _, prot = sale
    assert _cancels(fake) == ["o1"]
    assert _sells(fake) == [4.0]
    assert "kept_leg" not in prot
    assert [s["id"] for s in prot["specs"]] == ["o1"]


def test_an_unanswered_patch_sells_nothing_and_cancels_nothing(alerts):
    fake = HoldingBroker(10.0)
    fake.rest(10)
    fake.qty_amend_unknown = True
    b = _desk(fake)
    ps, state = _sell(fake, b, declined=[])

    assert _trim(ps, 4, 10.0) is None
    assert _cancels(fake) == [] and _sells(fake) == []
    assert state.get("_last_stop_clear_refusal") == "amend_unknown"


def test_a_full_exit_is_unchanged_cancel_all(alerts):
    fake = HoldingBroker(10.0)
    fake.rest(10)
    b = _desk(fake)
    ps, _ = _sell(fake, b)

    sale = _trim(ps, 10, 10.0, label="SELL")

    assert sale is not None
    _, prot = sale
    assert _qty_amends(fake) == []
    assert _cancels(fake) == ["o1"]
    assert _sells(fake) == [10.0]
    assert "kept_leg" not in prot


def test_a_short_cover_trim_shrinks_the_buy_stop_in_place(alerts):
    fake = HoldingBroker(-10)
    fake.rest(10)  # the buy-stop covering the short
    seen = _trace(fake)
    b = _desk(fake)
    ps, _ = _sell(fake, b)

    sale = _trim(ps, 4, 10.0, side="buy")

    assert sale is not None
    _, prot = sale
    assert _qty_amends(fake) == [("o1", 6)]
    assert _cancels(fake) == []
    assert [c for c in fake.calls if c[0] == "sell"] == [("sell", 4.0, "buy")]
    assert min(q for _, q in seen) >= 6.0
    assert prot["side"] == "buy" and prot["kept_leg"]["qty"] == 6.0


def test_finalizer_routes_a_kept_leg_to_the_invariant_not_the_restore():
    broker = MagicMock()
    broker.wait_for_order_terminal.return_value = "filled"
    fin = SellFinalization(
        broker=broker,
        db=MagicMock(),
        terminal_order_statuses=frozenset({"filled"}),
        finalize_protection_after_sell=MagicMock(),
        register_exit_settlement=MagicMock(),
        finalize_protection_after_sell_core=MagicMock(),
        cancel_stray_stops_on_flat=MagicMock(),
        current_position_qty_for_finalize=MagicMock(),
        persist_orphaned_protection_restore=MagicMock(),
        reprotect_residual_after_partial_sell=MagicMock(),
        derive_close_side_for_drain=MagicMock(),
    )
    prot = {"order_id": "s1", "symbol": SYM, "position_qty_before_sell": 10.0, "specs": [], "kept_leg": {"id": "o2"}}
    with patch("src.protection.trim_amend.finalize_trim_amend", return_value=True) as routed:
        fin._finalize_pending_protections([prot], context="TEST")
    routed.assert_called_once_with(fin, prot)
    fin._finalize_protection_after_sell.assert_not_called()
    assert prot["coverage_confirmed"] is True
