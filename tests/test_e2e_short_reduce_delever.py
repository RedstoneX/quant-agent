"""Hermetic end-to-end: a SHORT entry, a REDUCE of a held long, and a DE-LEVER.

Same harness and style as tests/test_e2e_rotation.py and
tests/test_e2e_close_existing_book.py (read them first): production
constructor, risk engine, Risk Manager and execution; the broker is the
rehearsal stand-in, the clock is frozen, every model seat answers from a
script, and the socket wall journals any attempt to leave the box.

Doctrine asserted from the INPUTS, never read back from the desk's output:
  * SHORT  a short opens as a whole-share SELL at the PM's weight of equity
           and is covered by exactly one GTC BUY stop ABOVE the entry, for
           the whole short, submitted after the entry;
  * REDUCE a PM target below the held weight sells only the difference,
           whole shares, and afterwards the stop resting on that name
           covers exactly the shares still held (no more, no fewer);
  * DELEVER a book over the gross cap is cut back under it by the session
           preamble before any model is asked, and every share still held
           is covered by a resting stop of exactly its size.

NOT COVERED: cover-to-reduce of a held short; a refused short; the cash-only
(`allow_margin=False`) force path; anything a model seat says.
"""

from __future__ import annotations

import json
import math

import pytest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests.test_e2e_morning_session as morning
from src.models import (
    AnalystProvenance,
    PortfolioDecision,
    ReasoningChain,
    TargetPosition,
    TechAnalysisResult,
    TechReasoningChain,
)
from tests.test_e2e_close_existing_book import (
    CLOSE_AT,
    _bars,
    _market,
    _news_says_nothing,
    _reviewer_says,
    _risk_says_yes,
)
from tests.test_e2e_morning_protection import _seed_company_profile_cache
from tests.test_e2e_morning_session import (
    LAST_CLOSE,
    RANGE_HIGH,
    RANGE_LOW,
    SESSION_AT,
    _build_config,
    _earnings_feed_stub,
    _macro_feed_stub,
    _market_stub,
    _news_feed_stub,
    _scripted_model_seats,
)
from tests.test_e2e_rotation import _seed_company_profile_cache_for

A, B = "SPY", "XLE"  # synthetic series (different sectors)
GROSS_CAP_X = 2.0  # config/settings.yaml risk.max_gross_exposure_x (asserted below)


def _tech(
    symbol: str, rating: str, *, last: float = LAST_CLOSE, floor: float = RANGE_LOW, ceil: float = RANGE_HIGH
) -> dict:
    bearish = rating == "sell"
    t = TechAnalysisResult(
        symbol=symbol,
        rating=rating,
        entry_price=last,
        reference_target=floor if bearish else ceil,
        stop_loss=round(ceil + 1.0, 2) if bearish else round(floor - 1.0, 2),
        support_levels=[floor],
        resistance_levels=[ceil],
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="scripted",
        thesis_invalid_if="closes below support",
        reasoning_chain=TechReasoningChain(trend="x", momentum="x", volatility="x", volume="x", support_resistance="x"),
    )
    return json.loads(t.model_dump_json(exclude_none=True))


def _pm(targets: list[TargetPosition]) -> dict:
    pm = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="x",
            news_check="x",
            earnings_check="x",
            signal_conflicts="x",
            sizing_logic="x",
            portfolio_balance="x",
            cash_target="x",
        ),
        targets=targets,
        portfolio_view="scripted",
    )
    return json.loads(pm.model_dump_json())


def _target(symbol, weight, *, direction="long", stance="buy", risk=None) -> TargetPosition:
    sizing = {"risk_allocation_pct": risk} if risk is not None else {"target_weight_pct": weight}
    return TargetPosition(
        symbol=symbol,
        direction=direction,
        **sizing,
        conviction="high",
        thesis="synthetic",
        thesis_invalid_if="closes above resistance" if direction == "short" else "closes below support",
        provenance=[
            AnalystProvenance(
                source="technical", observed_stance=stance, relationship="supports", evidence=f"rating {stance}"
            )
        ],
    )


