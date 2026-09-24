"""Cash-sweep plumbing: fund what will be SPENT, and never place a token order.

The owner's decision is that the sweep STAYS — a sell-before-buy is instant
and near-frictionless, so parking idle cash in SGOV is worth the round trip
*when the round trip buys something*. These tests pin the three ways the
plumbing made it buy nothing.

1. OVER-FUNDING. The sweep preflight sized the funding sale off
   `allocation_pct`; the submit loop then spent `min(alloc, risk_budget)`.
   Whenever the §11.1 vol-adjusted budget bound — the ordinary case — the
   difference was liquidated out of the yield vehicle and re-parked by the
   session bookend minutes later. Production ledger:

       2026-08-27 13:35:43  SWEEP_SELL  $3,422.61
       2026-08-27 13:36:36  SWEEP_BUY   $1,007.60     (53 seconds later)
       2026-08-31 19:21:58  SWEEP_SELL    $503.47
       2026-08-31 19:22:03  SWEEP_BUY     $806.40     (5 seconds later)

   Two crossings of the spread for no position. A SHORT's notional was
   folded into the same total even though a short never draws on
   `available_cash` — that funding is waste by construction.

2. TOKEN ORDERS. The execution-time cash clamp is the LAST resize. Fixed
   2026-09-24: it used to refuse a resize under the flat $500
   `min_order_usd` floor — an arbitrary number, not a broker minimum, and
   Alpaca charges no stock commission — so a real, small residual trade was
   refused as "too small to bother". With fractional sizing on, a $3.11
   residue now buys 0.0311 shares and the order goes out, same as a whole-
   share residue under the old floor now places its whole shares instead of
   being refused.

3. UNCONFIRMED PROCEEDS. `fund_buys` returns 0.0 when it cannot confirm the
   cash landed, but it has already refreshed `ctx` from the broker. The
   caller adopted that refresh only on the success path, so an unconfirmed
   attempt left the BUY loop clamping against a PRE-SALE cash reading.
"""
import pytest
from unittest.mock import MagicMock

from src.execution.cash_sweep import CashSweeper
from src.models import PortfolioDecision, ReasoningChain, TradeDecision
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage


def _rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m", news_check="n", earnings_check="e",
        signal_conflicts="s", sizing_logic="z", portfolio_balance="b",
        cash_target="c",
    )


def _pipeline(live_price=100.0, cash=50_000.0, *, fractional=False,
              min_order_usd=500.0):
    """ExecutionStage harness. Config stays a MagicMock (the stage reads many
    attributes); only the leaves these tests depend on are pinned to real
    values, because a MagicMock leaf silently reads as "not a number"."""
    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = live_price
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": cash, "portfolio_value": 100_000.0}, [], {},
    )
    pipeline.config.cash_sweep.min_order_usd = min_order_usd
    pipeline.config.execution.fractional_enabled = fractional
    pipeline.config.execution.fractional_share_decimals = 4
    pipeline.broker.get_fractionability.return_value = (
        {"fractionable": True} if fractional else {"fractionable": False}
    )
    return pipeline


def _ctx(decisions, cash=50_000.0) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = cash
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.decision_id = "run-x-dec-abc123"
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(), decisions=decisions, portfolio_view="t",
    )
    ctx.symbols_bars = {}
    return ctx


def _install_sweeper(pipeline, *, freed=0.0, on_call=None):
    """Install a REAL CashSweeper (the call site isinstance-checks it) whose
    `fund_buys` is replaced by a spy. Returns the recording dict."""
    sweeper = CashSweeper(pipeline=pipeline)
    recorded: dict = {"calls": []}

    def _spy(ctx, planned_notional):
        recorded["calls"].append(planned_notional)
        recorded["planned"] = planned_notional
        if on_call is not None:
            on_call(ctx)
        return freed

    sweeper.fund_buys = _spy
    pipeline._sweeper = lambda: sweeper
    return recorded


def _events(pipeline) -> list[tuple]:
    """(stage, outcome, reason) for every persisted pipeline_event."""
    out = []
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        payload = call.kwargs.get("evidence_json") or ""
        out.append((call.kwargs.get("kind"), payload))
    return out


# ---------------------------------------------------------------------------
# Defect 1 — funding is sized to what will be SPENT, not what was planned.
# ---------------------------------------------------------------------------


def test_funding_is_capped_by_the_risk_budget_not_the_allocation():
    """20% of a $100k book is $20,000 of allocation. The §11.1 risk budget
    (item 22 fix: the ratified 5% of equity = $5,000, not the stale 0.5%)
    against a $50/share stop distance allows 100 shares = $10,000. The
    submit loop spends $10,000, so the sweep must liquidate $10,000 — not
    $20,000 with $10,000 re-parked minutes later. `pipeline` is a MagicMock
    here, so `config.risk.max_position_risk_pct` is an unset Mock attribute
    and `_qty_by_risk_budget` falls back to the ratified 5.0 default."""
    pipeline = _pipeline(live_price=100.0, cash=50_000.0)
    recorded = _install_sweeper(pipeline)
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=20,
        entry_price=100.0, stop_loss=50.0, take_profit=175.0,
        reasoning="risk budget binds",
    )])

    ExecutionStage(pipeline=pipeline).run(ctx)

    assert recorded["planned"] == 10_000.0, (
        "funding must cover the risk-capped size the submit loop will spend"
    )


