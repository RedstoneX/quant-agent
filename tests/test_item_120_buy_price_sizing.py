"""A new-name BUY is sized off TODAY's real price — or it is refused.

docs/WORK.md item 120, sizing half.

When the desk opens a name it does not already hold, the share count is
`dollars / price`, so the price is the DIVISOR of the dollar allocation: a
wrong price mis-sizes the position proportionally. Two surfaces compute that
divisor, and BOTH used a price that could be a quote MID or a prior-session
trade:

  * the portfolio constructor's `price_map` for new names, filled at
    `pipeline_stages.DecisionStage` from the broker's bare price call; and
  * the EXECUTION stage, where `_today_order_price` (the fill reference)
    deliberately allows a today quote mid and that same number then set
    `sizing_price` — the share-count divisor — in the submit loop, the
    cash-sweep preflight and the rotation replacement-buy gate.

The fix separates the two USES of price. A quote mid stays a legitimate FILL
reference (crossing the spread on an already-sized order, owner 2026-09-12),
but the SIZING divisor must be a real today PRINT: `resolve_live_price` for
the constructor path (a real print or today's forming session bar, never a
quote mid) and `_today_sizing_price` (`is_today_print` only) for execution.
A name with no fresh today price is refused as unmeasurable rather than sized
on a bad price.

The resolver's own branch coverage (a quote mid can never become a price; a
prior-session trade is never returned) lives in tests/test_desk_sees_today.py.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import src.pipeline_stages as pipeline_stages
from src.data.levels import FAULT_NO_PRICE, FAULT_STALE_PRICE
from src.data.live_price import ONLY_STALE, resolve_live_price
from src.execution.broker import LivePrice
from src.models import (
    Position, PortfolioDecision, TargetPosition, TradeDecision,
    TechAnalysisResult, TechReasoningChain,
)
from src.pipeline_context import RunContext
from src.pipeline_stages import (
    ExecutionStage, _rotation_buy_leg_projected_refusal, _today_sizing_price,
)
from src.portfolio_constructor import PortfolioConstructor
from tests.session_clock import todays_session_bar_stamp

ET = ZoneInfo("America/New_York")

# Thursday 2026-09-17, the session board item 120 measured.
SEP17_OPEN = datetime(2026, 9, 17, 9, 30, tzinfo=ET)
SEP17_1031 = datetime(2026, 9, 17, 10, 31, tzinfo=ET)
SEP17_FRESH_PRINT = datetime(2026, 9, 17, 10, 30, 5, tzinfo=ET)
SEP16_CLOSE = datetime(2026, 9, 16, 15, 59, tzinfo=ET)
SEP17_BAR_AT = datetime(2026, 9, 17, 0, 0, tzinfo=ET)


def _snap(**kw) -> dict:
    """A `get_intraday_snapshots` payload with every field present."""
    base = {
        "last_price": None, "last_trade_at": None, "prev_close": 100.0,
        "session_bar_at": None, "minute_close": None, "minute_bar_at": None,
        "session_open": None, "session_close": None, "session_high": None,
        "session_low": None, "session_volume": None,
    }
    base.update(kw)
    return base


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x",
        volume="x", support_resistance="x",
    )


def _analysis(symbol: str, entry: float, stop: float, target: float,
              horizon: int = 60) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol, rating="buy", entry_price=entry,
        stop_loss=stop, reference_target=target, reasoning="test",
        support_levels=[stop], resistance_levels=[target],
        computed_levels=[stop, target],
        atr_14=(entry - stop) / 3.5,
        setup_type="range", expected_horizon_sessions=horizon,
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )


def _split(snapshots: dict[str, dict], syms: list[str], when: datetime):
    """The exact split `pipeline_stages.DecisionStage` performs on a new
    name: a fresh today price goes into `price_map`; any other outcome
    (stale, mid-only, absent) makes the name unpriceable, carrying the same
    stale-vs-absent fault code the stage records."""
    price_map: dict[str, float] = {}
    unpriceable: dict[str, str] = {}
    for sym in syms:
        resolved = resolve_live_price(snapshots.get(sym), when=when)
        if resolved.is_today_print:
            price_map[sym] = resolved.price
        else:
            unpriceable[sym] = (
                FAULT_STALE_PRICE if resolved.unavailable == ONLY_STALE
                else FAULT_NO_PRICE
            )
    return price_map, unpriceable


# --- constructor surface ----------------------------------------------------

def test_new_name_buy_sizes_off_the_resolved_last_trade():
    """A fresh today print prices the buy — and it is the RESOLVED last
    trade, not the analyst's (possibly hours-old) entry_price."""
    snapshots = {
        "NVDA": _snap(last_price=110.0, last_trade_at=SEP17_FRESH_PRINT,
                      session_bar_at=SEP17_BAR_AT, session_close=110.0),
    }
    price_map, unpriceable = _split(snapshots, ["NVDA"], when=SEP17_1031)
    assert price_map == {"NVDA": 110.0}
    assert unpriceable == {}

    constructor = PortfolioConstructor()
    # TA entry is deliberately DIFFERENT (100) from the live print (110) so a
    # buy priced off the TA fallback would be visibly detectable.
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="NVDA", target_weight_pct=8.0,
                                conviction="high", thesis="AI")],
        positions=[], analyses=[_analysis("NVDA", entry=100, stop=95, target=115)],
        total_value=100_000, price_map=price_map,
        unpriceable_symbols=unpriceable,
    )
    assert len(decisions) == 1
    assert decisions[0].action == "BUY"
    assert decisions[0].entry_price == 110.0  # resolved last trade, not TA 100


