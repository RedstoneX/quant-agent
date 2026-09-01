"""Margin interest tracker — spec `docs/QAMC_REMEDIATION_SPEC.md` §11.2.

Covers:
  1. The formula is right for a known balance and rate.
  2. A zero debit balance produces no charge and no alert noise.
  3. Intraday leverage with a flat close produces zero interest — the
     design lever (interest accrues ONLY on the overnight/EOD debit
     balance) is pinned by the function signature, not just documented.
  4. The figure is labelled an ESTIMATE wherever it is rendered.
  5. The INT-activity comparison path against a stubbed broker response,
     both when a charge is present and when it is absent.

This module MEASURES only — nothing here exercises sizing, execution, or
a risk-engine gate. No test in this file should ever need to construct a
TradeDecision or call RiskRuleEngine.check().
"""

from unittest.mock import MagicMock, patch

import pytest

from src.margin_interest import (
    ESTIMATE_LABEL,
    IntActivityComparison,
    MarginInterestEstimate,
    build_estimate,
    compare_estimate_to_broker_activity,
    estimate_daily_interest,
    format_alert_line,
    overnight_debit_balance,
)


# ---------------------------------------------------------------------------
# 1. Formula correctness
# ---------------------------------------------------------------------------

def test_formula_known_balance_and_rate():
    # $10,000 debit at 6.25% / 360 = $1.7361...
    daily = estimate_daily_interest(10_000.0, 6.25)
    assert daily == pytest.approx(10_000.0 * 0.0625 / 360, rel=1e-9)
    assert daily == pytest.approx(1.7361, abs=0.001)


def test_formula_reproduces_spec_order_of_magnitude_at_2x_on_9839_equity():
    """Spec §11.2: at a sustained 2.0x on ~$9,839 equity, ~$1.71/day,
    ~$614/yr. At 2.0x gross, debit balance = equity (gross - equity =
    2*equity - equity = equity)."""
    equity = 9_839.0
    debit_balance = equity  # 2.0x gross exposure
    estimate = build_estimate(debit_balance, 6.25)
    assert estimate is not None
    assert estimate.daily_usd == pytest.approx(1.71, abs=0.01)
    assert estimate.annual_usd == pytest.approx(614.0, abs=1.0)


def test_annual_is_daily_times_360_not_365():
    """Both figures use Alpaca's own 360-day convention — mixing a 360-day
    daily accrual with a 365-day year would silently overstate/understate
    the annual figure relative to the daily one it's derived from."""
    estimate = build_estimate(10_000.0, 6.25)
    assert estimate.annual_usd == pytest.approx(estimate.daily_usd * 360)


def test_elite_rate_produces_a_smaller_estimate():
    non_elite = estimate_daily_interest(10_000.0, 6.25)
    elite = estimate_daily_interest(10_000.0, 4.75)
    assert elite < non_elite


# ---------------------------------------------------------------------------
# 2. Zero debit balance -> no charge, no alert noise
# ---------------------------------------------------------------------------

def test_zero_cash_deficit_is_zero_debit_balance():
    assert overnight_debit_balance(0.0) == 0.0


def test_positive_cash_is_zero_debit_balance():
    # Plenty of cash on hand — nothing was borrowed.
    assert overnight_debit_balance(5_000.0) == 0.0


def test_none_cash_is_zero_debit_balance():
    assert overnight_debit_balance(None) == 0.0


def test_sub_noise_floor_deficit_is_zero_debit_balance():
    """A $0.30 deficit is settlement/rounding noise, not a real debit
    balance — same $1 floor (MARGIN_DEFICIT_FLOOR_USD) the existing
    force-delever / cash-only machinery already uses."""
    assert overnight_debit_balance(-0.30) == 0.0


def test_zero_debit_balance_produces_no_estimate():
    assert build_estimate(0.0, 6.25) is None


def test_zero_rate_produces_no_estimate():
    assert build_estimate(10_000.0, 0.0) is None


def test_zero_debit_balance_produces_no_alert_line():
    assert format_alert_line(build_estimate(0.0, 6.25)) is None
    assert format_alert_line(None) is None