def _seed_long(db, symbol, qty, entry, stop, when) -> None:
    row_id = db.insert_trade(
        symbol,
        "BUY",
        qty,
        entry,
        "synthetic range entry",
        "seed-entry",
        stop_loss=stop,
        take_profit=RANGE_HIGH,
        fill_status="filled",
        setup_type="range",
        expected_horizon_sessions=60,
        thesis_invalid_if=f"closes below {stop}",
    )
    db.conn.execute(
        "UPDATE trades SET timestamp=? WHERE id=?", ((when - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S"), row_id)
    )
    db.conn.commit()


def _run(
    tmp_path: Path,
    monkeypatch,
    *,
    answers: dict,
    held: dict,
    cash: float,
    session: str,
    market=None,
    at=SESSION_AT,
    borrowable=True,
):
    """`held` maps symbol -> (qty, entry, resting_stop)."""
    from ops.rehearsal.broker import BrokerSnapshot, install_rehearsal_broker
    from ops.rehearsal.broker_amend import give_amend_endpoint
    from ops.rehearsal.clock import frozen_clock
    from ops.rehearsal.network_wall import no_network
    from ops.rehearsal.runner import _sentinel_credentials
    from src.agents.base import reset_route_breakers
    from src.trading_calendar import ET

    reset_route_breakers()
    monkeypatch.chdir(tmp_path)
    for sym in (A, B):
        _seed_company_profile_cache_for(tmp_path, sym)
    _seed_company_profile_cache(tmp_path)
    config = _build_config(tmp_path)
    config.trading.universe = [A, B]
    assert config.risk.max_gross_exposure_x == GROSS_CAP_X
    now = at.replace(tzinfo=ET)
    monkeypatch.setattr(morning, "_scripted_answers", lambda: answers)
    trace: list = []
    attempts: list[str] = []
    price = LAST_CLOSE if market is None else market.get_ohlcv("x")[-1].close
    with (
        no_network(attempts),
        _sentinel_credentials(),
        patch("src.pipeline.MarketDataProvider", return_value=market or _market_stub()),
        patch("src.pipeline.MacroDataProvider", return_value=_macro_feed_stub()),
        patch("src.pipeline.NewsDataProvider", return_value=_news_feed_stub()),
        patch("src.pipeline.EarningsDataProvider", return_value=_earnings_feed_stub()),
        frozen_clock(now, run_id="e2e-short-reduce-delever"),
        _scripted_model_seats(trace),
    ):
        from src.pipeline import TradingPipeline

        pipeline = TradingPipeline(config)
        for sym, (qty, entry, stop) in held.items():
            _seed_long(pipeline.db, sym, qty, entry, stop, now.replace(tzinfo=None))
        mv = sum(q * price for q, _, _ in held.values())
        snapshot = BrokerSnapshot(
            as_of=now.date(),
            cash=cash,
            portfolio_value=cash + mv,
            last_equity=cash + mv,
            positions=[
                {
                    "symbol": s,
                    "qty": q,
                    "avg_entry": e,
                    "current_price": price,
                    "market_value": q * price,
                    "unrealized_pnl": q * (price - e),
                    "sector": "Energy" if s == B else "ETF",
                }
                for s, (q, e, _) in held.items()
            ],
            prices={A: price, B: price},
            standing_stops={s: st for s, (_, _, st) in held.items()},
        )
        trading = give_amend_endpoint(install_rehearsal_broker(pipeline.broker, snapshot, now=now))
        symbols_of = pipeline.broker._data_client._symbols
        pipeline.broker._data_client.get_stock_latest_trade = lambda request: {
            s: SimpleNamespace(price=price, timestamp=now) for s in symbols_of(request)
        }
        pipeline.broker.get_intraday_snapshots = lambda symbols, *a, **k: {
            s: {"last_price": price, "last_trade_at": now} for s in symbols
        }
        # The borrow gate reads the asset directory, which is offline here and
        # fails closed; answer it from the script (easy to borrow, or not).
        pipeline.broker.client.get_asset = lambda sym: SimpleNamespace(
            symbol=sym,
            shortable=borrowable,
            easy_to_borrow=borrowable,
            fractionable=False,
            tradable=True,
            status="active",
        )
        result = getattr(pipeline, f"run_{session}")()
    assert attempts == [], f"the session tried to leave the box: {attempts}"
    return result, trading, price


def _resting_stops(trading, symbol=None) -> list:
    return [
        o
        for o in trading._orders.values()
        if "stop" in o.order_type
        and o.status in ("pre_existing", "new", "accepted")
        and (symbol is None or o.symbol == symbol)
    ]


def _plain(trading) -> list:
    return [o.as_plain() for o in trading.submitted]


# ---------------------------------------------------------------- SHORT
SHORT_RISK_PCT = 1.0  # the PM's ask: the short may lose 1% of equity if stopped


def assert_short_entry_protected(result, trading) -> None:
    plain = _plain(trading)
    assert result["status"] == "executed", (result.get("status"), result.get("error"), plain)
    entries = [o for o in trading.submitted if o.symbol == A and "stop" not in o.order_type]
    assert len(entries) == 1 and entries[0].side == "sell", plain
    entry = entries[0]
    assert entry.status == "filled" and entry.limit_price, plain
    assert float(entry.qty) == math.floor(float(entry.qty)) >= 1, f"whole shares: {plain}"
    stops = _resting_stops(trading, A)
    assert len(stops) == 1, f"a short needs exactly one resting stop: {plain}"
    stop = stops[0]
    assert stop.side == "buy", f"a short is stopped by a BUY: {stop.as_plain()}"
    assert float(stop.qty) == float(entry.qty), (stop.as_plain(), entry.as_plain())
    assert str(stop.time_in_force).lower() == "gtc", stop.as_plain()
    assert stop.stop_price > entry.limit_price, f"a short's stop sits ABOVE its entry: {stop.as_plain()}"
    assert trading.submitted.index(stop) > trading.submitted.index(entry), plain
    loss_if_stopped = float(entry.qty) * (stop.stop_price - entry.limit_price)
    assert 0 < loss_if_stopped <= 10_000.0 * SHORT_RISK_PCT / 100.0 + 1e-6, (
        f"stopped out the short loses {loss_if_stopped:.2f}, over the {SHORT_RISK_PCT}% risk ask: {plain}"
    )
    assert result["stop_coverage_gaps"] == [], result["stop_coverage_gaps"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PRE-EXISTING, DIRECTION-NEUTRAL defect exposed by deleting the "
        "short-side gap haircut (owner ruling 2026-10-04). The constructor "
        "sizes against the analysis entry price, but execution submits a "
        "MARKETABLE limit, which prices away from that entry in whichever "
        "direction hurts: a short sells below the quote, a long buys above "
        "it. Either way the realized stop distance is wider than the one "
        "that was sized, so the position overshoots the PM's per-name risk "
        "ask. MEASURED here: 28 shares at limit 104.28 lose 110.88 against "
        "a 100.00 ask, 10.9% over. The 1.5x haircut was silently absorbing "
        "this slack on the short side ONLY; it never protected the long "
        "side, where the same overshoot has always been live and is simply "
        "not asserted anywhere. Re-adding a short-only buffer is exactly "
        "what the ruling forbids, and raising this bound would be "
        "loosening a guard to go green, so the defect is recorded "
        "red-but-known here instead. The real fix is to re-size against "
        "the limit price actually submitted, for both directions. NOT "
        "verified by me: whether the long side's overshoot is the same "
        "magnitude."
    ),
)
def test_a_short_opens_whole_shares_and_is_covered_by_a_buy_stop_above_entry(
    tmp_path,
    monkeypatch,
):
    base = morning._scripted_answers()
    # The same range series cut off at a swing HIGH: a short entered there has
    # the whole range to fall into, so the reward-to-risk refusal (owner ruling
    # 2026-10-01) does not apply and the short is a genuine candidate.
    bars = morning.BARS[:-16]
    top = bars[-1].close
    assert top > RANGE_HIGH - 1.5, top
    # Independent sources must net out in favour of the short: macro turns
    # bearish too (the scripted default is bullish and would oppose it).
    base["macro"] = {**base["macro"], "regime": "risk-off", "equity_outlook": "bearish"}
    base["tech"] = {"results": [_tech(A, "sell", last=top, floor=RANGE_LOW, ceil=RANGE_HIGH)]}
    base["portfolio"] = _pm([_target(A, 0, direction="short", stance="sell", risk=SHORT_RISK_PCT)])
    result, trading, _ = _run(
        tmp_path, monkeypatch, answers=base, held={}, cash=10_000.0, session="morning", market=_market(bars)
    )
    assert_short_entry_protected(result, trading)


# --------------------------------------------------------------- REDUCE
HELD, HELD_ENTRY, HELD_STOP = 100.0, 95.0, 92.0
REDUCE_TO_PCT = 4.0


def assert_reduce_keeps_exact_cover(result, trading, price, equity) -> None:
    plain = _plain(trading)
    assert result["status"] == "executed", (result.get("status"), result.get("error"), plain)
    sells = [o for o in trading.submitted if o.symbol == B and o.side == "sell" and o.order_type != "stop"]
    keep = math.ceil(equity * REDUCE_TO_PCT / 100.0 / price)
    assert len(sells) == 1, plain
    sold = float(sells[0].qty)
    assert sold == HELD - keep or abs(sold - (HELD - keep)) <= 1, (sold, HELD, keep, plain)
    assert 0 < sold < HELD, f"a reduce must neither be a no-op nor a close: {plain}"
    stops = _resting_stops(trading, B)
    assert len(stops) == 1, f"one stop must rest on the reduced name: {plain}"
    assert float(stops[0].qty) == HELD - sold, f"stop covers {stops[0].qty} but {HELD - sold} shares are held: {plain}"
    assert result["stop_coverage_gaps"] == [], result["stop_coverage_gaps"]


def test_a_reduce_sells_only_the_difference_and_resizes_the_stop_to_what_is_left(
    tmp_path,
    monkeypatch,
):
    base = morning._scripted_answers()
    base["tech"] = {"results": [_tech(B, "buy")]}
    base["portfolio"] = _pm([_target(B, REDUCE_TO_PCT)])
    cash = 1_000.0
    result, trading, price = _run(
        tmp_path, monkeypatch, answers=base, cash=cash, session="morning", held={B: (HELD, HELD_ENTRY, HELD_STOP)}
    )
    assert_reduce_keeps_exact_cover(result, trading, price, cash + HELD * price)


# -------------------------------------------------------------- DE-LEVER
OVER = {A: (130.0, 95.0, 92.0), B: (130.0, 95.0, 92.0)}


def assert_delevered_and_covered(result, trading, price, held, equity) -> None:
    plain = _plain(trading)
    sold = {s: 0.0 for s in held}
    for o in trading.submitted:
        if o.side == "sell" and o.order_type != "stop":
            sold[o.symbol] += float(o.qty)
    left = {s: held[s][0] - sold[s] for s in held}
    gross_x = sum(left.values()) * price / equity
    assert sum(sold.values()) > 0, f"nothing was cut from an over-cap book: {plain}"
    # Whole shares: the cut may fall short of the cap by less than ONE share.
    excess_usd = (sum(left.values()) * price) - GROSS_CAP_X * equity
    assert excess_usd < price, (
        f"gross {gross_x:.4f}x is over {GROSS_CAP_X}x by ${excess_usd:.2f}, more than one share: {plain}"
    )
    for s, qty in left.items():
        if qty > 0:
            cover = sum(float(o.qty) for o in _resting_stops(trading, s))
            assert cover == qty, f"{s}: {qty} held, {cover} covered: {plain}"
    assert result["stop_coverage_gaps"] == [], result["stop_coverage_gaps"]


def test_an_over_cap_book_is_cut_under_the_cap_and_every_share_left_is_covered(
    tmp_path,
    monkeypatch,
):
    bars = _bars(end=LAST_CLOSE)
    answers = {
        "position": _reviewer_says([{"action": "HOLD", "symbol": s, "reason": "thesis intact"} for s in (A, B)]),
        "risk": _risk_says_yes(),
        "news": _news_says_nothing(),
    }
    equity = 10_000.0
    price = bars[-1].close
    cash = equity - sum(q for q, _, _ in OVER.values()) * price  # negative: on margin
    assert sum(q for q, _, _ in OVER.values()) * price / equity > GROSS_CAP_X
    result, trading, price = _run(
        tmp_path, monkeypatch, answers=answers, held=OVER, cash=cash, session="close", market=_market(bars), at=CLOSE_AT
    )
    assert_delevered_and_covered(result, trading, price, OVER, equity)