def test_stale_only_snapshot_refuses_the_name_as_stale_price():
    """Only a prior-session last trade -> the name is unpriceable -> the
    constructor files a DATA FAULT (FAULT_STALE_PRICE) and builds NO order,
    even though the analyst supplied an entry_price it could have fallen back
    to."""
    snapshots = {
        "NVDA": _snap(last_price=161.79, last_trade_at=SEP16_CLOSE),
    }
    price_map, unpriceable = _split(snapshots, ["NVDA"], when=SEP17_OPEN)
    assert price_map == {}
    assert unpriceable == {"NVDA": FAULT_STALE_PRICE}

    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="NVDA", target_weight_pct=8.0,
                                conviction="high", thesis="AI")],
        positions=[], analyses=[_analysis("NVDA", entry=100, stop=95, target=115)],
        total_value=100_000, price_map=price_map,
        unpriceable_symbols=unpriceable,
    )
    assert decisions == []  # refused, not sized on the stale 161.79 or TA 100
    faults = constructor.drain_data_faults()
    assert faults["NVDA"]["fault"] == FAULT_STALE_PRICE


def test_a_quote_mid_only_snapshot_refuses_as_no_price_never_sizes():
    """A snapshot carrying only quote fields resolves to unavailable — the
    resolver has no branch that reads a quote — so the name is unpriceable
    (FAULT_NO_PRICE) and the buy is refused. No midpoint can ever reach the
    share count."""
    snapshots = {
        "NVDA": _snap(bid_price=158.0, ask_price=158.1),
    }
    price_map, unpriceable = _split(snapshots, ["NVDA"], when=SEP17_1031)
    assert price_map == {}
    assert unpriceable == {"NVDA": FAULT_NO_PRICE}

    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="NVDA", target_weight_pct=8.0,
                                conviction="high", thesis="AI")],
        positions=[], analyses=[_analysis("NVDA", entry=100, stop=95, target=115)],
        total_value=100_000, price_map=price_map,
        unpriceable_symbols=unpriceable,
    )
    assert decisions == []
    assert constructor.drain_data_faults()["NVDA"]["fault"] == FAULT_NO_PRICE