def test_zero_debit_balance_produces_no_comparison():
    """No debit balance means nothing to settle — compare_* must return
    None rather than fabricating a comparison against nothing."""
    assert compare_estimate_to_broker_activity(None, []) is None
    assert compare_estimate_to_broker_activity(None, [{"net_amount": -5.0}]) is None


# ---------------------------------------------------------------------------
# 3. Intraday leverage + flat close -> zero interest (the design lever)
# ---------------------------------------------------------------------------

def test_intraday_debit_with_flat_close_is_zero_overnight_debit():
    """The desk ran leveraged intraday (cash dipped to -$8,000 at some
    point during the day) but trimmed back to flat before the close.
    Only the END-OF-DAY cash figure is a valid input to
    overnight_debit_balance() — there is no argument for "today's
    intraday low" on this function, so a caller literally cannot charge
    for the intraday draw even by mistake. This is the pin."""
    intraday_low_cash = -8_000.0  # never passed to overnight_debit_balance
    end_of_day_cash = 0.0         # flat close
    debit_balance = overnight_debit_balance(end_of_day_cash)
    assert debit_balance == 0.0
    assert build_estimate(debit_balance, 6.25) is None
    assert format_alert_line(build_estimate(debit_balance, 6.25)) is None
    # Sanity: the intraday figure was never touched — this test would be
    # meaningless if some code path fed it in.
    assert intraday_low_cash < 0


def test_intraday_debit_with_positive_close_is_zero_overnight_debit():
    """Same scenario, but the close is actually cash-positive (sold down
    past flat) — still zero, never negative."""
    assert overnight_debit_balance(2_500.0) == 0.0


def test_only_a_debit_balance_still_present_at_close_accrues_interest():
    """Contrast case: the debit balance IS still present at end-of-day
    (nothing was trimmed into the close) — THIS accrues interest. Confirms
    the zero result above is about timing, not about the function being
    broken."""
    debit_balance = overnight_debit_balance(-8_000.0)
    assert debit_balance == 8_000.0
    estimate = build_estimate(debit_balance, 6.25)
    assert estimate is not None
    assert estimate.daily_usd > 0


# ---------------------------------------------------------------------------
# 4. Labelled ESTIMATE wherever rendered
# ---------------------------------------------------------------------------

def test_estimate_object_carries_the_label():
    estimate = build_estimate(10_000.0, 6.25)
    assert estimate.label == ESTIMATE_LABEL
    assert "ESTIMATE" in estimate.label


def test_alert_line_carries_the_label():
    estimate = build_estimate(10_000.0, 6.25)
    line = format_alert_line(estimate)
    assert "ESTIMATE" in line


def test_estimate_label_never_claims_an_observed_charge():
    estimate = build_estimate(10_000.0, 6.25)
    assert "observed" not in estimate.label.lower() or "not an observed" in estimate.label.lower()


# ---------------------------------------------------------------------------
# 5. INT-activity comparison against a stubbed broker response
# ---------------------------------------------------------------------------

def test_int_activity_comparison_when_charge_present():
    estimate = build_estimate(9_839.0, 6.25)
    stubbed_activities = [
        {"date": "2026-09-01", "net_amount": -1.71, "description": "MARGIN INTEREST"},
    ]
    comparison = compare_estimate_to_broker_activity(estimate, stubbed_activities)
    assert comparison is not None
    assert comparison.charge_confirmed is True
    assert comparison.observed_usd == pytest.approx(1.71)
    assert "confirmed" in comparison.note.lower()


def test_int_activity_comparison_sums_multiple_rows():
    estimate = build_estimate(9_839.0, 6.25)
    stubbed_activities = [
        {"net_amount": -1.00},
        {"net_amount": -0.71},
    ]
    comparison = compare_estimate_to_broker_activity(estimate, stubbed_activities)
    assert comparison.observed_usd == pytest.approx(1.71)


def test_int_activity_comparison_when_charge_absent():
    """No INT activity at all — must NOT be silently treated as
    'confirmed zero'. It's reported as not-confirmed, undecided."""
    estimate = build_estimate(9_839.0, 6.25)
    comparison = compare_estimate_to_broker_activity(estimate, [])
    assert comparison is not None
    assert comparison.charge_confirmed is False
    assert comparison.observed_usd is None
    assert "no int activity" in comparison.note.lower()
    # Does not assert paper never charges interest — the whole point is
    # that this is unconfirmed, not disproven.
    assert "not confirmed" in comparison.note.lower() or "may not" in comparison.note.lower()


