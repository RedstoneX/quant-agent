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
    the alarms for the whole book, and must not silently make the book look
    calmer than it is either (item 92). It is NAMED in `unmeasured_symbols`,
    but its weight still counts: it is assumed to move like the measured
    peers' median that session, so a book where every holding moves
    identically measures the SAME volatility whether or not the fourth
    holding's own history happens to be usable."""
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
    # Every holding here moves identically, so imputing NEWCO's session
    # return from its peers' median reproduces the fully-measurable case
    # almost exactly (up to the peer median vs. the single true series).
    full = measure_portfolio_daily_vol(
        _held(A=25, B=25, C=25, NEWCO=25),
        {**bars, "NEWCO": _steady(1.0)},
    )
    assert estimate.daily_vol_pct == pytest.approx(full.daily_vol_pct, rel=0.03)


def test_dropping_an_unmeasurable_holding_would_have_tightened_the_limit():
    """item 92's reproduction. Before the fix, `NEWCO`'s weight was
    excluded from the basket entirely (contributing zero), which made a
    book that actually moves 1%/session measure as if it only moved 0.75%
    — three of its four equal holdings' worth. That understated volatility
    feeds `vol_relative_drawdown_threshold_pct` directly, so the daily-loss
    limit came out tighter than the book's real behaviour justifies, making
    the halt more likely to fire on an ordinary day. Simulating the OLD
    (drop-entirely) arithmetic here, without touching the shipped function,
    pins the number the fix must not reproduce."""
    bars = {
        "A": _steady(1.0), "B": _steady(1.0), "C": _steady(1.0),
        "NEWCO": _steady(1.0, sessions=3),
    }
    fixed = measure_portfolio_daily_vol(
        _held(A=25, B=25, C=25, NEWCO=25), bars,
    )
    # The old behaviour: NEWCO's 25% weight contributes nothing.
    old_dropped_returns = []
    for i in range(1, REALIZED_VOL_WINDOW_SESSIONS + 1):
        old_dropped_returns.append(0.75 if i % 2 else -0.75)
    import statistics as _stats
    old_sigma = _stats.stdev(old_dropped_returns)
    assert old_sigma < fixed.daily_vol_pct
    old_limit = vol_relative_drawdown_threshold_pct(
        daily_vol_pct=old_sigma, window_sessions=1, sensitivity=3.0,
        fallback_pct=-6.7,
    )
    fixed_limit = vol_relative_drawdown_threshold_pct(
        daily_vol_pct=fixed.daily_vol_pct, window_sessions=1, sensitivity=3.0,
        fallback_pct=-6.7,
    )
    # A smaller (tighter) magnitude limit under the old, defective arithmetic.
    assert abs(old_limit) < abs(fixed_limit)


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


# ==========================================================================
# 2026-09-14, docs/WORK.md item 32 — the two measurement defects the halt
# change also fixed. Both are about the breaker comparing two things that
# were never the same object.
# ==========================================================================

def _pos(symbol, market_value, *, intraday=0.0, qty=1.0):
    from src.models import Position
    return Position(
        symbol=symbol, qty=qty, avg_entry=100.0, current_price=100.0,
        market_value=market_value, unrealized_pnl=0.0,
        unrealized_intraday_pnl=intraday, sector="ETF",
    )


def test_the_cash_park_is_excluded_from_the_volatility_yardstick():
    """`gross_exposure`, `book_exposure`, `sector_side_gross`, the
    stop-coverage audit and every LLM-facing view all drop the sweep
    vehicle. `normalized_holding_weights` was the one risk calculation that
    did not, so the daily breaker's denominator was neither the risk book
    nor the account but a third object: on the archived book the park was
    78% of the gross weight it was measured over."""
    positions = [_pos("SGOV", 7530.0), _pos("MSFT", 501.0), _pos("V", 374.0)]
    equity = 9822.37

    with_park = normalized_holding_weights(positions, equity)
    without = normalized_holding_weights(
        positions, equity, cash_park_symbol="SGOV",
    )

    assert "SGOV" in with_park, "no cash_park_symbol given → nothing excluded"
    assert "SGOV" not in without
    assert set(without) == {"MSFT", "V"}
    # The real holdings keep their weights: this excludes, it does not
    # renormalise. Renormalising would destroy the deployment-scaling the
    # whole design rests on.
    assert without["MSFT"] == pytest.approx(with_park["MSFT"])
    assert without["V"] == pytest.approx(with_park["V"])