def test_funding_still_covers_the_allocation_when_risk_does_not_bind():
    """The cap is a MIN, not a replacement. A tight stop leaves the risk
    budget slack, and funding must still cover the full allocation or the
    BUY it was meant to fund gets clamped for want of cash."""
    pipeline = _pipeline(live_price=100.0, cash=50_000.0)
    recorded = _install_sweeper(pipeline)
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=5,
        entry_price=100.0, stop_loss=99.0, take_profit=110.0,
        reasoning="allocation binds",
    )])

    ExecutionStage(pipeline=pipeline).run(ctx)

    # alloc: 5% of $100k = $5,000 = 50 sh. risk: $500 / $1 = 500 sh. Min = 50.
    assert recorded["planned"] == 5_000.0


@pytest.mark.parametrize("entry,stop,alloc", [
    (105.0, 95.0, 20),    # entry ABOVE market — loop sizes at max(mkt, entry)
    (95.0, 90.0, 20),     # entry BELOW market — limit is raised to market
    (100.0, 99.5, 20),    # tight stop — the allocation binds, not the budget
    (100.0, 80.0, 3),     # wide stop, small allocation
])
def test_funding_is_never_less_than_what_the_loop_spends(entry, stop, alloc):
    """The invariant the whole fix rests on: the sweep may over-sell, but it
    must never sell LESS than the BUY loop is about to spend. Under-funding
    costs a trade; over-funding costs a spread.

    It holds because the preflight prices the risk cap at the loop's own
    reference — `max(market, entry)` for a long — and every later adjustment
    (marketable-limit ceiling, ATR stop floor) only shrinks the quantity.
    """
    pipeline = _pipeline(live_price=100.0, cash=50_000.0)
    recorded = _install_sweeper(pipeline)
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=alloc,
        entry_price=entry, stop_loss=stop, take_profit=entry * 1.4,
        reasoning="funding must cover this",
    )])

    ExecutionStage(pipeline=pipeline).run(ctx)

    assert pipeline.broker.submit_order.called, "scenario must reach a submit"
    submitted = pipeline.broker.submit_order.call_args.kwargs
    spent = submitted["qty"] * max(100.0, entry)
    assert recorded["planned"] >= spent - 0.01, (
        f"funded ${recorded['planned']:,.2f} but the loop spent "
        f"${spent:,.2f} — the sweep would under-fund its own BUY"
    )


def test_shorts_are_excluded_from_the_funding_total():
    """A SHORT sells borrowed shares and never draws on `available_cash`
    (D11 in the submit loop). Liquidating SGOV to fund one raises cash no
    order can spend — guaranteed churn, not a safety margin."""
    pipeline = _pipeline(live_price=100.0, cash=50_000.0)
    recorded = _install_sweeper(pipeline)
    pipeline.broker.get_shortability.return_value = {
        "shortable": True, "easy_to_borrow": True,
    }
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx([
        TradeDecision(action="BUY", symbol="XLE", allocation_pct=5,
                      entry_price=100.0, stop_loss=99.0, take_profit=110.0,
                      reasoning="real cash spend"),
        TradeDecision(action="SHORT", symbol="XLF", allocation_pct=5,
                      entry_price=100.0, stop_loss=101.0, take_profit=90.0,
                      reasoning="spends no cash"),
    ])

    ExecutionStage(pipeline=pipeline).run(ctx)

    # Only the BUY's $5,000 — the SHORT's $5,000 must not be funded.
    assert recorded["planned"] == 5_000.0


def test_short_only_session_funds_nothing():
    pipeline = _pipeline(live_price=100.0, cash=50_000.0)
    recorded = _install_sweeper(pipeline)
    pipeline.broker.get_shortability.return_value = {
        "shortable": True, "easy_to_borrow": True,
    }
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx([TradeDecision(
        action="SHORT", symbol="XLF", allocation_pct=5,
        entry_price=100.0, stop_loss=101.0, take_profit=90.0,
        reasoning="spends no cash",
    )])

    ExecutionStage(pipeline=pipeline).run(ctx)

    assert recorded["planned"] == 0.0, (
        "a short-only session must not liquidate the yield vehicle at all"
    )


# ---------------------------------------------------------------------------
# Defect 2 — fixed 2026-09-24: the cash clamp used to refuse a resize under
# the flat $500 `min_order_usd` floor. That floor was an arbitrary round
# number (config/number_ledger.yaml), not a broker minimum, and Alpaca
# charges no stock commission — so a real, small residual trade was being
# refused on a false "too small to bother" basis. The clamp now places
# whatever the raw cash actually funds, however small, and only refuses when
# that is a genuine zero.
# ---------------------------------------------------------------------------