def test_int_activity_comparison_never_prejudges_before_data():
    """The comparison function itself carries no baked-in assumption
    about which way the open question resolves — same code path handles
    both outcomes, driven only by what the stub returns."""
    estimate = build_estimate(9_839.0, 6.25)
    absent = compare_estimate_to_broker_activity(estimate, [])
    present = compare_estimate_to_broker_activity(
        estimate, [{"net_amount": -1.71}],
    )
    assert absent.charge_confirmed is False
    assert present.charge_confirmed is True


def test_int_activity_zero_net_amount_is_not_confirmed():
    """An INT row present but net $0 (e.g. a reversal) must not read as a
    confirmed charge."""
    estimate = build_estimate(9_839.0, 6.25)
    comparison = compare_estimate_to_broker_activity(estimate, [{"net_amount": 0.0}])
    assert comparison.charge_confirmed is False


# ---------------------------------------------------------------------------
# Broker method: get_margin_interest_activities against a stubbed SDK client
# ---------------------------------------------------------------------------

@patch("src.execution.broker.TradingClient")
def test_broker_get_margin_interest_activities_parses_response(MockTradingClient):
    from src.execution.broker import AlpacaBroker

    mock_client = MagicMock()
    mock_client.get.return_value = [
        {"activity_type": "INT", "date": "2026-09-01", "net_amount": "-1.71",
         "description": "MARGIN INTEREST"},
    ]
    MockTradingClient.return_value = mock_client

    broker = AlpacaBroker(api_key="k", secret_key="s", paper=True)
    activities = broker.get_margin_interest_activities()

    assert len(activities) == 1
    assert activities[0]["net_amount"] == pytest.approx(-1.71)
    assert activities[0]["activity_type"] == "INT"
    mock_client.get.assert_called_once()
    args, _ = mock_client.get.call_args
    assert args[0] == "/account/activities/INT"


@patch("src.execution.broker.TradingClient")
def test_broker_get_margin_interest_activities_empty_response(MockTradingClient):
    from src.execution.broker import AlpacaBroker

    mock_client = MagicMock()
    mock_client.get.return_value = []
    MockTradingClient.return_value = mock_client

    broker = AlpacaBroker(api_key="k", secret_key="s", paper=True)
    assert broker.get_margin_interest_activities() == []


@patch("src.execution.broker.TradingClient")
def test_broker_get_margin_interest_activities_never_raises_on_broker_error(MockTradingClient):
    from src.execution.broker import AlpacaBroker

    mock_client = MagicMock()
    mock_client.get.side_effect = RuntimeError("broker down")
    MockTradingClient.return_value = mock_client

    broker = AlpacaBroker(api_key="k", secret_key="s", paper=True)
    assert broker.get_margin_interest_activities() == []


# ---------------------------------------------------------------------------
# End-to-end: stubbed broker response feeding the comparison, both directions
# ---------------------------------------------------------------------------

@patch("src.execution.broker.TradingClient")
def test_end_to_end_broker_confirms_charge(MockTradingClient):
    from src.execution.broker import AlpacaBroker

    mock_client = MagicMock()
    mock_client.get.return_value = [
        {"activity_type": "INT", "date": "2026-09-01", "net_amount": "-1.71"},
    ]
    MockTradingClient.return_value = mock_client
    broker = AlpacaBroker(api_key="k", secret_key="s", paper=True)

    estimate = build_estimate(9_839.0, 6.25)
    comparison = compare_estimate_to_broker_activity(
        estimate, broker.get_margin_interest_activities(),
    )
    assert comparison.charge_confirmed is True


@patch("src.execution.broker.TradingClient")
def test_end_to_end_broker_shows_no_charge(MockTradingClient):
    from src.execution.broker import AlpacaBroker

    mock_client = MagicMock()
    mock_client.get.return_value = []
    MockTradingClient.return_value = mock_client
    broker = AlpacaBroker(api_key="k", secret_key="s", paper=True)

    estimate = build_estimate(9_839.0, 6.25)
    comparison = compare_estimate_to_broker_activity(
        estimate, broker.get_margin_interest_activities(),
    )
    assert comparison.charge_confirmed is False