def test_the_park_exclusion_uses_the_configured_symbol_not_a_hardcoded_one():
    positions = [_pos("BIL", 5000.0), _pos("SGOV", 1000.0)]
    weights = normalized_holding_weights(
        positions, 10_000.0, cash_park_symbol="bil",
    )
    assert set(weights) == {"SGOV"}


def test_excluding_the_park_can_only_tighten_the_threshold_never_loosen_it():
    """Parked cash barely moves, so dropping it from the basket makes the
    measured volatility SMALLER, which makes the threshold TIGHTER — the
    alarm fires sooner. That direction is the safe one for a brake, and it
    is the reason this fix needed no owner sign-off on a trip level."""
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(40)]

    def bars(series):
        return [{"date": d, "close": c} for d, c in zip(days, series)]

    rng = random.Random(11)
    # A real holding that moves, and a park that essentially does not.
    real = [100.0]
    park = [100.0]
    for _ in range(39):
        real.append(real[-1] * (1 + rng.gauss(0, 0.012)))
        park.append(park[-1] * (1 + rng.gauss(0, 0.00005)))
    by_symbol = {"MSFT": bars(real), "SGOV": bars(park)}

    positions = [_pos("SGOV", 7530.0), _pos("MSFT", 2000.0)]
    equity = 10_000.0
    with_park = measure_portfolio_daily_vol(
        normalized_holding_weights(positions, equity), by_symbol,
    )
    without = measure_portfolio_daily_vol(
        normalized_holding_weights(
            positions, equity, cash_park_symbol="SGOV",
        ), by_symbol,
    )

    assert without.daily_vol_pct <= with_park.daily_vol_pct


# ---- numerator and denominator must measure the same object -------------

def test_held_book_daily_pnl_sums_the_book_and_drops_the_park():
    from src.risk.rules import held_book_daily_pnl
    positions = [
        _pos("SGOV", 7530.0, intraday=-1.0),
        _pos("MSFT", 500.0, intraday=-40.0),
        _pos("V", 370.0, intraday=-10.0),
    ]
    pnl, measurable = held_book_daily_pnl(positions, cash_park_symbol="SGOV")
    assert measurable is True
    assert pnl == pytest.approx(-50.0)


def test_an_unreadable_intraday_change_is_not_treated_as_flat():
    """A holding whose day change cannot be read is not assumed to have not
    moved — assuming flat understates a loss and delays a brake."""
    from src.risk.rules import held_book_daily_pnl

    class Opaque:
        symbol = "XYZ"
        unrealized_intraday_pnl = float("nan")

    pnl, measurable = held_book_daily_pnl(
        [_pos("MSFT", 500.0, intraday=-40.0), Opaque()],
    )
    assert measurable is False
    assert pnl == pytest.approx(-40.0)


def test_the_numerator_is_the_account_at_every_rung():
    """2026-09-20 owner ruling, docs/WORK.md item 32. The 2026-09-14 repair
    closed the numerator/denominator mismatch by shrinking the NUMERATOR to
    the held book. That made the breaker blind to realized losses — the
    desk's designed, normal loss mode — so the mismatch is now closed from
    the denominator side instead: every rung is a percent of the ACCOUNT, so
    the account's whole-day change is the numerator at every rung, realized
    losses and commissions included."""
    from src.risk.rules import daily_loss_numerator
    positions = [_pos("MSFT", 500.0, intraday=-40.0)]

    pnl, basis = daily_loss_numerator(
        -900.0, positions, vol_relative=True, cash_park_symbol="SGOV",
    )
    assert basis == "account"
    assert pnl == pytest.approx(-900.0)


def test_the_breaker_is_not_blind_to_realized_losses():
    """The regression the 2026-09-14 held-book numerator introduced, pinned
    so it cannot come back: names stop out for a real account-level loss and
    the survivors sit flat. Under the held-book numerator the book read flat
    and nothing tripped on exactly the day the breaker is meant to bind."""
    from src.risk.rules import daily_loss_numerator
    survivors = [_pos("MSFT", 500.0, intraday=0.0)]

    pnl, basis = daily_loss_numerator(
        -4000.0, survivors, vol_relative=True, cash_park_symbol="SGOV",
    )
    assert basis == "account"
    assert pnl == pytest.approx(-4000.0)