def test_fractional_clamp_places_a_token_order_not_refuses_it():
    """$3.11 of raw cash buys 0.0311 shares under fractional sizing. That
    is now SUBMITTED rather than refused — no commission, and fractional
    sizing means a tiny order is not actually costly to hold."""
    pipeline = _pipeline(live_price=100.0, cash=3.11, fractional=True)
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=10,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning="approved, then starved of cash",
    )], cash=3.11)

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert pipeline.broker.submit_order.call_args.kwargs["qty"] == pytest.approx(0.0311)
    assert ctx.execution_skips == []


def test_clamp_still_places_a_meaningful_partial_order():
    """A partial funding fill preserving a smaller real position is the
    behaviour the resize exists for — unaffected by the floor's removal."""
    pipeline = _pipeline(live_price=100.0, cash=750.0, fractional=True)
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=10,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning="approved, partially funded",
    )], cash=750.0)

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert pipeline.broker.submit_order.call_args.kwargs["qty"] == 7.5
    assert ctx.execution_skips == []


def test_whole_share_clamp_also_places_the_small_order():
    """The removal is not fractional-only. Four whole shares at $100 is
    $400 — well under the old $500 floor — and is now placed rather than
    refused."""
    pipeline = _pipeline(live_price=100.0, cash=499.0, fractional=False)
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=10,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning="approved, starved",
    )], cash=499.0)

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert pipeline.broker.submit_order.call_args.kwargs["qty"] == 4
    assert ctx.execution_skips == []


def test_min_notional_floor_falls_back_to_500_not_zero():
    """An unreadable `cash_sweep.min_order_usd` must not silently become
    "no floor" — that is the defect, not the fallback."""
    from src.pipeline_stages import _min_order_usd

    broken = MagicMock()          # config.cash_sweep.min_order_usd is a Mock
    assert _min_order_usd(broken) == 500.0
    assert _min_order_usd(None) == 500.0

    configured = MagicMock()
    configured.config.cash_sweep.min_order_usd = 250.0
    assert _min_order_usd(configured) == 250.0


# ---------------------------------------------------------------------------
# Defect 3 — unconfirmed proceeds.
# ---------------------------------------------------------------------------


def test_unconfirmed_funding_is_governed_by_raw_cash_not_refused():
    """`fund_buys` returning 0.0 means the proceeds could not be confirmed.
    The BUY loop must then be governed by raw cash — $174.96 of raw cash
    against a $10,000 approved order sizes down to what that cash actually
    buys (1.7496 shares). Fixed 2026-09-24: this used to be refused outright
    as under the flat $500 floor; it is now placed at the raw-cash size
    instead, since the resize itself (not the floor) is what keeps this
    from becoming a $10,000 unfunded position."""
    pipeline = _pipeline(live_price=100.0, cash=174.96, fractional=True)
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    _install_sweeper(pipeline, freed=0.0)
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=10,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning="approved, funding unconfirmed",
    )], cash=174.96)

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert pipeline.broker.submit_order.call_args.kwargs["qty"] == pytest.approx(1.7496)
    assert ctx.execution_skips == []


def test_unconfirmed_funding_is_visible_not_silent():
    """The zero-confirmed path must leave a durable trace: a
    `cash_sweep_released_zero` pipeline event. Fixed 2026-09-24: the
    resized buy is now placed rather than skipped, so the durable trace of
    the resize itself is the `funding` / `resized` pipeline event, not a
    skip."""
    pipeline = _pipeline(live_price=100.0, cash=174.96, fractional=True)
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    _install_sweeper(pipeline, freed=0.0)
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=10,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning="approved, funding unconfirmed",
    )], cash=174.96)

    ExecutionStage(pipeline=pipeline).run(ctx)

    payloads = " ".join(p for _kind, p in _events(pipeline))
    assert "cash_sweep_released_zero" in payloads
    assert "confirmed_cash_partially_funded_order" in payloads
    assert ctx.execution_skips == []


def test_unconfirmed_funding_adopts_the_refreshed_cash_reading():
    """`fund_buys` refreshes ctx from the broker BEFORE deciding it cannot
    confirm anything. If that refresh shows LESS cash than the pre-sale
    reading, the BUY loop must clamp against the smaller, truer figure —
    the stale reading is the one that lets an unfunded order through."""
    pipeline = _pipeline(live_price=100.0, cash=10_000.0, fractional=True)
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}

    def _drain(ctx):
        ctx.cash = 750.0          # broker says less than we started with

    _install_sweeper(pipeline, freed=0.0, on_call=_drain)
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=10,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning="approved",
    )], cash=10_000.0)

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    # Stale pre-sale cash ($10,000) would have funded the full 100 shares.
    assert pipeline.broker.submit_order.call_args.kwargs["qty"] == 7.5