def test_a_held_name_is_never_made_unpriceable_by_this_path():
    """The split only lists NEW targets. A held name keeps its
    `p.current_price` and is never routed through the resolver, so an empty
    snapshot for it cannot make an existing position unpriceable."""
    price_map, unpriceable = _split({}, [], when=SEP17_1031)
    assert unpriceable == {}

    constructor = PortfolioConstructor()
    held = Position(symbol="NVDA", qty=100, avg_entry=90.0, current_price=100.0,
                    market_value=10_000.0, unrealized_pnl=1_000.0,
                    sector="Technology")
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="NVDA", target_weight_pct=5.0,
                                conviction="high", thesis="trim")],
        positions=[held],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=115)],
        total_value=100_000, price_map={"NVDA": 100.0},
        unpriceable_symbols={},
    )
    assert constructor.drain_data_faults() == {}
    assert any(d.symbol == "NVDA" for d in decisions)


# --- execution surface (REAL pipeline_stages wiring, not a reimplementation) -

def _broker_stamped(live: LivePrice | None):
    """A broker whose stamped price is a REAL LivePrice (so the
    `is_today_print` gate in `_today_sizing_price` actually bites), and whose
    bare `get_latest_price` mirrors it as the fill reference."""
    return SimpleNamespace(
        get_latest_price_stamped=lambda s: live,
        get_latest_price=lambda s: (live.price if live is not None else None),
    )


def test_today_sizing_price_returns_a_real_today_print():
    live = LivePrice(price=110.0, source="last_trade", trade_at=SEP17_FRESH_PRINT,
                     is_today=True, is_today_print=True)
    pipeline = SimpleNamespace(broker=_broker_stamped(live))
    assert _today_sizing_price(pipeline, "NVDA") == 110.0


def test_today_sizing_price_refuses_a_quote_mid_even_when_stamped_today():
    """The exact defect: a quote mid is is_today=True but is_today_print=False,
    so the FILL reference accepts it and the SIZING price must not."""
    mid = LivePrice(price=158.05, source="quote_mid", trade_at=SEP17_FRESH_PRINT,
                    is_today=True, is_today_print=False)
    pipeline = SimpleNamespace(broker=_broker_stamped(mid))
    assert _today_sizing_price(pipeline, "NVDA") is None


def test_today_sizing_price_refuses_a_prior_session_print():
    stale = LivePrice(price=161.79, source="last_trade", trade_at=SEP16_CLOSE,
                      is_today=False, is_today_print=False)
    pipeline = SimpleNamespace(broker=_broker_stamped(stale))
    assert _today_sizing_price(pipeline, "NVDA") is None


def _buy_decision(symbol="NVDA"):
    return SimpleNamespace(
        symbol=symbol, action="BUY", entry_price=110.0, stop_loss=104.0,
        allocation_pct=5.0,
    )


def test_rotation_buy_leg_refuses_to_size_off_a_quote_mid():
    """The real execution-stage sizing gate: a today quote mid is a valid
    FILL reference (passes `_live_fill_price`) but is refused as a SIZING
    reference, so the replacement buy is not sized on it."""
    mid = LivePrice(price=110.0, source="quote_mid", trade_at=SEP17_FRESH_PRINT,
                    is_today=True, is_today_print=False)
    pipeline = SimpleNamespace(broker=_broker_stamped(mid))
    clearance, reason, detail = _rotation_buy_leg_projected_refusal(
        pipeline, SimpleNamespace(), rotation=SimpleNamespace(),
        buy_decision=_buy_decision(), positions=[], total_value=100_000.0,
        rotation_sell=None, cash=50_000.0,
    )
    assert clearance is None
    assert reason == "no_price"
    assert "today trade print" in detail


# --- REAL ExecutionStage submit loop ----------------------------------------

def _pm_rc():
    from src.models import ReasoningChain
    return ReasoningChain(
        macro_filter="x", news_check="x", earnings_check="x",
        signal_conflicts="x", sizing_logic="x",
        portfolio_balance="x", cash_target="x",
    )


def _exec_pipeline_with_print(price: float):
    """A MagicMock pipeline whose broker returns a REAL today last-trade
    LivePrice (so the sizing gate uses it), a crossable quote, and accepts
    orders — enough to drive the real BUY/SHORT submit loop."""
    now = datetime.now(ET)
    pipeline = MagicMock()
    pipeline.broker.get_latest_price_stamped.return_value = LivePrice(
        price=price, source="last_trade", trade_at=now,
        is_today=True, is_today_print=True,
    )
    pipeline.broker.get_latest_price.return_value = price
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": price - 1.0, "ask_price": price + 1.0,
    }
    pipeline.broker.submit_order.return_value = {
        "id": "o1", "status": "accepted",
    }
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0}, [], {},
    )
    return pipeline