def test_the_numerator_is_the_account_when_the_threshold_is_a_fixed_percent():
    """The same rule pointed the other way, and the reason it is a rule
    rather than a preference. Rungs 1 and 3 of `daily_loss_limit_pct` are
    percentages OF THE ACCOUNT; feeding the held book's loss to those would
    be the identical mismatch reversed."""
    from src.risk.rules import daily_loss_numerator
    positions = [_pos("MSFT", 500.0, intraday=-40.0)]

    pnl, basis = daily_loss_numerator(
        -900.0, positions, vol_relative=False, cash_park_symbol="SGOV",
    )
    assert basis == "account"
    assert pnl == pytest.approx(-900.0)


def test_a_flat_held_book_on_a_losing_account_falls_back_to_the_account():
    """A broker that omits the intraday field reports exactly $0.00 on every
    position, and nothing downstream can tell that apart from a genuinely
    unmoved book. So a flat read is not trusted to SUPPRESS a breach the
    account-wide number raises — fail toward not trading."""
    from src.risk.rules import daily_loss_numerator
    positions = [_pos("MSFT", 500.0, intraday=0.0)]

    pnl, basis = daily_loss_numerator(-900.0, positions, vol_relative=True)
    assert basis == "account"
    assert pnl == pytest.approx(-900.0)


def test_a_flat_account_on_a_flat_book_reports_no_loss():
    from src.risk.rules import daily_loss_numerator
    pnl, basis = daily_loss_numerator(
        0.0, [_pos("MSFT", 500.0, intraday=0.0)], vol_relative=True,
    )
    assert basis == "account"
    assert pnl == pytest.approx(0.0)


def test_an_empty_book_still_reports_the_account_day_change():
    """An account can lose money on a day it ends holding nothing — every
    name stopped out. The old held-book numerator reported 0.0 here."""
    from src.risk.rules import daily_loss_numerator
    pnl, basis = daily_loss_numerator(-900.0, [], vol_relative=True)
    assert basis == "account"
    assert pnl == pytest.approx(-900.0)


def test_the_limit_reports_which_rung_produced_it():
    """`daily_loss_limit_pct` has three rungs and reading the number no
    longer tells you which one fired. The numerator has to match, so the
    basis is reported rather than guessed."""
    engine = RiskRuleEngine(_cfg())
    assert engine.daily_loss_limit_basis() == RiskRuleEngine.FIXED_BASIS

    engine_vol = RiskRuleEngine(_cfg(), portfolio_vol_provider=lambda: 0.25)
    assert engine_vol.daily_loss_limit_basis() == RiskRuleEngine.VOL_RELATIVE_BASIS
    assert engine_vol.daily_loss_limit_pct == pytest.approx(0.75, abs=0.01)

    explicit = RiskRuleEngine(
        _cfg(max_daily_loss_pct=3.0), portfolio_vol_provider=lambda: 0.25,
    )
    assert explicit.daily_loss_limit_basis() == RiskRuleEngine.FIXED_BASIS
    assert explicit.daily_loss_limit_pct == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# docs/WORK.md item 32, reconciliation pass 2026-09-14.
#
# Neither test below moves a trip level. Both pin a place where the machinery
# could report something untrue: a yardstick that outlives the session it was
# measured in, and a brake that says nothing when it cannot see.
# ---------------------------------------------------------------------------

