"""docs/WORK.md item 32 — the drawdown alarms' BASIS, owner calls 2026-09-11.

`tests/test_drawdown_brake_rescale.py` pinned the previous fix: the three
loss alarms (daily circuit breaker, 5-day and 20-day rolling-return brakes)
were re-expressed as multiples of the real per-trade risk unit, and the
multiples themselves were made square-root-of-time consistent. Both of those
still produced a FIXED PERCENTAGE OF EQUITY per alarm.

That is the flaw this file covers being removed. A fixed percentage is only
correct for the volatility regime it was picked in, and markets are not
stationary: the same -6.7% is an ordinary week's noise in one regime and an
unreachable gap event in another. The owner REFUSED recalibrating the fixed
number from more history — a recalibrated frozen number has the identical
flaw — and asked for a different basis.

THE BASIS IS THE BOOK ACTUALLY HELD. The yardstick is the realized daily
volatility of the current holdings at their current weights, reconstructed
from those holdings' real market price history, scaled to each window by
sqrt(time).

  NOT the account's own equity curve. That was the first implementation and
  the owner rejected it the same day, for two reasons no calibration fixes:
  (1) RAMP-UP CONTAMINATION — the account was reset 2026-09-02 and spends
  its first sessions going from all-cash to fully deployed; a mostly-cash
  account barely moves, so the measurement over exactly the sessions needed
  to activate the alarms would be artificially LOW, the thresholds
  artificially TIGHT, and they would then fire on normal behaviour once the
  book was deployed; (2) THE RECORD IS CONTAMINATED ANYWAY — this desk has
  never operated correctly, and calibrating a safety threshold from a record
  of malfunction is not sound.

What the holdings basis settles, and what it does not:

  RESEARCH-GROUNDED and unchanged: the sqrt(time) relationship between
  windows (Van Hemert, Ganz, Harvey et al., "Drawdowns", Journal of
  Portfolio Management, 2020). ONE sensitivity multiple scaled by sqrt(T)
  drives all three alarms, which is why no per-window multiple survives.

  ALSO SOUND, and not a number at all: measuring the basket's own return
  series and taking its standard deviation, rather than combining the
  holdings' individual volatilities. Correlation is handled implicitly and
  exactly.

  PROVISIONAL and labelled so: the sensitivity itself
  (`RiskConfig.drawdown_vol_sensitivity`), shipped at 3.0. Researched
  2026-09-11 — there is no citable industry-standard number for how many
  multiples of recent volatility should trip a drawdown alarm. 3.0 is an
  owner risk-appetite decision, reversible, and explicitly NOT a validated
  or derived figure. It replaced 6.7, which had been set only for day-one
  continuity and which measurement then showed left the daily breaker
  firing on a ~6.7-sigma session, i.e. effectively dormant.

  NOT TOUCHED: position sizing. Volatility here is only the yardstick the
  ALARM measures a loss against. Volatility-target exposure scaling was
  separately rejected for this desk (docs/OUTCOME.md).
"""
import math
import random
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import RiskConfig
from src.pipeline import TradingPipeline
from src.risk.constants import DEFAULT_DRAWDOWN_VOL_SENSITIVITY
from src.risk.rules import (
    GROSS_LADDER_ALERT_PCT,
    MIN_REALIZED_VOL_RETURNS,
    REALIZED_VOL_SKIP_NEWEST_SESSIONS,
    REALIZED_VOL_WINDOW_SESSIONS,
    PortfolioVolEstimate,
    RiskRuleEngine,
    measure_portfolio_daily_vol,
    normalized_holding_weights,
    portfolio_daily_vol_pct,
    position_weight_pct,
    vol_relative_drawdown_threshold_pct,
)