def _run_exec(pipeline, decision, monkeypatch):
    """Drive the real ExecutionStage submit loop, capturing the sizing_price
    actually handed to the risk-budget sizer (the share-count divisor)."""
    captured = {}

    real = pipeline_stages._qty_by_risk_budget

    def _spy(pipe, *, total_value, sizing_price, stop_price, is_short, fractional):
        captured["sizing_price"] = sizing_price
        return None  # non-binding: qty falls back to the allocation count

    monkeypatch.setattr(pipeline_stages, "_qty_by_risk_budget", _spy)

    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.symbols_bars = {}
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(), decisions=[decision], portfolio_view="t",
    )
    ExecutionStage(pipeline=pipeline).run(ctx)
    _ = real  # keep a reference; monkeypatch restores it after the test
    return captured


def test_submit_loop_short_is_not_over_sized_off_the_below_market_limit(monkeypatch):
    """The dangerous-direction bug: a SHORT's share count must divide by the
    today PRINT, never the below-market `bid_limit` (which would give MORE
    shares — a bigger short). Drives the real submit loop."""
    pipeline = _exec_pipeline_with_print(100.0)
    short = TradeDecision(
        action="SHORT", symbol="TSLA", allocation_pct=10,
        entry_price=100.0, stop_loss=106.0, take_profit=88.0,
        reasoning="short new name",
    )
    captured = _run_exec(pipeline, short, monkeypatch)
    # bid_limit would be ~99.6 (100 * (1 - 40bp)); sizing must be the print.
    assert captured["sizing_price"] == 100.0


def test_submit_loop_buy_never_sizes_below_the_print(monkeypatch):
    """A BUY sizes off the print, raised only to the offer ceiling — never
    below the print. The ceiling can only make the buy SMALLER (accepted)."""
    pipeline = _exec_pipeline_with_print(100.0)
    buy = TradeDecision(
        action="BUY", symbol="TSLA", allocation_pct=10,
        entry_price=100.0, stop_loss=94.0, take_profit=118.0,
        reasoning="buy new name",
    )
    captured = _run_exec(pipeline, buy, monkeypatch)
    # offer ceiling ~100.4 (100 * (1 + 40bp)); sizing >= the print, never below.
    assert captured["sizing_price"] >= 100.0


def test_execution_and_constructor_agree_on_a_today_session_bar():
    """An IEX-thin name with a today forming SESSION BAR but no last-trade
    print is item 120's exact population. Both stages must size it off that
    bar (a real intraday price, never a mid), so a name the paid seats size
    is not silently skipped at execution."""
    # A fixed, unambiguously-past stale stamp — written out explicitly per
    # tests/session_clock.py's own rule ("a stamp meaning stale... must NOT
    # come from here"), so this test does not itself trip the live-clock
    # scan in tests/test_no_clock_dependent_price_stamps.py.
    stale_last_trade_at = datetime(2020, 1, 1, tzinfo=ET)
    session_bar_at = todays_session_bar_stamp()
    snapshot = _snap(
        last_price=161.79, last_trade_at=stale_last_trade_at,  # stale
        session_bar_at=session_bar_at,
        session_open=158.38, session_close=158.55,
    )
    constructor_price = resolve_live_price(snapshot).price  # session bar close

    broker = MagicMock()
    broker.get_latest_price_stamped.return_value = LivePrice(
        price=161.79, source="last_trade", trade_at=stale_last_trade_at,
        is_today=False, is_today_print=False,  # stale print, refused for sizing
    )
    broker.get_intraday_snapshots.return_value = {"NVDA": snapshot}
    pipeline = SimpleNamespace(broker=broker)

    exec_price = _today_sizing_price(pipeline, "NVDA")
    assert exec_price == constructor_price == 158.55