def test_the_volatility_memo_cannot_outlive_the_session_it_measured():
    """The memo keys on the TRADING DATE as well as the book.

    `src/scheduler.py` holds ONE `TradingPipeline` for the life of the
    process, so this memo outlives a session. Keyed on holdings and weights
    alone, a book whose weights round to the same 4dp on two consecutive days
    would be priced today against yesterday's volatility — every one of the
    three loss alarms silently reading a stale yardstick. The comment on the
    memo claimed a date component the key did not have; this pins the code.
    """
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.config = SimpleNamespace(
        risk=_cfg(), trading=SimpleNamespace(lookback_days=1800),
    )
    pipeline.broker = MagicMock()
    pipeline.broker.get_account.return_value = SimpleNamespace(
        portfolio_value=100_000.0,
    )
    pipeline.broker.get_positions.return_value = [
        SimpleNamespace(symbol="NVDA", market_value=40_000.0, current_price=10.0),
    ]
    moves = [1.0 if i % 2 else -1.0 for i in range(30)]
    pipeline.market = MagicMock()
    pipeline.market.get_ohlcv.side_effect = lambda sym, days: _bars(moves)

    first = pipeline.measure_held_book_daily_vol()
    assert first.daily_vol_pct is not None
    assert pipeline.market.get_ohlcv.call_count == 1

    # Same day, identical book: served from the memo, no second fetch.
    pipeline.measure_held_book_daily_vol()
    assert pipeline.market.get_ohlcv.call_count == 1

    # A new trading date with a book whose weights round identically must
    # re-measure rather than serve yesterday's number.
    stored_key, stored_estimate = pipeline._held_book_vol_memo
    assert isinstance(stored_key, tuple) and len(stored_key) == 2
    pipeline._held_book_vol_memo = (
        ("1999-01-04", stored_key[1]), stored_estimate,
    )
    pipeline.measure_held_book_daily_vol()
    assert pipeline.market.get_ohlcv.call_count == 2, (
        "a yardstick measured on another date was served to today's alarms"
    )


def test_an_unmeasurable_rolling_window_is_stated_not_printed_as_a_null():
    """A brake that cannot see must not read as an all-clear.

    `_compute_recent_performance` returns None for a window with too little
    equity history (it reads rows[5] / rows[20], so it needs 6 and 21
    recorded sessions). Rendered as "None%" in the Portfolio Manager's
    prompt that reads to a model as a number near zero, i.e. as "we are not
    in drawdown", when the truth is that the brake is blind. The desk is in
    exactly that state after the 2026-09-02 reset: rows are written only by
    an evening run, so a paused desk does not accrue them.
    """
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.models import Position, TechAnalysisResult, TechReasoningChain

    agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    analyses = [
        TechAnalysisResult(
            symbol="SPY", rating="buy", entry_price=500.0,
            reference_target=530.0, stop_loss=485.0,
            support_levels=[485.0], resistance_levels=[530.0],
            setup_type="range", expected_horizon_sessions=10,
            reasoning="x",
            reasoning_chain=TechReasoningChain(
                trend="x", momentum="x", volatility="x", volume="x",
                support_resistance="x",
            ),
        thesis_invalid_if="closes below support",
    ),
    ]
    positions = [
        Position(symbol="AAPL", qty=5, avg_entry=180.0, current_price=190.0,
                 market_value=950.0, unrealized_pnl=50.0, sector="Technology"),
    ]
    message = agent.build_user_message(
        analyses=analyses, positions=positions, macro_analysis=None,
        cash_balance=9050.0, total_value=10000.0,
        recent_performance={
            "rolling_5d_pct": None,
            "rolling_20d_pct": None,
            "in_drawdown": False,
            "trailing_days": 1,
            "drawdown_5d_threshold_pct": -6.7,
            "drawdown_20d_threshold_pct": -13.4,
        },
    )
    assert "NOT YET MEASURABLE" in message
    assert "None%" not in message
    assert "do not read it as zero" in message
    # The thresholds themselves are still reported: the brake is blind, the
    # level it would fire at is not a secret.
    assert "-6.7" in message and "-13.4" in message


# ---------------------------------------------------------------------------
# docs/WORK.md item 32 — OWNER RULING 2026-09-20.
#
# "I think the nuclear option should only be for a nuclear option. And given
# what you've told me, that's not the case. So this whole premise is
# completely wrong."
#
# The daily breaker's yardstick is the held book's realized volatility, and
# `normalized_holding_weights` deliberately does not renormalise — so a
# 5%-deployed book reconstructed a normal daily move about 5% the size of the
# same basket fully deployed, and the trip point shrank with it. Right for a
# BRAKE (the 5-day / 20-day alarms keep it, and the last test here pins that).
# Wrong for the response that stops the desk trading for the session.
#
# The DAILY breaker now re-expresses that same measurement AT FULL DEPLOYMENT.
# No new constant: same sensitivity, same sqrt(time), same ladder cap, and the
# scale factor is read off the live holdings' gross weight.
# ---------------------------------------------------------------------------


