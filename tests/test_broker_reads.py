"""Unit tests for `src/api/broker_reads.py` — the degradation contract.

`tests/test_api_contract.py` exercises the *routes* with this whole module
stubbed out, which is right for a route contract test but left the module
itself at ~28% line coverage: every documented "never raises, degrades to
an `error` field" path was unproven, as was the defensive per-field order
flattening.

That contract is exactly what a credential-gateway outage exercises. When
OneCLI is down, or the runtime's `.env` wiring is wrong, every call here
raises inside the SDK — and Mission Control must answer 200 with an
`error` field rather than 500 with a stack trace that names internal
paths. These tests pin that behaviour directly.

Monkeypatch targets follow the convention documented in
`tests/test_api_contract.py`: patch at the *consuming* module's namespace.
`_get_broker` is `lru_cache`d, so each test that touches it patches the
name rather than the cache.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.api.broker_reads as broker_reads


class _Boom(Exception):
    """Distinct type so a test can tell a real failure from a stray one."""


def _broker(**methods):
    """A stand-in broker whose methods are supplied per test."""
    return SimpleNamespace(**methods)


@pytest.fixture(autouse=True)
def _clear_broker_cache():
    """`_get_broker` is a process-wide lru_cache — never let one test's
    broker leak into the next.

    The original is captured up front because most tests monkeypatch the
    name away, and this fixture's teardown runs while that patch is still
    in place (autouse fixtures are set up first, so they finalize last).
    """
    original = broker_reads._get_broker
    original.cache_clear()
    yield
    original.cache_clear()


# ---------------------------------------------------------------------------
# read_account
# ---------------------------------------------------------------------------

def test_read_account_returns_broker_values(monkeypatch):
    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _broker(
        get_account=lambda: {"cash": 1000.0, "portfolio_value": 25000.0,
                             "last_equity": 24000.0},
    ))
    out = broker_reads.read_account()
    assert out == {"cash": 1000.0, "portfolio_value": 25000.0,
                   "last_equity": 24000.0, "error": None}


def test_read_account_degrades_to_error_when_the_broker_raises(monkeypatch):
    """A gateway outage must produce nulls + an error string, never an
    exception the route layer has to catch."""
    def _raise():
        raise _Boom("gateway unreachable")

    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _broker(get_account=_raise))
    out = broker_reads.read_account()
    assert out["cash"] is None
    assert out["portfolio_value"] is None
    assert out["last_equity"] is None
    assert "gateway unreachable" in out["error"]


def test_read_account_degrades_when_the_broker_cannot_be_constructed(monkeypatch):
    """Construction itself can raise (bad credentials, SDK import failure)."""
    def _raise():
        raise _Boom("cannot build client")

    monkeypatch.setattr(broker_reads, "_get_broker", _raise)
    assert "cannot build client" in broker_reads.read_account()["error"]


def test_read_account_tolerates_a_partial_account_payload(monkeypatch):
    """Missing keys become None rather than KeyError-ing out of the read."""
    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _broker(
        get_account=lambda: {"cash": 1.0},
    ))
    out = broker_reads.read_account()
    assert out["cash"] == 1.0
    assert out["portfolio_value"] is None
    assert out["error"] is None


# ---------------------------------------------------------------------------
# read_positions
# ---------------------------------------------------------------------------

def _position(**overrides):
    base = dict(symbol="NVDA", qty=10.0, avg_entry=100.0, current_price=110.0,
                market_value=1100.0, unrealized_pnl=100.0,
                unrealized_intraday_pnl=5.0, sector="Technology")
    base.update(overrides)
    return SimpleNamespace(**base)


def test_read_positions_flattens_position_objects(monkeypatch):
    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _broker(
        get_positions=lambda: [_position()],
    ))
    out = broker_reads.read_positions()
    assert out["error"] is None
    assert out["positions"] == [{
        "symbol": "NVDA", "qty": 10.0, "avg_entry": 100.0,
        "current_price": 110.0, "market_value": 1100.0,
        "unrealized_pnl": 100.0, "unrealized_intraday_pnl": 5.0,
        "sector": "Technology",
    }]


def test_read_positions_defaults_optional_fields_to_none(monkeypatch):
    """`unrealized_intraday_pnl`/`sector` are read with getattr defaults —
    an older Position shape must not break the read."""
    bare = SimpleNamespace(symbol="SPY", qty=1.0, avg_entry=1.0,
                           current_price=1.0, market_value=1.0,
                           unrealized_pnl=0.0)
    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _broker(
        get_positions=lambda: [bare],
    ))
    row = broker_reads.read_positions()["positions"][0]
    assert row["unrealized_intraday_pnl"] is None
    assert row["sector"] is None


def test_read_positions_degrades_to_empty_list_with_error(monkeypatch):
    def _raise():
        raise _Boom("positions unavailable")

    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _broker(get_positions=_raise))
    out = broker_reads.read_positions()
    assert out["positions"] == []
    assert "positions unavailable" in out["error"]


# ---------------------------------------------------------------------------
# _order_to_dict — defensive per-field flattening
# ---------------------------------------------------------------------------

def _order(**overrides):
    base = dict(
        id="ord-1", symbol="AAPL",
        side=SimpleNamespace(value="BUY"),
        qty="10",
        order_type=SimpleNamespace(value="LIMIT"),
        status=SimpleNamespace(value="FILLED"),
        limit_price="150.5", stop_price=None,
        filled_qty="10", filled_avg_price="150.4",
        submitted_at=SimpleNamespace(isoformat=lambda: "2026-08-12T13:30:00+00:00"),
        filled_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_order_to_dict_unwraps_enums_and_coerces_numbers():
    out = broker_reads._order_to_dict(_order())
    assert out["id"] == "ord-1"
    assert out["side"] == "buy"          # enum .value, lowercased
    assert out["order_type"] == "limit"
    assert out["status"] == "filled"
    assert out["qty"] == 10.0            # str -> float
    assert out["limit_price"] == 150.5
    assert out["stop_price"] is None
    assert out["submitted_at"] == "2026-08-12T13:30:00+00:00"
    assert out["filled_at"] is None


def test_order_to_dict_falls_back_to_the_type_attribute():
    """Older SDK shapes expose `type` rather than `order_type`."""
    o = _order(order_type=None)
    o.type = SimpleNamespace(value="MARKET")
    assert broker_reads._order_to_dict(o)["order_type"] == "market"


def test_order_to_dict_degrades_one_bad_field_without_losing_the_row():
    """A single unparseable field must become None, not abort the order."""
    out = broker_reads._order_to_dict(_order(qty="not-a-number"))
    assert out["qty"] is None
    assert out["symbol"] == "AAPL"       # the rest of the row survives


def test_order_to_dict_survives_a_property_that_raises():
    """`_extract_order_field` exists so an SDK attribute that raises on
    access degrades to None instead of taking down the whole response."""
    class _Exploding:
        id = "ord-2"
        symbol = "MSFT"

        @property
        def qty(self):
            raise _Boom("attribute access blew up")

        def __getattr__(self, name):
            return None

    out = broker_reads._order_to_dict(_Exploding())
    assert out["id"] == "ord-2"
    assert out["qty"] is None


def test_order_to_dict_handles_a_non_isoformat_timestamp():
    out = broker_reads._order_to_dict(_order(submitted_at="2026-08-12 13:30:00"))
    assert out["submitted_at"] == "2026-08-12 13:30:00"


# ---------------------------------------------------------------------------
# read_orders
# ---------------------------------------------------------------------------

def _orders_broker(orders, capture=None):
    def _get_orders(filter=None):  # noqa: A002 — mirrors the SDK's kwarg name
        if capture is not None:
            capture.append(filter)
        return orders

    return _broker(client=SimpleNamespace(get_orders=_get_orders))


def test_read_orders_returns_flattened_rows(monkeypatch):
    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _orders_broker([_order()]))
    out = broker_reads.read_orders()
    assert out["error"] is None
    assert out["orders"][0]["symbol"] == "AAPL"


@pytest.mark.parametrize("requested,expected", [
    ("open", "OPEN"), ("closed", "CLOSED"), ("all", "ALL"),
    ("OPEN", "OPEN"),
    # Unrecognized values fall back to OPEN rather than raising — route-level
    # validation is expected to have rejected them already.
    ("garbage", "OPEN"), ("", "OPEN"), (None, "OPEN"),
])
def test_read_orders_maps_status_safely(monkeypatch, requested, expected):
    from alpaca.trading.enums import QueryOrderStatus

    captured: list = []
    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _orders_broker([], captured))
    broker_reads.read_orders(status=requested)
    assert captured[0].status == getattr(QueryOrderStatus, expected)


def test_read_orders_passes_the_limit_through(monkeypatch):
    captured: list = []
    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _orders_broker([], captured))
    broker_reads.read_orders(limit=7)
    assert captured[0].limit == 7


def test_read_orders_degrades_a_wholly_broken_row_to_nulls(monkeypatch):
    """An order object whose every attribute access raises still yields a
    row — of `None`s — because `_order_to_dict` guards each field
    individually. The neighbouring orders are unaffected, which is the
    property that matters."""
    class _Unflattenable:
        def __getattr__(self, name):
            raise _Boom("this whole object is broken")

    monkeypatch.setattr(
        broker_reads, "_get_broker",
        lambda: _orders_broker([_order(), _Unflattenable(), _order(id="ord-3")]),
    )
    out = broker_reads.read_orders()
    assert out["error"] is None
    assert [o["id"] for o in out["orders"]] == ["ord-1", None, "ord-3"]


def test_read_orders_skips_a_row_whose_flattening_raises(monkeypatch):
    """The row-level guard in `read_orders` is the belt behind
    `_order_to_dict`'s per-field braces: if flattening ever raises as a
    whole, that one order is dropped and the rest still return."""
    real = broker_reads._order_to_dict

    def _flaky(o):
        if getattr(o, "id", None) == "ord-2":
            raise _Boom("flattening blew up")
        return real(o)

    monkeypatch.setattr(broker_reads, "_order_to_dict", _flaky)
    monkeypatch.setattr(
        broker_reads, "_get_broker",
        lambda: _orders_broker([_order(), _order(id="ord-2"), _order(id="ord-3")]),
    )
    out = broker_reads.read_orders()
    assert out["error"] is None
    assert [o["id"] for o in out["orders"]] == ["ord-1", "ord-3"]


def test_read_orders_tolerates_a_none_response(monkeypatch):
    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _orders_broker(None))
    assert broker_reads.read_orders() == {"orders": [], "error": None}


def test_read_orders_degrades_when_the_query_raises(monkeypatch):
    def _raise(filter=None):  # noqa: A002
        raise _Boom("orders query failed")

    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _broker(
        client=SimpleNamespace(get_orders=_raise),
    ))
    out = broker_reads.read_orders()
    assert out["orders"] == []
    assert "orders query failed" in out["error"]


# ---------------------------------------------------------------------------
# _get_broker — construction is narrow by design
# ---------------------------------------------------------------------------

def test_get_broker_builds_from_the_narrow_accessors_only(monkeypatch):
    """The singleton must be built from `get_alpaca_credentials()` /
    `get_alpaca_paper()` — never from the full `AppConfig`, which carries
    every plaintext key on the system (see the module docstring)."""
    built: dict = {}

    def _fake_broker(api_key, secret_key, paper):
        built.update(api_key=api_key, secret_key=secret_key, paper=paper)
        return _broker()

    monkeypatch.setattr(broker_reads, "get_alpaca_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(broker_reads, "get_alpaca_paper", lambda: True)
    monkeypatch.setattr(broker_reads, "AlpacaBroker", _fake_broker)

    broker_reads._get_broker()
    assert built == {"api_key": "k", "secret_key": "s", "paper": True}


def test_get_broker_is_cached(monkeypatch):
    """One client per process — a fresh SDK client per read would multiply
    connections against the broker (and the credential gateway)."""
    calls = []

    def _fake_broker(api_key, secret_key, paper):
        calls.append(1)
        return _broker()

    monkeypatch.setattr(broker_reads, "get_alpaca_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(broker_reads, "get_alpaca_paper", lambda: True)
    monkeypatch.setattr(broker_reads, "AlpacaBroker", _fake_broker)

    broker_reads._get_broker()
    broker_reads._get_broker()
    assert len(calls) == 1


def test_check_broker_reachable_true_on_a_successful_account_call(monkeypatch):
    """The healthy leg — the two failure legs live in test_api_contract.py."""
    monkeypatch.setattr(broker_reads, "get_alpaca_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _broker(
        get_account=lambda: {"cash": 0.0},
    ))
    assert broker_reads.check_broker_reachable() is True


def test_check_broker_reachable_is_none_when_credentials_cannot_be_read(monkeypatch):
    """A config that won't load must read as "not configured", not as a
    crash — this is the state before the runtime `.env` wiring exists."""
    def _raise():
        raise _Boom("config unreadable")

    monkeypatch.setattr(broker_reads, "get_alpaca_credentials", _raise)
    assert broker_reads.check_broker_reachable() is None