_EPOCH = date(2026, 1, 5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bars(daily_pct, *, start: float = 100.0, first_date: date = _EPOCH):
    """A bar series, OLDEST-FIRST (what `MarketDataProvider.get_ohlcv`
    returns), whose consecutive close-to-close returns are `daily_pct`."""
    bars = [SimpleNamespace(date=first_date, close=start)]
    for i, pct in enumerate(daily_pct, start=1):
        bars.append(SimpleNamespace(
            date=first_date + timedelta(days=i),
            close=bars[-1].close * (1 + pct / 100.0),
        ))
    return bars


def _steady(sigma_pct: float, *, sessions: int = 30, **kwargs):
    """A series whose daily returns are exactly +/-`sigma_pct`, alternating,
    rather than random — the tests below assert on thresholds and a random
    walk would make them flaky for reasons unrelated to the code under test.

    Its SAMPLE standard deviation (ddof=1, this codebase's convention) is
    `sigma_pct * sqrt(n/(n-1))`, i.e. ~2.6% above `sigma_pct` over a
    20-session window. That is the unbiased-variance correction doing its
    job, not drift, which is why the assertions below allow a few percent.
    """
    return _bars(
        [sigma_pct if i % 2 else -sigma_pct for i in range(sessions)],
        **kwargs,
    )


def _held(**symbol_to_weight_pct):
    """`{symbol: fraction of equity}` from readable percentages."""
    return {sym: pct / 100.0 for sym, pct in symbol_to_weight_pct.items()}


def _cfg(**overrides) -> RiskConfig:
    base = dict(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
    )
    base.update(overrides)
    return RiskConfig(**base)


def _engine(sigma_pct, **cfg_overrides) -> RiskRuleEngine:
    """An engine whose held book measures `sigma_pct` per session."""
    return RiskRuleEngine(
        _cfg(**cfg_overrides), portfolio_vol_provider=lambda: sigma_pct,
    )


# ---------------------------------------------------------------------------
# The measurement itself — real holdings, real weights
# ---------------------------------------------------------------------------

def test_it_measures_the_volatility_of_the_book_actually_held():
    """Sanity floor: a fully-deployed single holding that moves 1% a session
    makes a book that moves 1% a session."""
    estimate = measure_portfolio_daily_vol(_held(NVDA=100), {"NVDA": _steady(1.0)})
    assert estimate.daily_vol_pct == pytest.approx(1.0, rel=0.03)
    assert estimate.symbols_used == ("NVDA",)
    assert estimate.unmeasured_symbols == ()
    assert estimate.observations == REALIZED_VOL_WINDOW_SESSIONS


def test_it_never_reads_the_accounts_own_performance():
    """The whole reason for the second basis change. The measurement takes
    holdings and market bars — there is no argument through which an account
    equity curve, or any record of this desk's own trading, could reach it.
    A signature test, because the previous implementation's contamination
    was structural, not a tuning mistake."""
    import inspect
    params = list(inspect.signature(measure_portfolio_daily_vol).parameters)
    assert params[:2] == ["weights", "bars_by_symbol"]
    forbidden = ("equity", "pnl", "account", "curve", "history")
    for name in params:
        assert not any(token in name for token in forbidden), (
            f"`{name}` looks like the account's own record leaking back into "
            f"the yardstick — the owner rejected that basis on 2026-09-11"
        )


def test_the_threshold_scales_with_how_deployed_the_book_is():
    """REQUIRED behaviour, not a bug to correct away. Weights are fractions
    of equity and are deliberately NOT renormalised to sum to one, so a
    30%-deployed book reconstructs a normal daily move ~30% the size of the
    same basket fully deployed — and its alarm tightens to match. A third of
    the book at risk must not be allowed the same loss as all of it."""
    bars = {"NVDA": _steady(2.0)}
    full = portfolio_daily_vol_pct(_held(NVDA=100), bars)
    third = portfolio_daily_vol_pct(_held(NVDA=30), bars)
    assert full == pytest.approx(2.0, rel=0.03)
    assert third == pytest.approx(full * 0.30, rel=1e-6)
    # And so does the alarm distance that comes off it.
    assert _engine(third).daily_loss_limit_pct == pytest.approx(
        _engine(full).daily_loss_limit_pct * 0.30, rel=1e-2,
    )


def test_a_ramping_book_is_measurable_from_the_first_session():
    """What the equity-curve basis could not do. A book that went from cash
    to 20% deployed this morning has a measurable normal daily move today,
    because the measurement uses the HOLDINGS' price history — which is
    years long — not the account's, which is one session old."""
    estimate = measure_portfolio_daily_vol(
        _held(NVDA=20), {"NVDA": _steady(1.5)},
    )
    assert estimate.daily_vol_pct == pytest.approx(0.30, rel=0.03)
    assert estimate.observations >= MIN_REALIZED_VOL_RETURNS


def test_a_position_opened_today_is_measurable_today():
    """A holding's price history exists regardless of when the desk bought
    it. There is no warm-up period per position, and no "too new to judge"
    state for a normally-traded symbol."""
    # Nothing in the estimate depends on holding age — the same bars and the
    # same weight give the same answer whatever the entry date was.
    bars = {"AAPL": _steady(0.8)}
    assert portfolio_daily_vol_pct(_held(AAPL=50), bars) == pytest.approx(
        0.40, rel=0.03,
    )


# ---------------------------------------------------------------------------
# Correlation — why the basket's returns are built before the stdev is taken
# ---------------------------------------------------------------------------

def test_two_holdings_moving_together_are_one_bet():
    """Two perfectly correlated 50% holdings that each move 1% make a book
    that moves 1% — not 0.71%, which is what independence would give."""
    moves = [1.0 if i % 2 else -1.0 for i in range(30)]
    estimate = measure_portfolio_daily_vol(
        _held(XLK=50, QQQ=50),
        {"XLK": _bars(moves), "QQQ": _bars(moves)},
    )
    assert estimate.daily_vol_pct == pytest.approx(1.0, rel=0.03)


def test_an_offsetting_pair_is_not_the_sum_of_its_volatilities():
    """The reason weighted RETURNS are used and individual volatilities are
    never summed. Two holdings that move exactly opposite each other make a
    book that barely moves; summing their volatilities would report a
    dangerous book and set a far looser alarm than the book deserves."""
    up = [1.0 if i % 2 else -1.0 for i in range(30)]
    down = [-pct for pct in up]
    estimate = measure_portfolio_daily_vol(
        _held(A=50, B=50), {"A": _bars(up), "B": _bars(down)},
    )
    # Each leg on its own measures ~0.5% at a 50% weight; naive summing
    # would say 1.0%. The real basket move is ~0.
    assert portfolio_daily_vol_pct(_held(A=50), {"A": _bars(up)}) \
        == pytest.approx(0.5, rel=0.03)
    assert estimate.daily_vol_pct is None
    assert "no measurable daily movement" in estimate.reason


def test_a_short_offsets_rather_than_adds():
    """Weights are SIGNED. A long and a short in the same thing is a flat
    book, and the yardstick must say so; taking absolute weights would
    report a hedge as twice as volatile as an outright."""
    moves = [1.0 if i % 2 else -1.0 for i in range(30)]
    bars = {"SPY": _bars(moves)}
    weights = normalized_holding_weights(
        [
            SimpleNamespace(symbol="SPY", market_value=50_000.0),
            SimpleNamespace(symbol="SPY", market_value=-50_000.0),
        ],
        equity=100_000.0,
    )
    assert weights == {}, "a flat net position is no exposure to measure"
    assert measure_portfolio_daily_vol(weights, bars).daily_vol_pct is None


def test_leverage_is_not_double_counted_in_the_weights():
    """`position_weight_pct` scales a leveraged ETF's weight by its leverage
    factor, which is right for an EXPOSURE CAP and wrong here: the ETF's own
    price history already moves at that leverage, so multiplying the weight
    as well would report several times the volatility the book can actually
    experience. The vol weighting must be raw market value over equity."""
    from src.quantities import ETF_LEVERAGE
    # abs(): the table's leveraged entries are INVERSE ETFs (SQQQ at -3.0),
    # and `gross_multiplier` takes the magnitude. A -3x ETF's own price
    # series moves 3x, so the double-count hazard is identical.
    levered = next(
        (sym for sym, factor in ETF_LEVERAGE.items() if abs(float(factor)) > 1.0),
        None,
    )
    assert levered is not None, (
        "the leverage table lost every leveraged entry — this test's premise "
        "is gone, not satisfied"
    )
    position = SimpleNamespace(symbol=levered, market_value=10_000.0)
    weights = normalized_holding_weights([position], equity=100_000.0)
    assert weights[levered] == pytest.approx(0.10), (
        "the volatility yardstick must weight by raw market value over "
        "equity; the leverage is already in the ETF's own price series"
    )
    assert position_weight_pct(position, 100_000.0) > 10.0, (
        "guard on the premise: the exposure-cap convention really does "
        "apply a leverage multiplier, so reusing it here would double-count"
    )


# ---------------------------------------------------------------------------
# Edge cases — each must be honest, and none may crash
# ---------------------------------------------------------------------------

def test_an_all_cash_book_has_no_portfolio_to_measure():
    """Not "a portfolio with zero volatility". Zero would collapse every
    threshold to zero and trip the alarm on the first cent lost, so this
    falls back to the fixed percentage."""
    for empty in ({}, None):
        estimate = measure_portfolio_daily_vol(empty, {"NVDA": _steady(1.0)})
        assert estimate.daily_vol_pct is None
        assert "cash" in estimate.reason
    cold = RiskRuleEngine(_cfg(), portfolio_vol_provider=lambda: None)
    assert cold.daily_loss_limit_pct == pytest.approx(
        _cfg().effective_max_daily_loss_pct,
    )


def test_a_single_position_book_measures_normally():
    """No special case, and no minimum number of names. One holding is a
    perfectly well-defined portfolio."""
    estimate = measure_portfolio_daily_vol(_held(NVDA=80), {"NVDA": _steady(1.25)})
    assert estimate.daily_vol_pct == pytest.approx(1.0, rel=0.03)
    assert estimate.symbols_used == ("NVDA",)


def test_the_only_holding_having_no_price_history_falls_back():
    """No measurement exists, so no threshold may be produced from one. The
    fixed percentage governs and the reason is recorded."""
    for bars in ({}, {"NEWCO": []}, {"NEWCO": _steady(1.0, sessions=4)}):
        estimate = measure_portfolio_daily_vol(_held(NEWCO=50), bars)
        assert estimate.daily_vol_pct is None
        assert estimate.unmeasured_symbols == ("NEWCO",)
        assert estimate.book_weight_pct == pytest.approx(50.0)


def test_a_holding_with_no_history_is_dropped_not_allowed_to_void_the_rest():
    """A fresh listing next to three measurable holdings must not disable
    the alarms for the whole book. It is dropped and NAMED, which makes the
    measured volatility a LOWER bound — so the threshold comes out tighter,
    the alarm fires sooner not later, and the arithmetic is identical to
    that holding simply not being deployed (which is the deployment-scaling
    behaviour this design already wants)."""
    bars = {
        "A": _steady(1.0), "B": _steady(1.0), "C": _steady(1.0),
        "NEWCO": _steady(1.0, sessions=3),
    }
    estimate = measure_portfolio_daily_vol(
        _held(A=25, B=25, C=25, NEWCO=25), bars,
    )
    assert estimate.daily_vol_pct is not None
    assert estimate.unmeasured_symbols == ("NEWCO",)
    assert estimate.measured_weight_pct == pytest.approx(75.0)
    assert estimate.book_weight_pct == pytest.approx(100.0)
    # Strictly tighter than the same book with NEWCO measurable — the safe
    # direction for a brake.
    full = measure_portfolio_daily_vol(
        _held(A=25, B=25, C=25, NEWCO=25),
        {**bars, "NEWCO": _steady(1.0)},
    )
    assert estimate.daily_vol_pct < full.daily_vol_pct


def test_unusable_bars_and_junk_inputs_never_crash():
    """Whatever arrives, the answer is a number that was measured or None.
    Never an exception, and never a threshold from a measurement that does
    not exist."""
    junk_bars = [
        SimpleNamespace(date=None, close=100.0),
        SimpleNamespace(date=_EPOCH, close=0.0),
        SimpleNamespace(date=_EPOCH, close=-5.0),
        SimpleNamespace(date=_EPOCH, close=float("nan")),
        SimpleNamespace(date=_EPOCH, close=float("inf")),
        SimpleNamespace(date=_EPOCH, close=None),
        SimpleNamespace(date=_EPOCH, close="120"),
        {"date": _EPOCH, "close": 100.0},
    ]
    assert measure_portfolio_daily_vol(
        _held(X=50), {"X": junk_bars},
    ).daily_vol_pct is None
    for bad_weights in (
        {"": 0.5}, {"X": None}, {"X": float("nan")}, {"X": True}, {"X": 0.0},
    ):
        assert isinstance(
            measure_portfolio_daily_vol(bad_weights, {"X": _steady(1.0)}),
            PortfolioVolEstimate,
        )
    assert normalized_holding_weights(None, 0) == {}
    assert normalized_holding_weights([SimpleNamespace(symbol="X")], -1) == {}
    assert normalized_holding_weights(
        [SimpleNamespace(symbol="", market_value=1.0)], 100.0,
    ) == {}


# ---------------------------------------------------------------------------
# The two properties inherited from the previous design, still holding
# ---------------------------------------------------------------------------

def test_the_session_being_judged_is_excluded_from_its_own_yardstick():
    """The self-loosening trap. If the window included today, a -6% session
    on a book that normally moves 0.25% would raise the measured volatility
    enough to re-classify ITSELF as ordinary — the alarm would be unable to
    fire on precisely the days it exists for."""
    assert REALIZED_VOL_SKIP_NEWEST_SESSIONS == 1
    calm = _steady(0.25, sessions=30)
    shocked = list(calm)
    shocked[-1] = SimpleNamespace(
        date=calm[-1].date, close=calm[-2].close * 0.94,
    )
    weights, bars = _held(SPY=100), {"SPY": shocked}
    assert portfolio_daily_vol_pct(weights, bars) == pytest.approx(
        0.25, rel=0.05,
    )
    assert portfolio_daily_vol_pct(weights, bars, skip_newest=0) > 1.0


def test_the_window_is_twenty_sessions_and_older_history_is_ignored():
    """The trailing window is what makes this non-stationary. A violent
    quarter the market has already left behind must not still be setting
    today's thresholds."""
    assert REALIZED_VOL_WINDOW_SESSIONS == 20
    ancient_chaos = [6.0 if i % 2 else -6.0 for i in range(200)]
    recent_calm = [0.5 if i % 2 else -0.5 for i in range(25)]
    bars = _bars(ancient_chaos + recent_calm)
    assert portfolio_daily_vol_pct(_held(SPY=100), {"SPY": bars}) \
        == pytest.approx(0.5, rel=0.05)


def test_enough_history_is_still_required_and_the_reason_is_still_honest():
    """Unchanged in value and derivation, now applied to the HOLDINGS' price
    history rather than the account's record. The relative standard error of
    a sample standard deviation is ~`1 / sqrt(2(n-1))`, so 10 returns puts
    the estimate's own error at ~24% — below that the estimate is the
    dominant source of error and falling back is the more honest answer."""
    assert MIN_REALIZED_VOL_RETURNS == 10
    assert 1 / math.sqrt(2 * (MIN_REALIZED_VOL_RETURNS - 1)) \
        == pytest.approx(0.2357, abs=1e-3)
    # One short of enough overlapping sessions is None, not a noisy number.
    needed = MIN_REALIZED_VOL_RETURNS + REALIZED_VOL_SKIP_NEWEST_SESSIONS
    assert portfolio_daily_vol_pct(
        _held(X=100), {"X": _steady(1.0, sessions=needed - 1)},
    ) is None
    assert portfolio_daily_vol_pct(
        _held(X=100), {"X": _steady(1.0, sessions=needed)},
    ) is not None


def test_returns_are_keyed_on_dates_not_on_position_in_the_list():
    """Bars are read by DATE, never by index. Lining two holdings up by
    position instead would pair one symbol's Tuesday with another's
    Thursday whenever their series start on different days — silently, and
    invisibly to a standard deviation."""
    moves = [1.0 if i % 2 else -1.0 for i in range(40)]
    bars = _bars(moves)
    ordered = portfolio_daily_vol_pct(_held(A=100), {"A": bars})
    shuffled = list(bars)
    random.Random(20260911).shuffle(shuffled)
    assert portfolio_daily_vol_pct(_held(A=100), {"A": shuffled}) \
        == pytest.approx(ordered), (
        "the answer changed when the bar list was reordered — the series is "
        "being read by index rather than by date"
    )


def test_holdings_with_different_history_lengths_share_one_date_axis():
    """Returns are computed on the dates every measurable holding has a bar
    for, so each holding's return covers the same interval. A symbol with a
    shorter history shortens the shared axis; it does not shift anyone's
    returns onto the wrong days.

    Calendar gaps in that shared axis — weekends, market holidays, or a
    date one symbol was halted — are NOT splices: no trading happened on
    the missing days for the names being measured, so there is no return
    being skipped over.
    """
    moves = [1.0 if i % 2 else -1.0 for i in range(40)]
    full = _bars(moves)
    later_listing = [b for b in full if b.date >= _EPOCH + timedelta(days=18)]
    both = measure_portfolio_daily_vol(
        _held(A=50, B=50), {"A": full, "B": later_listing},
    )
    # Same underlying series, so a 50/50 book of them moves like one of them.
    assert both.daily_vol_pct == pytest.approx(1.0, rel=0.03)
    assert both.observations <= REALIZED_VOL_WINDOW_SESSIONS
    assert both.symbols_used == ("A", "B")

    # A holding halted for a stretch drops those dates from the shared axis
    # and the estimate stays the same order of magnitude — it does not blow
    # up, and it does not silently void the book.
    halted = [
        b for b in full
        if not _EPOCH + timedelta(days=20) <= b.date <= _EPOCH + timedelta(days=23)
    ]
    gapped = measure_portfolio_daily_vol(
        _held(A=50, B=50), {"A": halted, "B": full},
    )
    assert gapped.daily_vol_pct is not None
    assert 0.5 < gapped.daily_vol_pct / both.daily_vol_pct < 2.0


# ---------------------------------------------------------------------------
# The threshold — the actual behaviour change
# ---------------------------------------------------------------------------

def test_the_alarm_trips_at_a_smaller_loss_when_the_book_is_quiet():
    """In a quiet regime an ordinary bad day is small, so the alarm must
    trip at a smaller absolute loss than the frozen -6.7% would have."""
    quiet = _engine(0.5)
    assert quiet.daily_loss_limit_pct < 6.7
    assert quiet.check_daily_loss(baseline=100_000, daily_pnl=-4_000) is not None
    # ... and the old fixed basis said nothing about that same day.
    assert RiskRuleEngine(_cfg()).check_daily_loss(
        baseline=100_000, daily_pnl=-4_000,
    ) is None


def test_the_alarm_tolerates_a_larger_loss_when_the_book_is_volatile():
    """The other half. A book whose holdings really do move 3% a session
    must not be de-levered for a day that is, for that book, unremarkable."""
    violent = _engine(3.0)
    assert violent.daily_loss_limit_pct > 6.7
    assert violent.check_daily_loss(baseline=100_000, daily_pnl=-8_000) is None
    assert RiskRuleEngine(_cfg()).check_daily_loss(
        baseline=100_000, daily_pnl=-8_000,
    ) is not None
    # Not a blank cheque: a genuinely abnormal day still trips.
    assert violent.check_daily_loss(
        baseline=100_000, daily_pnl=-25_000,
    ) is not None


def test_the_threshold_scales_proportionally_with_measured_volatility():
    """Doubling the book's own volatility doubles the alarm distance. This is
    the property a fixed percentage cannot have at all, and it is what makes
    the alarm's firing RATE stable across regimes rather than its absolute
    level."""
    assert _engine(2.0).daily_loss_limit_pct == pytest.approx(
        _engine(1.0).daily_loss_limit_pct * 2, rel=1e-3,
    )


def test_sensitivity_is_the_only_knob_and_it_is_honoured():
    assert _engine(1.0, drawdown_vol_sensitivity=3.0).daily_loss_limit_pct \
        == pytest.approx(
            _engine(1.0, drawdown_vol_sensitivity=6.0).daily_loss_limit_pct / 2,
            rel=1e-3,
        )


def test_the_shipped_sensitivity_is_three_and_is_flagged_provisional():
    """Owner risk-appetite decision 2026-09-11, and the numbers it produces,
    written down rather than assumed.

    3.0 is NOT derived and NOT validated. It replaced 6.7, which existed
    only so behaviour would not jump when the basis changed and which
    measurement showed meant the daily breaker fired on a ~6.7-sigma
    session — effectively dormant. A silent change to it fails here.
    """
    assert DEFAULT_DRAWDOWN_VOL_SENSITIVITY == 3.0
    assert _cfg().drawdown_vol_sensitivity == pytest.approx(3.0)

    # At the reference volatility measured 2026-09-11 from real market data
    # over this desk's own configured universe (~1.0%/session for baskets
    # the size this desk runs). Illustrative of scale only — the live
    # thresholds come from the real book.
    def threshold(window, sigma=1.0):
        return vol_relative_drawdown_threshold_pct(
            daily_vol_pct=sigma, window_sessions=window, sensitivity=3.0,
            fallback_pct=-99.0,
            cap_pct=GROSS_LADDER_ALERT_PCT if window == 20 else None,
        )
    assert threshold(1) == pytest.approx(-3.0, abs=0.01)
    assert threshold(5) == pytest.approx(-6.71, abs=0.01)
    assert threshold(20) == pytest.approx(-13.42, abs=0.01)
    # A ~3% daily loss is a rough day, not a crash — which is the whole
    # point of the move down from 6.7.
    assert 2.0 < -threshold(1) < 4.0


def test_the_twenty_day_cap_no_longer_binds_at_a_normal_book_but_still_exists():
    """Recorded honestly: at sensitivity 3.0 and a ~1%/session book the
    20-day threshold (-13.4%) sits inside the ladder's -20% alert point, so
    the cap is NOT binding today — it was at 6.7. It stays because it is the
    guarantee that this brake can never be asleep past the point the §11.2
    ladder halves the book and alerts the owner, whatever the regime."""
    def twenty(sigma):
        return vol_relative_drawdown_threshold_pct(
            daily_vol_pct=sigma, window_sessions=20, sensitivity=3.0,
            fallback_pct=-20.0, cap_pct=GROSS_LADDER_ALERT_PCT,
        )
    assert twenty(1.0) > GROSS_LADDER_ALERT_PCT   # not binding
    assert twenty(1.49) > GROSS_LADDER_ALERT_PCT  # still not
    for violent in (1.6, 2.0, 5.0, 8.0):          # binding, and capped
        assert twenty(violent) == pytest.approx(GROSS_LADDER_ALERT_PCT)


# ---------------------------------------------------------------------------
# The relative severity structure — the part that IS research-grounded
# ---------------------------------------------------------------------------

def test_window_scaling_is_square_root_of_time():
    """Van Hemert/Ganz/Harvey, JPM 2020. Unchanged by either basis change,
    and expressed once rather than as three per-window multiples that could
    drift apart (which is exactly how bug 1 happened)."""
    def threshold(window):
        return vol_relative_drawdown_threshold_pct(
            daily_vol_pct=1.0, window_sessions=window,
            sensitivity=3.0, fallback_pct=-99.0,
        )
    assert threshold(5) == pytest.approx(threshold(1) * math.sqrt(5), rel=1e-3)
    assert threshold(20) == pytest.approx(threshold(1) * math.sqrt(20), rel=1e-3)


def test_severity_order_holds_at_every_volatility():
    """A shorter window may never tolerate a bigger loss than a longer one,
    and no window may sleep past the ladder's owner-alert point."""
    for sigma in (0.25, 0.5, 1.0, 2.0, 5.0):
        result = _pipeline(_rows(26), sigma=sigma)._compute_recent_performance(
            current_equity=100_000.0,
        )
        daily = -_engine(sigma).daily_loss_limit_pct
        assert 0 > daily >= result["drawdown_5d_threshold_pct"] \
            >= result["drawdown_20d_threshold_pct"] >= GROSS_LADDER_ALERT_PCT, (
            f"severity order broke at sigma={sigma}%"
        )


# ---------------------------------------------------------------------------
# Fallback and fail-safe direction
# ---------------------------------------------------------------------------

def test_no_provider_at_all_behaves_exactly_as_before():
    """Every existing fixture in this repo builds the engine with a config
    only. Those must be untouched by this change."""
    assert RiskRuleEngine(_cfg()).daily_loss_limit_pct == pytest.approx(6.7)


def test_an_explicit_override_still_wins_over_everything():
    """Unchanged precedence: an operator who sets `max_daily_loss_pct` is
    deliberately opting out of volatility-relative alarms."""
    engine = _engine(3.0, max_daily_loss_pct=3.0)
    assert engine.daily_loss_limit_pct == pytest.approx(3.0)
    violation = engine.check_daily_loss(baseline=100_000, daily_pnl=-4_000)
    assert violation is not None
    assert violation.limit == pytest.approx(3.0)


def test_production_settings_do_not_pin_the_breaker_to_a_fixed_percent():
    """Regression guard on a real trap: `config/settings.yaml` used to carry
    `max_daily_loss_pct: 6.7`, which is an override that beats every
    derivation — leaving it there would have made this whole change inert in
    production while every unit test still passed."""
    from pathlib import Path

    import yaml
    settings = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    risk = yaml.safe_load(settings.read_text(encoding="utf-8"))["risk"]
    assert risk.get("max_daily_loss_pct") is None, (
        "a literal here opts the desk out of volatility-relative loss "
        "alarms — see the comment block above it in settings.yaml"
    )
    assert risk.get("drawdown_vol_sensitivity") == pytest.approx(
        DEFAULT_DRAWDOWN_VOL_SENSITIVITY,
    ), "settings and the shipped default must not disagree silently"


def test_a_broken_volatility_read_never_disables_the_breaker():
    """Fail-safe direction. Anything that is not a usable positive number —
    a provider that raises, a None, a NaN, a string — must degrade to the
    fixed percentage, not to no limit at all."""
    def boom():
        raise RuntimeError("market data gone")
    for provider in (
        boom, lambda: None, lambda: float("nan"), lambda: float("inf"),
        lambda: 0.0, lambda: -1.0, lambda: "1.0", lambda: True,
    ):
        engine = RiskRuleEngine(_cfg(), portfolio_vol_provider=provider)
        assert engine.daily_loss_limit_pct == pytest.approx(6.7)
        assert engine.check_daily_loss(
            baseline=100_000, daily_pnl=-8_000,
        ) is not None


# ---------------------------------------------------------------------------
# The rolling-return brakes, end to end through the pipeline
# ---------------------------------------------------------------------------

def _rows(n: int, *, start: float = 100_000.0) -> list[dict]:
    """`daily_pnl` rows, newest-first, flat in value. The ROLLING RETURNS
    still come from the account's own equity — that is what the brake
    judges. Only the YARDSTICK moved off the equity curve."""
    return [{"total_value": start} for _ in range(n)]


def _pipeline(
    rows: list[dict], *, sigma: float | None = None,
    risk: RiskConfig | None = None,
) -> TradingPipeline:
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.config = SimpleNamespace(risk=risk or _cfg())
    pipeline.db = MagicMock()
    pipeline.db.get_daily_pnl.return_value = rows
    pipeline.held_book_daily_vol_pct = lambda: sigma
    return pipeline


def test_rolling_brakes_tighten_in_a_quiet_regime():
    result = _pipeline(_rows(25), sigma=0.5)._compute_recent_performance(
        current_equity=100_000.0,
    )
    assert result["held_book_daily_vol_pct"] == pytest.approx(0.5)
    # Both brakes strictly tighter than the fixed -15% / -20% they replace.
    assert -15.0 < result["drawdown_5d_threshold_pct"] < 0
    assert -20.0 < result["drawdown_20d_threshold_pct"] < 0


def test_rolling_brakes_loosen_in_a_violent_regime_but_never_past_the_ladder():
    result = _pipeline(_rows(25), sigma=4.0)._compute_recent_performance(
        current_equity=100_000.0,
    )
    # sqrt(time) alone would put the 20-day brake near -54%; the ladder
    # constraint binds instead.
    assert result["drawdown_20d_threshold_pct"] == pytest.approx(
        GROSS_LADDER_ALERT_PCT,
    )
    # And the 5-day brake is clamped to it rather than going deeper.
    assert result["drawdown_5d_threshold_pct"] == pytest.approx(
        result["drawdown_20d_threshold_pct"],
    )


def test_rolling_brakes_fall_back_when_there_is_nothing_to_measure():
    """An all-cash book, or holdings without price history. The brakes must
    read exactly the risk-unit-derived percentages main ships."""
    cfg = _cfg()
    result = _pipeline(
        _rows(25), sigma=None, risk=cfg,
    )._compute_recent_performance(current_equity=100_000.0)
    assert result["held_book_daily_vol_pct"] is None
    assert result["drawdown_5d_threshold_pct"] == pytest.approx(
        cfg.drawdown_5d_threshold_pct,
    )
    assert result["drawdown_20d_threshold_pct"] == pytest.approx(
        cfg.drawdown_20d_threshold_pct,
    )


def test_the_same_bad_run_can_trip_or_not_depending_on_the_book():
    """The behaviour a fixed percentage cannot produce, end to end: one
    rolling-window return, two verdicts, because the two books hold things
    with different normal movement."""
    bad_run_pct = -6.0

    def trips(sigma: float) -> bool:
        rows = _rows(26)
        current = rows[5]["total_value"] * (1 + bad_run_pct / 100)
        rows[0] = {"total_value": current}
        result = _pipeline(rows, sigma=sigma)._compute_recent_performance(
            current_equity=current,
        )
        assert result["rolling_5d_pct"] == pytest.approx(bad_run_pct, abs=0.02)
        return result["in_drawdown"]

    assert trips(0.5) is True, "a -6% week is a real alarm for a calm book"
    assert trips(2.0) is False, "the same -6% week is ordinary for a wild one"


def test_the_pipeline_measures_from_real_holdings_and_the_shared_market_feed():
    """Wiring test. The live yardstick must come from the CURRENT positions,
    their CURRENT weights, and the same market feed the rest of the desk
    reads — not a second price path, and not the account's equity curve."""
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.config = SimpleNamespace(
        risk=_cfg(), trading=SimpleNamespace(lookback_days=1800),
    )
    pipeline.broker = MagicMock()
    pipeline.broker.get_account.return_value = SimpleNamespace(
        portfolio_value=100_000.0,
    )
    pipeline.broker.get_positions.return_value = [
        SimpleNamespace(symbol="NVDA", market_value=20_000.0, current_price=10.0),
        SimpleNamespace(symbol="AAPL", market_value=20_000.0, current_price=10.0),
    ]
    moves = [1.0 if i % 2 else -1.0 for i in range(30)]
    pipeline.market = MagicMock()
    pipeline.market.get_ohlcv.side_effect = lambda sym, days: _bars(moves)

    estimate = pipeline.measure_held_book_daily_vol()
    # 40% deployed in two things that move identically at 1% a session.
    assert estimate.daily_vol_pct == pytest.approx(0.4, rel=0.03)
    assert estimate.symbols_used == ("AAPL", "NVDA")
    assert pipeline.market.get_ohlcv.call_count == 2

    # Memoized: the breaker fires from six places a session and each
    # measurement is one market fetch per holding.
    assert pipeline.held_book_daily_vol_pct() == pytest.approx(
        estimate.daily_vol_pct,
    )
    assert pipeline.market.get_ohlcv.call_count == 2

    # A broken market feed degrades to the fallback, never to no limit.
    pipeline._held_book_vol_memo = None
    pipeline.market.get_ohlcv.side_effect = RuntimeError("feed down")
    assert pipeline.held_book_daily_vol_pct() is None

    # A broker read that fails does the same.
    pipeline._held_book_vol_memo = None
    pipeline.broker.get_positions.side_effect = RuntimeError("broker down")
    assert pipeline.held_book_daily_vol_pct() is None


def test_an_all_cash_pipeline_reports_no_yardstick_and_fetches_nothing():
    """The ramp's real starting state. No holdings means no measurement, and
    no reason to pay for price history either."""
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.config = SimpleNamespace(
        risk=_cfg(), trading=SimpleNamespace(lookback_days=1800),
    )
    pipeline.broker = MagicMock()
    pipeline.broker.get_account.return_value = SimpleNamespace(
        portfolio_value=100_000.0,
    )
    pipeline.broker.get_positions.return_value = []
    pipeline.market = MagicMock()

    estimate = pipeline.measure_held_book_daily_vol()
    assert estimate.daily_vol_pct is None
    assert "cash" in estimate.reason
    pipeline.market.get_ohlcv.assert_not_called()


def test_volatility_is_never_used_to_size_a_position():
    """Guard on the scope boundary. Volatility-target exposure scaling was
    separately REJECTED for this desk (docs/OUTCOME.md) — this machinery is
    the alarm's yardstick and nothing else. `_compute_recent_performance`
    must not have grown a sizing output."""
    result = _pipeline(_rows(25), sigma=2.0)._compute_recent_performance(
        current_equity=100_000.0,
    )
    forbidden = ("size", "scale", "target_vol", "allocation", "exposure_x")
    for key in result:
        assert not any(word in key for word in forbidden), (
            f"`{key}` looks like volatility-based SIZING leaking out of the "
            f"drawdown alarm — that technique is rejected for this desk"
        )