def _deployed_engine(sigma_pct, gross_fraction, **cfg_overrides):
    """A book measuring `sigma_pct` per session at `gross_fraction` deployed.

    Built through the SAME pure function the pipeline's provider uses, so
    these tests exercise the real scaling rather than a re-implementation of
    it: `src/pipeline.py::breaker_daily_vol_pct` reads one measurement and
    hands exactly these four values to `breaker_vol_yardstick_pct`.
    """
    from src.risk.rules import breaker_vol_yardstick_pct
    cfg = _cfg(**cfg_overrides)
    yardstick = breaker_vol_yardstick_pct(
        sigma_pct, gross_fraction, sensitivity=3.0,
        fallback_pct=cfg.effective_max_daily_loss_pct,
    )
    return RiskRuleEngine(cfg, portfolio_vol_provider=lambda: yardstick)


def test_a_lightly_deployed_book_having_an_ordinary_bad_day_does_not_halt():
    """(a) THE OLD FAILURE CASE. The measured example from the incident
    history: 5% of equity in one name, that name has an unremarkable 6%
    single-stock day. The account is down 0.30%.

    Before: the yardstick was 3 x (1.5% x 0.05) = 0.225% of equity, so 0.30%
    breached it and the desk halted. After: the yardstick is the same book at
    full deployment, 3 x 1.5% = 4.5% of equity, and a 0.30% day is nowhere
    near it."""
    name_vol_pct = 1.5
    deployment = 0.05
    engine = _deployed_engine(name_vol_pct * deployment, deployment)

    # The threshold no longer collapses with deployment.
    assert engine.daily_loss_limit_pct == pytest.approx(4.5, abs=0.01)
    assert engine.daily_loss_limit_basis() == RiskRuleEngine.VOL_RELATIVE_BASIS

    # A 6% day in the one name held at 5% of equity: -0.30% of the account.
    equity = 100_000.0
    day_loss = -equity * deployment * 0.06
    assert engine.check_daily_loss(equity, day_loss) is None

    # And the old basis is what would have fired, so this test is pinning a
    # real change rather than an arithmetic coincidence.
    old_style = RiskRuleEngine(
        _cfg(), portfolio_vol_provider=lambda: name_vol_pct * deployment,
    )
    assert old_style.check_daily_loss(equity, day_loss) is not None


def test_a_genuinely_severe_account_wide_loss_still_halts():
    """(b) THE BREAKER IS NOT DISABLED. Same lightly-deployed book, but the
    account is down 6% on the day — names stopped out, realized. That is
    larger than a 3-sigma day would cost this book fully deployed, and it
    still fires. Note the loss is ACCOUNT-wide and the surviving book is
    flat: the numerator that sees it is the one the 2026-09-20 ruling
    restored."""
    engine = _deployed_engine(1.5 * 0.05, 0.05)
    equity = 100_000.0

    violation = engine.check_daily_loss(equity, -0.06 * equity)
    assert violation is not None
    assert violation.rule == "max_daily_loss_pct"


def test_a_fully_deployed_book_trips_exactly_where_it_did_before():
    """The regime the desk is ramping toward is untouched. At full deployment
    the scale factor is 1.0 and the trip point is the pre-2026-09-20 one."""
    scaled = _deployed_engine(1.2, 1.0)
    unscaled = RiskRuleEngine(_cfg(), portfolio_vol_provider=lambda: 1.2)
    assert scaled.daily_loss_limit_pct == pytest.approx(
        unscaled.daily_loss_limit_pct,
    )
    assert scaled.daily_loss_limit_pct == pytest.approx(3.6, abs=0.01)


def test_a_levered_book_is_never_scaled_down():
    """The floor only ever raises the yardstick. A book levered past 1.0x
    gross genuinely can lose more in a day, and dividing by its gross would
    have TIGHTENED the breaker on the most exposed book the desk can hold."""
    levered = _deployed_engine(2.4, 2.0)
    assert levered.daily_loss_limit_pct == pytest.approx(7.2, abs=0.01)


