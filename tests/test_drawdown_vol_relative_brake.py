"""docs/WORK.md item 32 — the drawdown alarms' BASIS, owner call 2026-09-11.

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

The basis is now the account's OWN realized daily volatility over a rolling
trailing window, scaled to each window by sqrt(time). What that does and
does not settle:

  RESEARCH-GROUNDED and unchanged: the sqrt(time) relationship between
  windows (Van Hemert, Ganz, Harvey et al., "Drawdowns", Journal of
  Portfolio Management, 2020). ONE sensitivity multiple scaled by sqrt(T)
  reproduces the previously-shipped 1 : sqrt(5) daily-to-5-day ratio
  exactly, which is why no per-window multiple survives.

  PROVISIONAL and labelled so: the sensitivity itself
  (`RiskConfig.drawdown_vol_sensitivity`). Researched 2026-09-11 — there is
  no citable industry-standard number for how many multiples of recent
  volatility should trip a drawdown alarm. 6.7 is set by DAY-ONE CONTINUITY
  against a measured reference volatility, not by a severity opinion, and is
  not validated against real post-reset trading behaviour because there is
  none (verified 2026-09-11: the live desk's `daily_pnl` holds one row).

  NOT TOUCHED: position sizing. Volatility here is only the yardstick the
  ALARM measures a loss against. Volatility-target exposure scaling was
  separately rejected for this desk (docs/OUTCOME.md).
"""
import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import RiskConfig
from src.pipeline import TradingPipeline
from src.risk.rules import (
    GROSS_LADDER_ALERT_PCT,
    MIN_REALIZED_VOL_RETURNS,
    REALIZED_VOL_SKIP_NEWEST_SESSIONS,
    REALIZED_VOL_WINDOW_SESSIONS,
    RiskRuleEngine,
    realized_daily_vol_pct,
    vol_relative_drawdown_threshold_pct,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _curve(sigma_pct: float, *, sessions: int = 21, start: float = 100_000.0):
    """An equity curve, NEWEST-FIRST, whose realized daily volatility is
    (very close to) `sigma_pct`.

    Returns alternate +sigma / -sigma, so the sample standard deviation of
    the daily returns is sigma by construction rather than by chance — the
    tests below assert on thresholds, and a random walk would make them
    flaky for reasons that have nothing to do with the code under test.
    """
    values = [start]
    for i in range(sessions - 1):
        values.append(values[-1] * (1 + (sigma_pct / 100) * (1 if i % 2 else -1)))
    return list(reversed(values))


def _cfg(**overrides) -> RiskConfig:
    base = dict(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
    )
    base.update(overrides)
    return RiskConfig(**base)


def _engine(curve, **cfg_overrides) -> RiskRuleEngine:
    return RiskRuleEngine(
        _cfg(**cfg_overrides), equity_history_provider=lambda: curve,
    )


# ---------------------------------------------------------------------------
# The volatility measurement itself
# ---------------------------------------------------------------------------

def test_realized_vol_measures_the_accounts_own_daily_movement():
    """Sanity floor: the yardstick actually reports what the equity curve
    is doing, in percent per session."""
    assert realized_daily_vol_pct(_curve(1.0)) == pytest.approx(1.0, rel=0.05)
    assert realized_daily_vol_pct(_curve(2.5)) == pytest.approx(2.5, rel=0.05)


def test_realized_vol_reads_newest_first_like_the_database_returns():
    """`Database.get_daily_pnl` orders `date DESC`. If this function read the
    series the other way round the RETURNS would be sign-flipped, which a
    standard deviation hides completely — so pin the ordering contract with
    a curve whose recent half is calm and whose old half is violent."""
    calm_recent = list(reversed(
        # oldest-first: 10 violent sessions, then 10 calm ones
        [100_000 * (1.05 ** i if i % 2 else 0.95 ** i) for i in range(11)]
        + [150_000 + (i % 2) * 100 for i in range(10)]
    ))
    quiet = realized_daily_vol_pct(calm_recent, min_returns=5, window_sessions=10)
    violent = realized_daily_vol_pct(
        list(reversed(calm_recent)), min_returns=5, window_sessions=10,
    )
    assert quiet is not None and violent is not None
    assert quiet < violent, (
        "the trailing window must be measured from the NEWEST end of the "
        "series; reading it oldest-first would price today's risk off "
        "history the account has already left behind"
    )


def test_realized_vol_needs_enough_history_to_be_worth_acting_on():
    """A standard deviation from a handful of points is mostly its own
    estimation error. Below `MIN_REALIZED_VOL_RETURNS` returns the answer is
    None — "no usable estimate" — not a noisy number that would set a risk
    threshold."""
    assert MIN_REALIZED_VOL_RETURNS >= 10
    # `MIN + 1` readings would be exactly enough returns, except that the
    # newest is dropped (it is the session being judged), so one more is
    # needed. One short must be None, not a noisy number.
    just_short = _curve(
        1.0, sessions=MIN_REALIZED_VOL_RETURNS + REALIZED_VOL_SKIP_NEWEST_SESSIONS,
    )
    assert realized_daily_vol_pct(just_short) is None
    just_enough = _curve(
        1.0,
        sessions=MIN_REALIZED_VOL_RETURNS + REALIZED_VOL_SKIP_NEWEST_SESSIONS + 1,
    )
    assert realized_daily_vol_pct(just_enough) is not None


def test_realized_vol_is_none_right_after_the_account_reset():
    """The 2026-09-02 clean-slate reset left the equity curve with a single
    point, which is the desk's real state. That must degrade to the
    fixed-percentage fallback, not crash and not produce a number."""
    assert realized_daily_vol_pct([9862.74]) is None
    assert realized_daily_vol_pct([]) is None
    assert realized_daily_vol_pct(None) is None


def test_realized_vol_truncates_at_a_gap_rather_than_splicing_over_it():
    """A missing/zero/non-finite `total_value` truncates the window. Skipping
    it would splice a multi-session move into one "daily" return, inflating
    the volatility estimate and LOOSENING every alarm on exactly the days
    the data is unreliable."""
    curve = _curve(1.0, sessions=21)
    holed = list(curve)
    holed[4] = None  # 4 sessions back, i.e. inside the window
    assert realized_daily_vol_pct(holed) is None  # only 3 returns survive
    for bad in (0, -1.0, float("nan"), float("inf"), True):
        probe = list(curve)
        probe[4] = bad
        assert realized_daily_vol_pct(probe) is None


def test_a_flat_equity_curve_is_not_zero_volatility():
    """An account that has not traded has zero MEASURED volatility. Taken
    literally that collapses every threshold to zero and trips the alarm on
    the first cent lost, so it is treated as "no usable estimate"."""
    assert realized_daily_vol_pct([100_000.0] * 25) is None


def test_the_window_is_twenty_sessions_and_older_history_is_ignored():
    """The trailing window is what makes this non-stationary. A violent
    quarter the account has already left behind must not still be setting
    today's thresholds."""
    assert REALIZED_VOL_WINDOW_SESSIONS == 20
    recent_calm = _curve(0.5, sessions=REALIZED_VOL_WINDOW_SESSIONS + 5)
    ancient_chaos = _curve(6.0, sessions=200, start=recent_calm[-1])
    combined = recent_calm + ancient_chaos  # newest-first: calm, then chaos
    assert realized_daily_vol_pct(combined) == pytest.approx(0.5, rel=0.05)


# ---------------------------------------------------------------------------
# The threshold — the actual behaviour change
# ---------------------------------------------------------------------------

def test_the_alarm_trips_at_a_smaller_loss_when_the_book_is_quiet():
    """The whole point. In a quiet regime an ordinary bad day is small, so
    the alarm must trip at a smaller absolute loss than the frozen -6.7%
    would have — a 4% day is not ordinary for a book that moves 0.5% a
    session, and the old fixed threshold called it fine."""
    quiet = _engine(_curve(0.5))
    assert quiet.daily_loss_limit_pct < 6.7
    assert quiet.check_daily_loss(baseline=100_000, daily_pnl=-4_000) is not None
    # ... and the old fixed basis said nothing about that same day.
    assert RiskRuleEngine(_cfg()).check_daily_loss(
        baseline=100_000, daily_pnl=-4_000,
    ) is None


def test_the_alarm_tolerates_a_larger_loss_when_the_book_is_volatile():
    """The other half. In a violent regime the frozen -6.7% fires on days
    that are, for that book, unremarkable — de-levering a desk that is
    behaving exactly as its own recent record says it does."""
    violent = _engine(_curve(2.0))
    assert violent.daily_loss_limit_pct > 6.7
    assert violent.check_daily_loss(baseline=100_000, daily_pnl=-8_000) is None
    # The old fixed basis tripped on that same day.
    assert RiskRuleEngine(_cfg()).check_daily_loss(
        baseline=100_000, daily_pnl=-8_000,
    ) is not None
    # It is not a blank cheque: a genuinely abnormal day still trips.
    assert violent.check_daily_loss(
        baseline=100_000, daily_pnl=-20_000,
    ) is not None


def test_the_threshold_scales_proportionally_with_measured_volatility():
    """Doubling the book's own volatility doubles the alarm distance. This is
    the property a fixed percentage cannot have at all, and it is what makes
    the alarm's firing RATE stable across regimes instead of its absolute
    level."""
    one = _engine(_curve(1.0)).daily_loss_limit_pct
    two = _engine(_curve(2.0)).daily_loss_limit_pct
    assert two == pytest.approx(one * 2, rel=1e-3)


def test_sensitivity_is_the_only_knob_and_it_is_honoured():
    cfg_curve = _curve(1.0)
    assert _engine(cfg_curve, drawdown_vol_sensitivity=3.0).daily_loss_limit_pct \
        == pytest.approx(
            _engine(cfg_curve, drawdown_vol_sensitivity=6.0).daily_loss_limit_pct / 2,
            rel=1e-3,
        )


def test_day_one_continuity_at_the_measured_reference_volatility():
    """How the provisional sensitivity was SET, pinned so a silent change to
    it fails here.

    6.7 is not a severity judgement — it is chosen so that at the reference
    daily volatility measured 2026-09-11 (1.0% per session, from real market
    data over this desk's own configured universe: equal-weight baskets the
    size this desk runs came in at a 0.80%-1.04% median) the new thresholds
    equal the ones already shipped. The only thing that changes on day one is
    that they now MOVE.
    """
    cfg = _cfg()
    assert cfg.drawdown_vol_sensitivity == pytest.approx(6.7)
    reference_sigma = 1.0
    assert cfg.drawdown_vol_sensitivity * reference_sigma == pytest.approx(
        6.7, abs=0.01,
    )  # the shipped fixed daily breaker
    assert cfg.drawdown_vol_sensitivity * reference_sigma * math.sqrt(5) \
        == pytest.approx(15.0, abs=0.05)  # the shipped fixed 5-day brake


# ---------------------------------------------------------------------------
# The relative severity structure — the part that IS research-grounded
# ---------------------------------------------------------------------------

def test_window_scaling_is_square_root_of_time():
    """Van Hemert/Ganz/Harvey, JPM 2020. Unchanged from the fixed-percentage
    design, and now expressed once rather than as three per-window multiples
    that could drift apart (which is exactly how bug 1 happened)."""
    sigma = 1.0
    def threshold(window):
        return vol_relative_drawdown_threshold_pct(
            daily_vol_pct=sigma, window_sessions=window,
            sensitivity=6.7, fallback_pct=-99.0,
        )
    assert threshold(5) == pytest.approx(threshold(1) * math.sqrt(5), rel=1e-3)
    assert threshold(20) == pytest.approx(threshold(1) * math.sqrt(20), rel=1e-3)
    # The 1 : sqrt(5) daily-to-5-day ratio the previous fix shipped, now an
    # identity rather than two independently-rounded constants.
    assert threshold(5) / threshold(1) == pytest.approx(math.sqrt(5), rel=1e-3)


def test_severity_order_holds_at_every_volatility():
    """A shorter window may never tolerate a bigger loss than a longer one,
    and no window may sleep past the ladder's owner-alert point.

    The 5% case is the one that found a real bug during this work: with only
    the 20-day window capped, a violent regime put the DAILY limit at -34%,
    i.e. deeper than the 20-day brake. All three windows are capped at the
    ladder alert now.
    """
    for sigma in (0.25, 0.5, 1.0, 2.0, 5.0):
        curve = _curve(sigma, sessions=26)
        rows = [{"total_value": v} for v in curve]
        result = _pipeline(rows)._compute_recent_performance(
            current_equity=rows[0]["total_value"],
        )
        daily = -_engine(curve).daily_loss_limit_pct
        assert 0 > daily >= result["drawdown_5d_threshold_pct"] \
            >= result["drawdown_20d_threshold_pct"] >= GROSS_LADDER_ALERT_PCT, (
            f"severity order broke at sigma={sigma}%"
        )


def test_the_20_day_brake_stays_reconciled_with_the_delevering_ladder():
    """Bug 2's constraint SURVIVES the basis change. The §11.2 ladder halves
    the book and alerts the owner at -20%; this brake must never again be
    asleep past that point, however calm the book has been."""
    for sigma in (0.5, 1.0, 2.0, 8.0):
        capped = vol_relative_drawdown_threshold_pct(
            daily_vol_pct=sigma, window_sessions=20, sensitivity=6.7,
            fallback_pct=-20.0, cap_pct=GROSS_LADDER_ALERT_PCT,
        )
        assert capped >= GROSS_LADDER_ALERT_PCT


# ---------------------------------------------------------------------------
# Fallback — the desk's actual state today
# ---------------------------------------------------------------------------

def test_no_history_falls_back_to_the_shipped_fixed_percentage():
    """Right after the reset there is nothing to measure. Behaviour must be
    exactly what main shipped, so this change cannot break a cold start."""
    cold = RiskRuleEngine(_cfg(), equity_history_provider=lambda: [9862.74])
    assert cold.daily_loss_limit_pct == pytest.approx(6.7)
    assert cold.daily_loss_limit_pct == pytest.approx(
        _cfg().effective_max_daily_loss_pct,
    )


def test_no_provider_at_all_behaves_exactly_as_before():
    """Every existing fixture in this repo builds the engine with a config
    only. Those must be untouched by this change."""
    assert RiskRuleEngine(_cfg()).daily_loss_limit_pct == pytest.approx(6.7)


def test_an_explicit_override_still_wins_over_everything():
    """Unchanged precedence: an operator who sets `max_daily_loss_pct` is
    deliberately opting out of volatility-relative alarms."""
    engine = _engine(_curve(2.0), max_daily_loss_pct=3.0)
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
    assert risk.get("drawdown_vol_sensitivity") is not None


def test_a_broken_volatility_read_never_disables_the_breaker():
    """Fail-safe direction. A provider that raises must degrade to the fixed
    percentage, not to no limit at all."""
    def boom():
        raise RuntimeError("database gone")
    engine = RiskRuleEngine(_cfg(), equity_history_provider=boom)
    assert engine.daily_loss_limit_pct == pytest.approx(6.7)
    assert engine.check_daily_loss(
        baseline=100_000, daily_pnl=-8_000,
    ) is not None


# ---------------------------------------------------------------------------
# The rolling-return brakes, end to end through the pipeline
# ---------------------------------------------------------------------------

def _pipeline(rows: list[dict], risk: RiskConfig | None = None) -> TradingPipeline:
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.config = SimpleNamespace(risk=risk or _cfg())
    pipeline.db = MagicMock()
    pipeline.db.get_daily_pnl.return_value = rows
    return pipeline


def test_rolling_brakes_tighten_in_a_quiet_regime():
    rows = [{"total_value": v} for v in _curve(0.5)]
    result = _pipeline(rows)._compute_recent_performance(
        current_equity=rows[0]["total_value"],
    )
    assert result["realized_daily_vol_pct"] == pytest.approx(0.5, rel=0.05)
    # Both brakes strictly tighter than the fixed -15% / -20% they replace.
    assert -15.0 < result["drawdown_5d_threshold_pct"] < 0
    assert -20.0 < result["drawdown_20d_threshold_pct"] < 0


def test_rolling_brakes_loosen_in_a_violent_regime_but_never_past_the_ladder():
    rows = [{"total_value": v} for v in _curve(2.0)]
    result = _pipeline(rows)._compute_recent_performance(
        current_equity=rows[0]["total_value"],
    )
    assert result["realized_daily_vol_pct"] == pytest.approx(2.0, rel=0.05)
    # sqrt(time) alone would put the 20-day brake near -60%; the ladder
    # constraint binds instead.
    assert result["drawdown_20d_threshold_pct"] == pytest.approx(
        GROSS_LADDER_ALERT_PCT,
    )
    # And the 5-day brake is clamped to it rather than going deeper.
    assert result["drawdown_5d_threshold_pct"] == pytest.approx(
        result["drawdown_20d_threshold_pct"],
    )


def test_rolling_brakes_fall_back_to_the_fixed_percentages_after_the_reset():
    """The desk's real state: one row of history. The brakes must read
    exactly the risk-unit-derived percentages main ships."""
    rows = [{"total_value": 9862.74}]
    cfg = _cfg()
    result = _pipeline(rows, cfg)._compute_recent_performance(
        current_equity=9862.74,
    )
    assert result["realized_daily_vol_pct"] is None
    assert result["drawdown_5d_threshold_pct"] == pytest.approx(
        cfg.drawdown_5d_threshold_pct,
    )
    assert result["drawdown_20d_threshold_pct"] == pytest.approx(
        cfg.drawdown_20d_threshold_pct,
    )


def test_the_same_bad_run_can_trip_or_not_depending_on_the_regime():
    """The behaviour a fixed percentage cannot produce, end to end: one
    rolling-window return, two verdicts, because the two books have shown
    different normal movement."""
    bad_run_pct = -6.0

    def trips(sigma: float) -> bool:
        curve = _curve(sigma, sessions=26)
        rows = [{"total_value": v} for v in curve]
        current = curve[5] * (1 + bad_run_pct / 100)
        rows[0] = {"total_value": current}
        result = _pipeline(rows)._compute_recent_performance(
            current_equity=current,
        )
        assert result["rolling_5d_pct"] == pytest.approx(bad_run_pct, abs=0.02)
        return result["in_drawdown"]

    assert trips(0.25) is True, "a -6% week is a real alarm for a calm book"
    assert trips(2.0) is False, "the same -6% week is ordinary for a wild one"


def test_a_violent_session_does_not_widen_its_own_alarm():
    """The self-loosening trap, found and fixed during this work. If the
    volatility window included the session being judged, a -6% day on a book
    that normally moves 0.25% would raise the measured volatility enough to
    re-classify ITSELF as ordinary — the alarm would be unable to fire on
    precisely the days it exists for."""
    calm = _curve(0.25, sessions=26)
    shocked = list(calm)
    shocked[0] = calm[1] * 0.94  # a -6% session lands on a calm book
    assert realized_daily_vol_pct(shocked) == pytest.approx(0.25, rel=0.05)
    # Including the judged session would have inflated it several-fold.
    assert realized_daily_vol_pct(shocked, skip_newest=0) > 1.0


def test_volatility_is_never_used_to_size_a_position():
    """Guard on the scope boundary. Volatility-target exposure scaling was
    separately REJECTED for this desk (docs/OUTCOME.md) — this machinery is
    the alarm's yardstick and nothing else. `_compute_recent_performance`
    must not have grown a sizing output."""
    rows = [{"total_value": v} for v in _curve(2.0)]
    result = _pipeline(rows)._compute_recent_performance(
        current_equity=rows[0]["total_value"],
    )
    forbidden = ("size", "scale", "target_vol", "allocation", "exposure_x")
    for key in result:
        assert not any(word in key for word in forbidden), (
            f"`{key}` looks like volatility-based SIZING leaking out of the "
            f"drawdown alarm — that technique is rejected for this desk"
        )
