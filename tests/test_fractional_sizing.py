"""Spec §11.1 — fractional shares are ON, and the three guards that let them be.

WHAT THIS PINS

Whole-share rounding is a silent, constant tax on every position this desk
opens: the ratified example is a 6% target that arrived as 3.84% of the book.
`test_whole_share_rounding_tax_is_the_thing_being_removed` reproduces exactly
that arithmetic and fails if flooring ever comes back.

Turning it on is only acceptable because three guards bound the one real
risk — that the protective stop, which is now a SEPARATE order placed after
the fill, never lands at all:

  guard 1  the stop retries IMMEDIATELY and hard, in the same call
  guard 2  a stop that still fails ALERTS THE OWNER, not a log line
  guard 3  the 30-minute sweep reports NO STOP AT ALL as a different and
           worse condition than STOP PRESENT BUT MIS-SIZED

and because eligibility FAILS CLOSED: a symbol is sized fractionally only
when the broker itself confirms `fractionable`. Unknown, unreadable or
un-askable all mean whole shares. Fractional-by-assumption is the state this
must never reach.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.execution.broker import AlpacaBroker
from src.models import PortfolioDecision, ReasoningChain, TradeDecision
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage, _size_shares


# ==========================================================================
# Fixtures
# ==========================================================================

def _rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m", news_check="n", earnings_check="e",
        signal_conflicts="s", sizing_logic="z", portfolio_balance="b",
        cash_target="c",
    )


def _pipeline(*, live_price: float, cash: float = 1_000_000.0,
              fractional_enabled: bool = True,
              fractionable: dict | Exception | None = None,
              decimals: int = 4) -> MagicMock:
    """An ExecutionStage pipeline with the §11.1 knobs made real.

    `pipeline.config` on a bare MagicMock answers every attribute with a
    truthy mock, which would make `fractional_enabled` look permanently ON
    and `fractional_share_decimals` un-intable. Both are set explicitly here
    so each test states the configuration it is actually testing.
    """
    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = live_price
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": cash, "portfolio_value": 10_000.0}, [], {},
    )
    pipeline.risk_engine.check_daily_loss.return_value = None
    pipeline.config.execution.fractional_enabled = fractional_enabled
    pipeline.config.execution.fractional_share_decimals = decimals
    if isinstance(fractionable, Exception):
        pipeline.broker.get_fractionability.side_effect = fractionable
    else:
        pipeline.broker.get_fractionability.return_value = fractionable
    pipeline.broker.submit_order.return_value = {
        "id": "ord-1", "status": "accepted",
    }
    return pipeline


def _ctx(decisions, *, cash=1_000_000.0, total_value=10_000.0) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = cash
    ctx.total_value = total_value
    ctx.last_equity = total_value
    ctx.positions = []
    ctx.decision_id = "run-x-dec-frac"
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(), decisions=decisions, portfolio_view="t",
    )
    # No bars: the entry ATR stop floor needs them, and this file is about
    # share COUNTS, not stop placement.
    ctx.symbols_bars = {}
    return ctx


#: The ratified example, made arithmetic. A $10,000 book, a 6% target and a
#: $384 share price want 1.5625 shares. Floored to whole shares that is ONE
#: share — $384, or 3.84% of the book. 6% asked for, 3.84% delivered.
_PRICE = 384.0
_ALLOC_PCT = 6.0
_EXACT_SHARES = 1.5625


def _buy(symbol="V", price=_PRICE, alloc=_ALLOC_PCT, stop=364.0):
    """A BUY whose risk-based size is deliberately LARGER than its
    allocation-based size, so allocation is the binding constraint and the
    number under test is unambiguous."""
    return TradeDecision(
        action="BUY", symbol=symbol, allocation_pct=alloc,
        entry_price=price, stop_loss=stop, take_profit=price * 1.3,
        reasoning="fractional sizing",
    )


def _submitted_qty(pipeline) -> float:
    return pipeline.broker.submit_order.call_args.kwargs["qty"]


# ==========================================================================
# 1. Exact sizing — the tax, and its removal
# ==========================================================================

def test_exact_fractional_sizing_delivers_the_requested_risk_share():
    pipeline = _pipeline(
        live_price=_PRICE,
        fractionable={"fractionable": True, "reason": "fractionable",
                      "symbol": "V"},
    )

    orders = ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert len(orders) == 1
    assert _submitted_qty(pipeline) == pytest.approx(_EXACT_SHARES)
    # The point of the whole phase: the book gets the weight it asked for.
    delivered_pct = _submitted_qty(pipeline) * _PRICE / 10_000 * 100
    assert delivered_pct == pytest.approx(_ALLOC_PCT)


def test_whole_share_rounding_tax_is_the_thing_being_removed():
    """The regression guard §11.1 exists for. With fractional OFF this is the
    OLD behaviour, and the numbers are the ratified ones: 6% requested,
    3.84% delivered — a 36% haircut nobody chose. If flooring ever comes
    back on the fractional path, `test_exact_fractional_sizing...` above
    starts producing THIS number and fails."""
    pipeline = _pipeline(live_price=_PRICE, fractional_enabled=False)

    orders = ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert len(orders) == 1
    assert _submitted_qty(pipeline) == 1.0
    delivered_pct = _submitted_qty(pipeline) * _PRICE / 10_000 * 100
    assert delivered_pct == pytest.approx(3.84)
    assert delivered_pct < _ALLOC_PCT
    # The flag is a switch, not a suggestion: nothing was even asked of the
    # broker, so turning it off cannot cost an asset-directory call either.
    pipeline.broker.get_fractionability.assert_not_called()


def test_a_sub_one_share_position_is_taken_not_skipped():
    """Under whole-share sizing a position worth less than one share rounded
    to zero and was recorded as a `qty_zero` skip. Under exact sizing it is a
    legitimate, correctly-weighted position — and the funding preflight has
    to agree, or the BUY dies before the submit loop ever sees it."""
    pipeline = _pipeline(
        live_price=2_000.0,
        fractionable={"fractionable": True, "reason": "fractionable"},
    )
    ctx = _ctx([_buy(symbol="BRK-B", price=2_000.0, alloc=5.0, stop=1_900.0)])

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert _submitted_qty(pipeline) == pytest.approx(0.25)
    assert ctx.execution_skips == []


def test_a_fractional_entry_is_not_re_pegged():
    """Alpaca's replace endpoint types qty as an int. Truncating would SHRINK
    a position the risk math already sized, so the replacement is refused and
    the original order stays authoritative."""
    with patch("src.execution.broker.TradingClient"):
        broker = AlpacaBroker("k", "s", paper=True)

    out = broker.replace_entry_limit("ord-1", 101.0, qty=1.5625)

    assert out["id"] is None
    assert out["status"] == "replace_unsupported_fractional_qty"
    broker.client.replace_order_by_id.assert_not_called()


def test_fractional_sizing_floors_it_never_rounds_up():
    """Rounding UP would spend more risk budget than the sizing math allowed.
    At 2 decimals, 1.5625 shares must become 1.56 — not 1.57."""
    pipeline = _pipeline(
        live_price=_PRICE, decimals=2,
        fractionable={"fractionable": True, "reason": "fractionable"},
    )

    ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert _submitted_qty(pipeline) == pytest.approx(1.56)


# ==========================================================================
# 2. Eligibility fails CLOSED
# ==========================================================================

def test_non_fractionable_symbol_falls_back_to_whole_shares():
    pipeline = _pipeline(
        live_price=_PRICE,
        fractionable={"fractionable": False, "reason": "not_fractionable",
                      "symbol": "V"},
    )

    ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert _submitted_qty(pipeline) == 1.0


def test_unknown_fractionable_flag_falls_back_to_whole_shares():
    """The asset record came back but carries no `fractionable` field at all.
    "Absent" is not "true"."""
    pipeline = _pipeline(
        live_price=_PRICE,
        fractionable={"fractionable": False, "reason": "fractionable_unknown",
                      "symbol": "V"},
    )

    ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert _submitted_qty(pipeline) == 1.0


def test_failed_fractionable_lookup_falls_back_to_whole_shares():
    """The lookup RAISED. A fractional order on a name that cannot take one
    is rejected outright by the broker, turning an approved trade into no
    trade — so an unanswerable question means whole shares, every time."""
    pipeline = _pipeline(
        live_price=_PRICE, fractionable=RuntimeError("alpaca 500"),
    )

    orders = ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert len(orders) == 1, "a broken lookup must not cost the trade"
    assert _submitted_qty(pipeline) == 1.0


def test_short_entries_are_always_whole_share():
    """A borrowed share cannot be fractional, so this is not a policy choice
    to expose — the broker is not even asked."""
    pipeline = _pipeline(
        live_price=200.0,
        fractionable={"fractionable": True, "reason": "fractionable"},
    )
    pipeline.broker.get_shortability.return_value = {
        "shortable": True, "easy_to_borrow": True, "reason": "eligible",
    }
    short = TradeDecision(
        action="SHORT", symbol="TSLA", allocation_pct=7.0,
        entry_price=200.0, stop_loss=210.0, take_profit=170.0,
        reasoning="short it",
    )

    ExecutionStage(pipeline=pipeline).run(_ctx([short]))

    qty = _submitted_qty(pipeline)
    assert float(qty).is_integer()
    pipeline.broker.get_fractionability.assert_not_called()


# --- the broker-side gate on its own ---------------------------------------

def _broker_with_asset(asset) -> AlpacaBroker:
    with patch("src.execution.broker.TradingClient"):
        broker = AlpacaBroker("k", "s", paper=True)
    if isinstance(asset, Exception):
        broker.client.get_asset.side_effect = asset
    else:
        broker.client.get_asset.return_value = asset
    return broker


def test_get_fractionability_reports_true_only_when_the_broker_says_so():
    broker = _broker_with_asset({"fractionable": True})
    assert broker.get_fractionability("MSFT")["fractionable"] is True


@pytest.mark.parametrize("asset, reason", [
    ({"fractionable": False}, "not_fractionable"),
    ({}, "fractionable_unknown"),
    (RuntimeError("boom"), "asset_lookup_failed"),
])
def test_get_fractionability_fails_closed(asset, reason):
    broker = _broker_with_asset(asset)
    result = broker.get_fractionability("XYZ")
    assert result["fractionable"] is False
    assert result["reason"] == reason


def test_get_fractionability_is_cached_per_symbol():
    broker = _broker_with_asset({"fractionable": True})
    broker.get_fractionability("MSFT")
    broker.get_fractionability("MSFT")
    assert broker.client.get_asset.call_count == 1


# --- the quantizer on its own ----------------------------------------------

@pytest.mark.parametrize("raw, fractional, expected", [
    (1.5625, False, 1.0),
    (1.5625, True, 1.5625),
    (0.99999999, True, 0.9999),     # floors, never rounds up
    (0.99999999, False, 0.0),       # ...and whole-share mode still says zero
    (3.0, True, 3.0),
    (float("nan"), True, 0.0),
    (float("inf"), True, 0.0),
    (-4.0, True, 0.0),
])
def test_size_shares_quantization(raw, fractional, expected):
    pipeline = MagicMock()
    pipeline.config.execution.fractional_share_decimals = 4
    assert _size_shares(pipeline, raw, fractional=fractional) == pytest.approx(expected)


# ==========================================================================
# 3. Guard 1 — the stop retries immediately and hard
# ==========================================================================

def _protection_broker(*, filled_qty: float, stop_results: list) -> AlpacaBroker:
    """A broker whose entry filled `filled_qty` and whose stop submissions
    play out `stop_results` in order (an Exception is raised, a dict is
    returned)."""
    with patch("src.execution.broker.TradingClient"):
        broker = AlpacaBroker("k", "s", paper=True)
    broker.wait_for_order_terminal = MagicMock(return_value="filled")
    broker.get_order_fill_info = MagicMock(
        return_value={"filled_qty": filled_qty},
    )

    def _submit(**kwargs):
        outcome = stop_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {**outcome, "_qty": kwargs["qty"]}

    broker._submit_stop_limit_order = MagicMock(side_effect=_submit)
    return broker


def test_stop_placement_retries_immediately_and_the_retry_protects_the_position():
    """Guard 1. The first submit fails; the retry happens inside the SAME
    call — not queued, not deferred to the next sweep — and the position ends
    the call protected."""
    broker = _protection_broker(
        filled_qty=10.0,
        stop_results=[RuntimeError("429 rate limited"), {"id": "stop-1"}],
    )

    with patch("src.execution.broker.time.sleep") as sleep:
        out = broker.place_entry_protection("NVDA", "e1", 95.0, requested_qty=10)

    assert out is not None and out["id"] == "stop-1"
    assert broker._submit_stop_limit_order.call_count == 2
    assert out["_qty"] == 10.0, "the retry must cover the WHOLE fill"
    # The retry is immediate: sub-second, not a sweep interval away.
    assert sleep.call_args.args[0] <= 1.0


def test_stop_placement_gives_up_after_a_bounded_number_of_attempts():
    """Bounded, because a failure that survives the burst is a REJECTION and
    retrying a rejection forever only delays the alert that is the real
    remedy."""
    broker = _protection_broker(
        filled_qty=10.0,
        stop_results=[RuntimeError("nope")] * 10,
    )

    with patch("src.execution.broker.time.sleep"):
        out = broker.place_entry_protection("NVDA", "e1", 95.0, requested_qty=10)

    assert out is None
    assert broker._submit_stop_limit_order.call_count == 3


def test_a_fractional_fill_whose_exact_stop_is_refused_still_protects_the_whole_shares():
    """The §11.1 open question, contained. If the broker will not carry a
    stop for 12.3456 shares, covering 12 beats covering none — and the
    sub-share remainder is reported, never swallowed."""
    broker = _protection_broker(
        filled_qty=12.3456,
        stop_results=[RuntimeError("fractional qty not supported")] * 3
        + [{"id": "stop-whole"}],
    )

    with patch("src.execution.broker.time.sleep"):
        out = broker.place_entry_protection("NVDA", "e1", 95.0, requested_qty=13)

    assert out["id"] == "stop-whole"
    assert out["_qty"] == 12.0
    assert out["covered_qty"] == 12.0
    assert out["uncovered_qty"] == pytest.approx(0.3456)


# ==========================================================================
# 4. Guard 2 — a stop that never lands ALERTS THE OWNER
# ==========================================================================

def _entry_spec(**kw) -> dict:
    spec = {"symbol": "NVDA", "side": "buy", "order_id": "e1",
            "stop_price": 95.0, "qty": 10, "reference_price": 100.0,
            "limit_price": 100.0, "trade_row_id": 1}
    spec.update(kw)
    return spec


def _run_protection_phase(pipeline, spec) -> None:
    """Drive ExecutionStage as far as the post-fill protection loop by
    letting a real BUY flow through it with the broker's protection call
    stubbed to whatever the test wants."""
    pipeline.broker.submit_order.return_value = {
        "id": spec["order_id"], "status": "accepted", "symbol": spec["symbol"],
        "side": "buy", "pending_stop_price": spec["stop_price"],
    }
    decision = TradeDecision(
        action="BUY", symbol=spec["symbol"], allocation_pct=10.0,
        entry_price=100.0, stop_loss=spec["stop_price"], take_profit=130.0,
        reasoning="protection test",
    )
    ExecutionStage(pipeline=pipeline).run(_ctx([decision]))


def test_a_stop_that_fails_every_retry_raises_an_owner_alert():
    """Guard 2, and the difference between a brief gap and an indefinite one.
    Asserted on the ALERT — a log line is explicitly not acceptable here."""
    pipeline = _pipeline(live_price=100.0, fractional_enabled=False)
    pipeline.broker.place_entry_protection.return_value = None
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 7.0}

    with patch("src.notifier.send_owner_alert") as alert:
        _run_protection_phase(pipeline, _entry_spec())

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert "NO STOP AT ALL" in body
    assert "NVDA" in body
    assert alert.call_args.kwargs["symbols"] == ["NVDA"]


def test_a_partially_covering_stop_also_raises_an_owner_alert():
    pipeline = _pipeline(live_price=100.0, fractional_enabled=False)
    pipeline.broker.place_entry_protection.return_value = {
        "id": "stop-whole", "covered_qty": 12.0, "uncovered_qty": 0.3456,
    }

    with patch("src.notifier.send_owner_alert") as alert:
        _run_protection_phase(pipeline, _entry_spec())

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert "PARTIALLY COVERS" in body
    assert "0.3456" in body


def test_a_protected_position_does_not_wake_the_owner():
    pipeline = _pipeline(live_price=100.0, fractional_enabled=False)
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-1"}

    with patch("src.notifier.send_owner_alert") as alert:
        _run_protection_phase(pipeline, _entry_spec())

    alert.assert_not_called()


def test_an_entry_that_filled_nothing_does_not_wake_the_owner():
    """`place_entry_protection` returns None for a zero fill too. There is no
    position, so there is nothing unprotected — and waking a human for a BUY
    that simply did not fill is how guard 2 gets switched off."""
    pipeline = _pipeline(live_price=100.0, fractional_enabled=False)
    pipeline.broker.place_entry_protection.return_value = None
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 0.0}

    with patch("src.notifier.send_owner_alert") as alert:
        _run_protection_phase(pipeline, _entry_spec())

    alert.assert_not_called()


# ==========================================================================
# 5. Guard 3 — the sweep separates "no stop" from "mis-sized"
# ==========================================================================

def _sweep_pipeline(positions, covered_by_symbol: dict) -> MagicMock:
    pipeline = MagicMock()
    pipeline.broker.get_positions.return_value = positions
    pipeline.db.get_pending_protection_restores.return_value = []
    pipeline._sweeper.return_value = None
    pipeline._repair_stop_coverage.return_value = False

    def _snapshot(symbol, side="sell"):
        qty = covered_by_symbol.get(symbol, 0.0)
        return True, ([{"qty": qty}] if qty else [])

    pipeline.broker.snapshot_protective_stops.side_effect = _snapshot
    # The escalation is the behaviour under test — bind the REAL one, or the
    # MagicMock silently absorbs the call and every assertion below passes
    # for the wrong reason.
    pipeline._alert_owner_no_stop = TradingPipeline._alert_owner_no_stop
    return pipeline


def _held(symbol: str, qty: float) -> MagicMock:
    position = MagicMock()
    position.symbol = symbol
    position.qty = qty
    return position


def test_sweep_distinguishes_no_stop_at_all_from_a_mis_sized_stop():
    """Guard 3. Two positions, two different conditions, and they must not
    read as one count of 'gaps' — a position stopped at the wrong size still
    has a broker order standing watch; a position with none has nothing."""
    pipeline = _sweep_pipeline(
        [_held("NAKED", 10.0), _held("SHORTSTOP", 10.0)],
        {"NAKED": 0.0, "SHORTSTOP": 4.0},
    )

    with patch("src.notifier.send_owner_alert"):
        gaps = TradingPipeline._reconcile_stop_coverage(pipeline)

    by_symbol = {g["symbol"]: g for g in gaps}
    assert by_symbol["NAKED"]["coverage"] == "none"
    assert by_symbol["SHORTSTOP"]["coverage"] == "partial"


def test_sweep_says_no_stop_at_all_in_words_not_just_a_field(caplog):
    pipeline = _sweep_pipeline([_held("NAKED", 10.0)], {"NAKED": 0.0})

    with caplog.at_level("WARNING"), patch("src.notifier.send_owner_alert"):
        TradingPipeline._reconcile_stop_coverage(pipeline)

    assert "NO STOP AT ALL" in caplog.text
    assert "STOP MIS-SIZED" not in caplog.text


def test_sweep_says_mis_sized_in_words_for_a_partially_covered_position(caplog):
    pipeline = _sweep_pipeline([_held("SHORTSTOP", 10.0)], {"SHORTSTOP": 4.0})

    with caplog.at_level("WARNING"), patch("src.notifier.send_owner_alert"):
        TradingPipeline._reconcile_stop_coverage(pipeline)

    assert "STOP MIS-SIZED" in caplog.text
    assert "NO STOP AT ALL" not in caplog.text


def test_sweep_alerts_the_owner_only_for_a_position_with_no_stop_at_all():
    """Escalation is reserved for the worse condition. A mis-sized stop is
    real but bounded and rides the session banner; alerting on both is how a
    channel gets tuned out."""
    pipeline = _sweep_pipeline(
        [_held("NAKED", 10.0), _held("SHORTSTOP", 10.0)],
        {"NAKED": 0.0, "SHORTSTOP": 4.0},
    )

    with patch("src.notifier.send_owner_alert") as alert:
        TradingPipeline._reconcile_stop_coverage(pipeline)

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert "NO STOP AT ALL" in body
    assert "NAKED" in body
    assert "SHORTSTOP" not in body


def test_sweep_does_not_alert_when_the_auto_repair_closed_the_gap():
    pipeline = _sweep_pipeline([_held("NAKED", 10.0)], {"NAKED": 0.0})
    pipeline._repair_stop_coverage.return_value = True

    with patch("src.notifier.send_owner_alert") as alert:
        TradingPipeline._reconcile_stop_coverage(pipeline)

    alert.assert_not_called()


# ==========================================================================
# 6. Guard 3, second half — the sweep's finding reaches the operator
# ==========================================================================

def test_an_ordinary_intra_tick_is_still_silent():
    """14 ticks a day. Breaking that silence casually is what makes the
    channel worthless when it matters."""
    from src.notifier import format_session_result

    assert format_session_result(
        "intra_check", {"status": "ok", "run_id": "r", "positions": 3}, 2.0,
    ) is None


def test_an_intra_tick_that_found_a_coverage_gap_breaks_the_silence():
    """This is the mode whose normal tick sends NOTHING, so before §11.1 the
    30-minute sweep's worst finding was reported to a log file and nowhere
    else. The gap is what breaks the silence, and it names the condition."""
    from src.notifier import format_session_result

    msg = format_session_result(
        "intra_check",
        {"status": "ok", "run_id": "r", "positions": 3,
         "stop_coverage_gaps": [
             {"symbol": "NAKED", "held_qty": 10.0, "covered_qty": 0.0,
              "coverage": "none"},
         ]},
        2.0,
    )

    assert msg is not None
    assert "NO STOP AT ALL" in msg
    assert "NAKED" in msg