def test_the_trip_point_is_the_same_share_of_equity_at_every_deployment():
    """The property the ruling asked for, stated directly: how much of the
    ACCOUNT has to be lost before the desk stops trading must not depend on
    how far along the ramp from cash the desk happens to be.

    The old basis made it collapse — 4.5% of equity fully deployed, 0.045%
    at 1% deployed. It is now 4.5% throughout. Note this is invariance in
    ACCOUNT terms, which is the whole point: a 3-sigma day in a name that is
    5% of the book is no longer a reason to stop, because it costs the
    account almost nothing.

    **IT IS INVARIANT ONLY WHILE THE BOUND DOES NOT BIND** — see
    `test_the_trip_point_is_not_invariant_once_the_bound_binds`, which pins
    the other regime rather than leaving the claim overstated. An adversary
    pass caught this test asserting invariance while only ever exercising a
    volatility inside the invariant band."""
    name_vol_pct = 1.5
    limits = []
    for deployment in (1.0, 0.5, 0.25, 0.05, 0.01, 0.001):
        engine = _deployed_engine(name_vol_pct * deployment, deployment)
        limits.append(engine.daily_loss_limit_pct)
    for limit in limits:
        assert limit == pytest.approx(limits[0], rel=1e-9)
    assert limits[0] == pytest.approx(4.5, abs=0.01)


def test_a_near_empty_book_does_not_halt_on_a_single_dollar():
    """THE ROUNDING DEFECT. `vol_relative_drawdown_threshold_pct` used to end
    `-round(magnitude, 2)`. At about 0.1% deployment the threshold rounded
    away to -0.0, the basis still reported itself as a MEASUREMENT, and a
    limit of zero means any loss at all breaches it — one dollar halted the
    desk. Rendering rounds; a control value does not."""
    from src.risk.rules import vol_relative_drawdown_threshold_pct

    tiny = vol_relative_drawdown_threshold_pct(
        daily_vol_pct=0.0015, window_sessions=1, sensitivity=3.0,
        fallback_pct=-6.7, cap_pct=-20.0,
    )
    assert tiny < 0.0
    assert tiny != pytest.approx(0.0, abs=1e-9)

    engine = _deployed_engine(0.0015, 0.001)
    assert engine.daily_loss_limit_pct > 0.0
    assert engine.check_daily_loss(100_000.0, -1.0) is None


def test_an_unreadable_deployment_leaves_the_yardstick_unscaled():
    """Fail-soft: a deployment that cannot be read yields the
    pre-2026-09-20 behaviour — the measurement served unchanged — rather
    than a guessed scale factor."""
    from src.risk.rules import breaker_vol_yardstick_pct
    fallback = _cfg().effective_max_daily_loss_pct
    for gross in (None, 0.0, -1.0, float("nan"), True, "x"):
        assert breaker_vol_yardstick_pct(
            0.3, gross, sensitivity=3.0, fallback_pct=fallback,
        ) == pytest.approx(0.3)


def test_an_unmeasurable_book_never_produces_a_yardstick():
    """No measurement means no scaled measurement. The caller falls through
    to the fixed percentage; nothing is invented from the deployment alone."""
    from src.risk.rules import breaker_vol_yardstick_pct
    fallback = _cfg().effective_max_daily_loss_pct
    for sigma in (None, 0.0, -1.0, float("nan"), True, "x"):
        assert breaker_vol_yardstick_pct(
            sigma, 0.5, sensitivity=3.0, fallback_pct=fallback,
        ) is None


def test_an_all_cash_book_still_falls_back_to_the_fixed_percentage():
    """Nothing held, nothing measurable: the fixed rung governs, exactly as
    before, and it is honestly reported as the fixed rung rather than as a
    measurement."""
    engine = RiskRuleEngine(_cfg(), portfolio_vol_provider=lambda: None)
    assert engine.daily_loss_limit_basis() == RiskRuleEngine.FIXED_BASIS
    assert engine.daily_loss_limit_pct == pytest.approx(
        _cfg().effective_max_daily_loss_pct,
    )


def test_the_rolling_brakes_keep_the_deployment_scaled_yardstick():
    """The scaling is the DAILY breaker's alone. The 5-day / 20-day brakes
    only halve new BUY size, and deployment-scaling is correct for a brake —
    the incident history is explicit that the property is right there and
    wrong only when attached to a session-stopping response."""
    from src.risk.rules import breaker_vol_yardstick_pct

    fallback = _cfg().effective_max_daily_loss_pct
    # The breaker's own yardstick is the re-expressed one...
    assert breaker_vol_yardstick_pct(
        0.075, 0.05, sensitivity=3.0, fallback_pct=fallback,
    ) == pytest.approx(1.5)
    # ...and `_compute_recent_performance` never calls it: the rolling
    # brakes read `held_book_daily_vol_pct` straight off the pipeline, so
    # the scaling is structurally unable to reach them.
    import inspect
    from src import pipeline as _pipeline
    source = inspect.getsource(_pipeline.TradingPipeline._compute_recent_performance)
    assert "held_book_daily_vol_pct()" in source
    assert "breaker_daily_vol_pct" not in source


def test_gross_weight_fraction_is_unsigned_and_park_free_by_construction():
    """A market-neutral book IS deployed even though its signed weights
    cancel, so the deployment read is gross. The cash park never reaches
    here — `normalized_holding_weights` already dropped it."""
    from src.risk.rules import gross_weight_fraction

    assert gross_weight_fraction({"A": 0.3, "B": -0.3}) == pytest.approx(0.6)
    assert gross_weight_fraction({}) == pytest.approx(0.0)
    assert gross_weight_fraction(None) == pytest.approx(0.0)
    assert gross_weight_fraction({"A": float("nan")}) == pytest.approx(0.0)


def test_the_measurement_is_linear_in_the_weights():
    """THE LOAD-BEARING CLAIM OF THE WHOLE DESIGN, against the real
    measurement rather than against a re-implementation of it.

    `breaker_vol_yardstick_pct` SCALES the measured volatility instead of
    re-measuring on renormalised weights, on the grounds that the basket
    return series is linear in the weights — so multiplying every weight by
    c multiplies the standard deviation by exactly c. If that were false
    anywhere (a normalisation, a clamp, a winsorisation, a
    minimum-observation rule that is not scale-free, or item 92's imputation
    of a name with no usable history from its peers), the scaled number
    would be wrong and the design would have to re-measure."""
    from src.risk.rules import measure_portfolio_daily_vol

    bars = {
        "AAA": _bars([1.4, -2.1, 0.8, 1.9, -1.2, 2.3, -0.7, 1.1, -1.8,
                      0.6, 2.0, -1.4]),
        "BBB": _bars([-0.9, 1.7, -2.4, 0.5, 1.3, -1.1, 2.2, -0.4, 1.6,
                      -2.0, 0.7, 1.0]),
        # Deliberately short: this one has no usable return series of its
        # own and exercises item 92's peer imputation inside the scaling.
        "CCC": _bars([0.5]),
    }
    base = {"AAA": 0.40, "BBB": -0.25, "CCC": 0.10}
    reference = measure_portfolio_daily_vol(base, bars)
    assert reference.daily_vol_pct is not None

    for c in (0.5, 0.05, 0.001, 2.0):
        scaled = measure_portfolio_daily_vol(
            {sym: w * c for sym, w in base.items()}, bars,
        )
        assert scaled.daily_vol_pct == pytest.approx(
            reference.daily_vol_pct * c, rel=1e-9,
        )


def test_the_scaling_cannot_loosen_the_breaker_without_bound():
    """THE HOLE A FRESH ADVERSARY PASS FOUND IN THE FIRST VERSION OF THIS
    FIX. A naive `sigma / gross` rises without limit as the book shrinks,
    which inverts the point of the change: names stop out, gross falls, and
    the breaker gets LOOSER exactly as the damage accrues — the same shape as
    the self-tightening noose this desk has been burned by, running the more
    dangerous way.

    The scaled yardstick is bounded so the threshold it produces can never
    exceed the desk's own ratified fixed circuit-breaker level, which is what
    governs when there is nothing to measure at all. Scaling may loosen the
    breaker up to the level already ratified for an unmeasurable book, and
    not one basis point past it."""
    from src.risk.rules import breaker_vol_yardstick_pct
    cfg = _cfg()
    fallback = cfg.effective_max_daily_loss_pct

    # A violently volatile remnant at 2% deployment: unbounded scaling would
    # give 3 x (0.16 / 0.02) = 24% of equity before the ladder cap.
    engine = _deployed_engine(0.16, 0.02)
    assert engine.daily_loss_limit_pct == pytest.approx(fallback, abs=0.01)

    # And the bound holds however small the remnant gets.
    for gross in (0.02, 0.005, 0.001, 1e-6):
        limit = 3.0 * breaker_vol_yardstick_pct(
            0.16 * gross, gross, sensitivity=3.0, fallback_pct=fallback,
        )
        assert limit <= fallback + 1e-9


def test_a_genuinely_violent_fully_deployed_book_is_not_clipped_by_the_bound():
    """The mirror, and the reason the bound is `max(unscaled, fallback)`
    rather than a flat cap: a fully-deployed book in a violent regime should
    be allowed a threshold deeper than the fixed level, because that level
    was derived in a calm one. Nothing is being scaled here — the bound must
    not clip a measurement that stands on its own."""
    engine = _deployed_engine(4.0, 1.0)
    assert engine.daily_loss_limit_pct == pytest.approx(12.0, abs=0.01)
    assert engine.daily_loss_limit_pct > _cfg().effective_max_daily_loss_pct


def test_the_breaker_does_not_get_looser_as_the_book_stops_out():
    """Stated as the scenario rather than as arithmetic. A fully-deployed
    book loses most of itself to stops and leaves a small, volatile remnant.
    The yardstick is now measured on that remnant — so without the bound the
    trip point would have RISEN on the worst day of the desk's life."""
    fallback = _cfg().effective_max_daily_loss_pct
    whole_book = _deployed_engine(1.2, 1.0).daily_loss_limit_pct
    remnant = _deployed_engine(0.06, 0.03).daily_loss_limit_pct
    assert remnant <= max(whole_book, fallback) + 1e-9


def test_the_trip_point_is_not_invariant_once_the_bound_binds():
    """THE HONEST OTHER HALF, pinned rather than left as an overstatement.

    Deployment-invariance is what the scaling buys, but the bound that stops
    the scaling loosening the breaker without limit necessarily breaks it in
    the regime where the bound binds: above roughly 2.2%/day of
    full-deployment volatility, 3-sigma already exceeds the ratified fixed
    rung, so the clip returns the unscaled — deployment-collapsing — number
    instead. That is the conservative direction (a more violent book gets a
    TIGHTER breaker as it de-deploys, never a looser one), and it is the
    price of the bound. It must be visible, not claimed away."""
    violent = 5.0  # %/day at full deployment — 3-sigma is 15%, well past 6.7
    limits = [
        _deployed_engine(violent * g, g).daily_loss_limit_pct
        for g in (1.0, 0.5, 0.25, 0.1)
    ]
    # Fully deployed, the measurement stands on its own and is deep.
    assert limits[0] == pytest.approx(15.0, abs=0.01)
    # De-deployed, it is clipped to the fixed rung — tighter, never looser.
    assert limits[-1] == pytest.approx(
        _cfg().effective_max_daily_loss_pct, abs=0.01,
    )
    # Monotone non-increasing: de-deploying can only tighten, in this regime
    # as in every other. That is the invariant that actually holds
    # everywhere, and it is the one the owner's ruling needs.
    assert limits == sorted(limits, reverse=True)


def test_de_deploying_can_never_loosen_the_breaker_in_any_regime():
    """The property that DOES hold everywhere, swept rather than argued:
    for any book, at any volatility, reducing deployment never raises the
    loss the desk is allowed to take before it stops."""
    for full_vol in (0.5, 1.5, 2.2, 3.0, 5.0, 9.0):
        limits = [
            _deployed_engine(full_vol * g, g).daily_loss_limit_pct
            for g in (2.0, 1.0, 0.75, 0.5, 0.25, 0.1, 0.01, 0.001)
        ]
        # Floating-point tolerance: equality between rungs is the common
        # case here, and two arithmetically identical routes to it differ in
        # the last bit. The claim is about direction, not about bits.
        for tighter, looser in zip(limits, limits[1:]):
            assert looser <= tighter + 1e-9, (full_vol, limits)


def test_a_clipped_measurement_is_still_reported_as_a_measurement():
    """FINDING 2's "it hides its own trace", in its current form. The bound
    lands a clipped yardstick on EXACTLY the fixed rung's value, and the
    basis used to be decided by float equality against that value — so every
    partially-deployed volatile book would have told an operator "no
    measurement governed" on a day when a measurement, clipped, is precisely
    what set the limit."""
    engine = _deployed_engine(5.0 * 0.1, 0.1)
    assert engine.daily_loss_limit_pct == pytest.approx(
        _cfg().effective_max_daily_loss_pct, abs=0.01,
    )
    assert engine.daily_loss_limit_basis() == RiskRuleEngine.VOL_RELATIVE_BASIS

    # And a book with nothing measurable still says so.
    nothing = RiskRuleEngine(_cfg(), portfolio_vol_provider=lambda: None)
    assert nothing.daily_loss_limit_basis() == RiskRuleEngine.FIXED_BASIS
