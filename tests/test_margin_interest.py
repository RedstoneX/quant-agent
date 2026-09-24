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


# ---------------------------------------------------------------------------
# 6. src/api/broker_reads.py::read_margin_interest — the /account wiring.
#
# Added on verification: the module above had 28 passing tests and zero of
# them touched either place the estimate actually reaches a human (this
# API field, and the Telegram lines in section 7 below). That gap is what
# let a real bug ship: both wrappers originally fast-exited to "nothing to
# report" whenever `allow_margin` was `False`, without ever looking at
# `cash`. But `cash_only` (src/risk/rules.py) does not protect a COVER —
# D10 exempts it deliberately — and `src/agents/portfolio_manager.py`'s
# own DE-LEVER MANDATE already treats "cash negative, allow_margin False"
# as a real state a session can reach. So the original gate could report
# nothing for exactly the case this tracker exists to catch. Fixed to key
# off `cash` alone; the tests below pin that a negative cash balance is
# reported regardless of `allow_margin`.
# ---------------------------------------------------------------------------

import src.api.broker_reads as broker_reads  # noqa: E402


def test_read_margin_interest_no_debit_balance_is_an_explicit_zero(monkeypatch):
    """Was `..._is_all_none`, asserting the API returned nothing at all —
    which is exactly why the cockpit displayed nothing for 17 days.

    Owner decision 2026-09-18: an explicit zero, with the real configured
    rate, and `error` None so a caller can tell this apart from a failed
    read. `label` stays None: a certain zero is not an estimate."""
    from types import SimpleNamespace
    monkeypatch.setattr(
        broker_reads, "get_risk_limits",
        lambda: SimpleNamespace(margin_interest_rate_pct=6.25),
    )
    out = broker_reads.read_margin_interest(1_000.0)
    # `cumulative` is best-effort (a broker/DB read of its own) and degrades
    # to `None` in this unit test, which stubs no broker/config at all for
    # it — covered separately by the cumulative-specific tests below.
    out = {**out, "cumulative": None}
    assert out == {
        "debit_balance": 0.0, "rate_pct": 6.25, "daily_usd": 0.0,
        "annual_usd": 0.0, "label": None, "broker_check_note": None,
        "days_charged": 1, "period_usd": 0.0, "error": None,
        "cumulative": None,
    }


def test_read_margin_interest_missing_rate_is_a_fault_not_a_zero(monkeypatch):
    """A deleted or mis-keyed `risk.margin_interest_rate_pct` must reach
    the cockpit as a FAULT. `build_estimate` returns None for a zero rate
    exactly as it does for a zero debit, so a naive "None means zero"
    reading would print a reassuring $0.00 on the one day the owner most
    needs to know the tracker is broken — the inverse of what his
    "so I know it's still working" decision asked for."""
    from types import SimpleNamespace
    monkeypatch.setattr(
        broker_reads, "get_risk_limits",
        lambda: SimpleNamespace(margin_interest_rate_pct=0.0),
    )
    out = broker_reads.read_margin_interest(-5_000.0)
    assert out["error"]
    assert out["daily_usd"] is None
    assert out["debit_balance"] is None


def test_read_margin_interest_reports_a_debit_balance_even_with_margin_disabled(monkeypatch):
    """Regression: a COVER can push cash negative with `allow_margin`
    False (D10 exempts COVER from cash_only); the estimate must still
    surface rather than silently reporting nothing."""
    from types import SimpleNamespace
    monkeypatch.setattr(
        broker_reads, "get_risk_limits",
        lambda: SimpleNamespace(margin_interest_rate_pct=6.25),  # allow_margin intentionally absent
    )
    monkeypatch.setattr(
        broker_reads, "_get_broker",
        lambda: SimpleNamespace(get_margin_interest_activities=lambda: []),
    )
    out = broker_reads.read_margin_interest(-9_839.0)
    assert out["debit_balance"] == pytest.approx(9_839.0)
    assert out["daily_usd"] == pytest.approx(1.71, abs=0.01)
    assert out["error"] is None
    assert "ESTIMATE" in out["label"]


def test_read_margin_interest_config_read_failure_reports_error(monkeypatch):
    def boom():
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(broker_reads, "get_risk_limits", boom)
    out = broker_reads.read_margin_interest(-5_000.0)
    assert out["error"] == "config unreadable"
    assert out["debit_balance"] is None


def test_read_margin_interest_includes_broker_check_note(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(
        broker_reads, "get_risk_limits",
        lambda: SimpleNamespace(margin_interest_rate_pct=6.25),
    )
    monkeypatch.setattr(
        broker_reads, "_get_broker",
        lambda: SimpleNamespace(
            get_margin_interest_activities=lambda: [{"net_amount": -1.71}],
        ),
    )
    out = broker_reads.read_margin_interest(-9_839.0)
    assert out["broker_check_note"] is not None
    assert "confirmed" in out["broker_check_note"].lower()


def test_read_margin_interest_int_activity_failure_does_not_hide_the_estimate(monkeypatch):
    """The INT-activity check is a nicety layered on the estimate — its
    failure must not take the estimate itself down."""
    from types import SimpleNamespace

    def boom():
        raise RuntimeError("broker down")
    monkeypatch.setattr(
        broker_reads, "get_risk_limits",
        lambda: SimpleNamespace(margin_interest_rate_pct=6.25),
    )
    monkeypatch.setattr(
        broker_reads, "_get_broker",
        lambda: SimpleNamespace(get_margin_interest_activities=boom),
    )
    out = broker_reads.read_margin_interest(-9_839.0)
    assert out["error"] is None
    assert out["daily_usd"] == pytest.approx(1.71, abs=0.01)
    assert out["broker_check_note"] is None


def test_read_margin_interest_friday_reflects_the_three_day_weekend_carry(monkeypatch):
    """THE DEFECT THIS PINS: the /account panel (`GET /account` ->
    `routes_live._compute_margin_interest` -> this function) used to call
    `build_estimate(debit_balance, rate_pct)` with NO `days_charged`,
    silently defaulting to 1 every day — a flat per-day figure even on a
    Friday, while the Telegram alert (`src.notifier._margin_interest_lines`)
    already named the same debit's real 3-day (Fri+Sat+Sun) weekend carry
    via `days_charged_until_next_trading_day`. Same calendar helper, same
    stubbed `is_trading_day`, proving the dashboard no longer disagrees with
    the alert."""
    from types import SimpleNamespace
    monkeypatch.setattr(
        broker_reads, "get_risk_limits",
        lambda: SimpleNamespace(margin_interest_rate_pct=6.25),
    )
    monkeypatch.setattr(
        broker_reads, "_get_broker",
        lambda: SimpleNamespace(
            is_trading_day=_weekday_calendar(),
            get_margin_interest_activities=lambda: [],
        ),
    )
    monkeypatch.setattr("src.util.time.et_today", lambda: _date(2026, 9, 25))  # Friday

    out = broker_reads.read_margin_interest(-5_000.0)

    assert out["error"] is None
    assert out["daily_usd"] == pytest.approx(0.87, abs=0.01)
    assert out["days_charged"] == 3
    assert out["period_usd"] == pytest.approx(2.60, abs=0.01)


# ---------------------------------------------------------------------------
# 7. src/notifier.py::_margin_interest_lines — the morning Telegram wiring.
# ---------------------------------------------------------------------------

def test_margin_interest_lines_speak_the_zero_without_a_debit_balance(monkeypatch, tmp_path):
    """A zero-debit morning still produces exactly one cumulative line
    (owner decision 2026-09-18's "every day, even if it's zero" carries
    forward into the 2026-09-24 cumulative rewrite) — nothing borrowed
    persists as a $0.00 row, and the buckets built from it carry no "est."
    tag on a certain zero."""
    import src.notifier as n
    monkeypatch.setattr(n, "_REHEARSAL_MODE", False)
    monkeypatch.setattr(n, "_DB_PATH", tmp_path / "quant_agent.db")
    monkeypatch.setattr(
        "src.config.load_config",
        lambda *a, **kw: MagicMock(risk=MagicMock(margin_interest_rate_pct=6.25)),
    )
    monkeypatch.setattr(
        "src.api.deps.get_alpaca_credentials", lambda: ("k", "s"),
    )
    monkeypatch.setattr("src.api.deps.get_alpaca_paper", lambda: True)
    monkeypatch.setattr(
        "src.execution.broker.AlpacaBroker",
        lambda **kw: MagicMock(
            get_account=lambda: {"cash": 1_000.0},
            get_margin_interest_activities=lambda: [],
        ),
    )
    lines = n._margin_interest_lines()
    assert len(lines) == 1
    assert "this week $0.00" in lines[0]
    assert "all-time $0.00" in lines[0]
    assert "(est.)" not in lines[0]


def test_margin_interest_lines_present_with_margin_disabled_and_negative_cash(monkeypatch, tmp_path):
    """Regression, same bug as the broker_reads test above: a debit
    balance carried with `allow_margin` False must still produce a line,
    not silence — the Telegram alert is where the desk actually sees it."""
    import src.notifier as n
    monkeypatch.setattr(n, "_REHEARSAL_MODE", False)
    monkeypatch.setattr(n, "_DB_PATH", tmp_path / "quant_agent.db")
    monkeypatch.setattr(
        "src.config.load_config",
        lambda *a, **kw: MagicMock(
            risk=MagicMock(allow_margin=False, margin_interest_rate_pct=6.25),
        ),
    )
    monkeypatch.setattr(
        "src.api.deps.get_alpaca_credentials", lambda: ("k", "s"),
    )
    monkeypatch.setattr("src.api.deps.get_alpaca_paper", lambda: True)
    monkeypatch.setattr(
        "src.execution.broker.AlpacaBroker",
        lambda **kw: MagicMock(
            get_account=lambda: {"cash": -9_839.0},
            get_margin_interest_activities=lambda: [],
        ),
    )
    lines = n._margin_interest_lines()
    assert len(lines) >= 1
    assert "margin interest" in lines[0].lower()
    assert "(est.)" in lines[0]
    # 9,839 * 6.25 / 100 / 360 = 1.7089...
    assert "1.71" in lines[0]


def test_margin_interest_lines_suppressed_in_rehearsal(monkeypatch):
    import src.notifier as n
    monkeypatch.setattr(n, "_REHEARSAL_MODE", True)
    assert n._margin_interest_lines() == []


def test_margin_interest_lines_never_raises_when_broker_read_fails(monkeypatch, tmp_path):
    import src.notifier as n
    monkeypatch.setattr(n, "_REHEARSAL_MODE", False)
    monkeypatch.setattr(n, "_DB_PATH", tmp_path / "quant_agent.db")
    monkeypatch.setattr(
        "src.config.load_config",
        lambda *a, **kw: MagicMock(risk=MagicMock(margin_interest_rate_pct=6.25)),
    )

    def boom():
        raise RuntimeError("credentials gateway down")
    monkeypatch.setattr("src.api.deps.get_alpaca_credentials", boom)
    # Still never raises — but it now SAYS the read failed instead of
    # degrading to silence (2026-09-18): silence is indistinguishable from
    # a dead tracker, which is the whole defect being fixed. Crucially NOT
    # the zero line: "not available" and "$0.00" are different claims.
    from src.margin_interest import UNAVAILABLE_LINE
    assert n._margin_interest_lines() == [UNAVAILABLE_LINE]
    assert "$0.00" not in UNAVAILABLE_LINE


def test_margin_interest_lines_persists_a_row_for_the_cumulative_view(monkeypatch, tmp_path):
    """The whole point of persistence: a second morning's debit balance
    must be summed with the first, not replace it — this is what makes
    "this week"/"this month" a running total rather than always showing
    only today."""
    import sqlite3

    import src.notifier as n
    db_path = tmp_path / "quant_agent.db"
    monkeypatch.setattr(n, "_REHEARSAL_MODE", False)
    monkeypatch.setattr(n, "_DB_PATH", db_path)
    monkeypatch.setattr(
        "src.config.load_config",
        lambda *a, **kw: MagicMock(risk=MagicMock(margin_interest_rate_pct=6.25)),
    )
    monkeypatch.setattr("src.api.deps.get_alpaca_credentials", lambda: ("k", "s"))
    monkeypatch.setattr("src.api.deps.get_alpaca_paper", lambda: True)
    monkeypatch.setattr(
        "src.execution.broker.AlpacaBroker",
        lambda **kw: MagicMock(
            get_account=lambda: {"cash": -5_000.0},
            get_margin_interest_activities=lambda: [],
        ),
    )
    n._margin_interest_lines()
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT date, period_usd, source FROM margin_interest_daily").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][1] == pytest.approx(0.8680555, abs=1e-4)
    assert rows[0][2] == "estimate"


# ---------------------------------------------------------------------------
# 9. "Every day, even if it's zero" — owner decision 2026-09-18
# ---------------------------------------------------------------------------
# Verbatim: "Yes, every day, even if it's zero, that way I know it's still
# working." These pin the FOUR states `format_daily_line` must keep apart.
# `build_estimate` collapses two of them (no debit, no rate) into a single
# `None`, which is why the always-speak policy lives in its own function and
# not in `format_alert_line`.

def test_format_daily_line_says_the_zero_with_the_measured_cash_figure():
    """The zero is the evidence the tracker still runs, so it must SPEAK —
    and it carries the real overnight cash balance rather than a bare
    "$0.00", because a constant string is indistinguishable from a stuck
    one while a balance that moves day to day is not."""
    from src.margin_interest import format_daily_line
    line = format_daily_line(4_812.33, 6.25)
    assert line == (
        "💳 margin interest: $0.00/day — overnight cash $4,812.33, "
        "nothing borrowed"
    )
    # A certain zero is not an estimate — nothing borrowed costs nothing at
    # any rate at all. The label stays on the figure that IS a projection.
    assert "ESTIMATE" not in line


def test_format_daily_line_does_not_claim_nothing_was_borrowed_below_the_floor():
    """MARGIN_DEFICIT_FLOOR_USD means a 99-cent overnight deficit is
    ignored as settlement noise. Money WAS borrowed there, so the line must
    not assert that none was — it shows the measured figure and says the
    amount is too small to charge on."""
    from src.margin_interest import format_daily_line
    line = format_daily_line(-0.99, 6.25)
    assert line == (
        "💳 margin interest: $0.00/day — overnight cash -$0.99, "
        "too small to charge on"
    )
    assert "nothing borrowed" not in line


def test_format_daily_line_still_renders_a_real_debit_exactly_as_before():
    """The non-zero line is UNCHANGED by the visibility work — same figures,
    same wording, ESTIMATE label verbatim."""
    from src.margin_interest import (
        ESTIMATE_LABEL, build_estimate, format_alert_line, format_daily_line,
    )
    line = format_daily_line(-5_729.0, 6.25)
    assert line == format_alert_line(build_estimate(5_729.0, 6.25))
    assert line.startswith("💳 margin interest: $0.99/day (~$358/yr) on $5,729")
    assert "carried overnight at 6.25%" in line
    assert ESTIMATE_LABEL in line


def test_format_daily_line_calls_a_missing_rate_a_fault_not_a_zero():
    """A deleted or mis-keyed rate must NOT print a reassuring zero on the
    one day the owner most needs to know the tracker is broken — that is
    the inverse of what "so I know it's still working" asked for."""
    from src.margin_interest import RATE_UNAVAILABLE_LINE, format_daily_line
    for bad_rate in (None, 0.0, -1.0):
        assert format_daily_line(-5_729.0, bad_rate) == RATE_UNAVAILABLE_LINE
    assert "$0.00" not in RATE_UNAVAILABLE_LINE


def test_format_daily_line_calls_an_unreadable_cash_balance_a_fault():
    from src.margin_interest import UNAVAILABLE_LINE, format_daily_line
    assert format_daily_line(None, 6.25) == UNAVAILABLE_LINE
    assert "$0.00" not in UNAVAILABLE_LINE


def test_format_alert_line_keeps_its_silent_contract():
    """Guard: the always-speak policy must NOT have been pushed down into
    the shared formatter, which cannot tell "no debit" from "no rate"."""
    from src.margin_interest import build_estimate, format_alert_line
    assert format_alert_line(None) is None
    assert format_alert_line(build_estimate(0.0, 6.25)) is None


def test_margin_interest_is_its_own_section_not_part_of_the_cost_block(monkeypatch):
    """Owner, 2026-09-18: it sat with model spend and the prepaid balance
    since 2026-09-01 and he never found it. Borrowed money is not an
    operating expense, and filing it under running costs is what made it
    invisible. Asserts a blank separator line between the two."""
    import src.notifier as n

    monkeypatch.setattr(n, "_REHEARSAL_MODE", False)
    monkeypatch.setattr(n, "_session_cost_line", lambda run_id: "🧠 AI cost: $0.12")
    monkeypatch.setattr(n, "_day_cost_line", lambda: "📅 today: $0.34")
    monkeypatch.setattr(n, "_openrouter_balance_line", lambda: "🔋 OpenRouter: $7.10 left")
    monkeypatch.setattr(
        n, "_margin_interest_lines",
        lambda: ["💳 margin interest: $0.00/day — overnight cash $1,000.00, nothing borrowed"],
    )
    msg = n.format_session_result("morning", {"status": "ok", "run_id": "r"}, 5.0)
    lines = msg.split("\n")
    balance_at = next(i for i, ln in enumerate(lines) if "OpenRouter" in ln)
    margin_at = next(i for i, ln in enumerate(lines) if "margin interest" in ln)
    assert margin_at > balance_at
    assert any(not lines[i].strip() for i in range(balance_at + 1, margin_at)), (
        "margin interest is still glued to the running-cost block"
    )


# ---------------------------------------------------------------------------
# 10. Weekend / holiday carry — owner-confirmed from Alpaca's docs 2026-09-23
# ---------------------------------------------------------------------------
# Alpaca charges margin interest for EVERY calendar day a debit balance is
# carried, trading day or not. A Friday's overnight is charged 3 days
# (Fri+Sat+Sun); a Friday before a Monday holiday, 4. The flat per-day
# figure understated tonight's bill whenever the carry spans a closure.

from datetime import date as _date  # noqa: E402


def _weekday_calendar(holidays=()):
    """Stub for `AlpacaBroker.is_trading_day`: Mon-Fri open, minus holidays."""
    return lambda d: d.weekday() < 5 and d not in holidays


def _stub_broker_with_calendar(monkeypatch, cash, today, calendar, tmp_path=None):
    monkeypatch.setattr(
        "src.config.load_config",
        lambda *a, **kw: MagicMock(risk=MagicMock(margin_interest_rate_pct=6.25)),
    )
    monkeypatch.setattr("src.api.deps.get_alpaca_credentials", lambda: ("k", "s"))
    monkeypatch.setattr("src.api.deps.get_alpaca_paper", lambda: True)
    monkeypatch.setattr("src.util.time.et_today", lambda: today)
    monkeypatch.setattr(
        "src.execution.broker.AlpacaBroker",
        lambda **kw: MagicMock(
            get_account=lambda: {"cash": cash},
            get_margin_interest_activities=lambda: [],
            is_trading_day=calendar,
        ),
    )
    if tmp_path is not None:
        import src.notifier as n
        monkeypatch.setattr(n, "_DB_PATH", tmp_path / "quant_agent.db")


def test_days_charged_weeknight_is_one():
    from src.margin_interest import days_charged_until_next_trading_day
    wed = _date(2026, 9, 23)
    assert wed.weekday() == 2
    assert days_charged_until_next_trading_day(_weekday_calendar(), wed) == 1


def test_days_charged_friday_is_three():
    from src.margin_interest import days_charged_until_next_trading_day
    fri = _date(2026, 9, 25)
    assert fri.weekday() == 4
    assert days_charged_until_next_trading_day(_weekday_calendar(), fri) == 3


def test_days_charged_friday_before_monday_holiday_is_four():
    from src.margin_interest import days_charged_until_next_trading_day
    fri = _date(2026, 9, 4)          # Labor Day 2026 is Mon 2026-09-07
    assert fri.weekday() == 4
    cal = _weekday_calendar(holidays={_date(2026, 9, 7)})
    assert days_charged_until_next_trading_day(cal, fri) == 4


def test_days_charged_degrades_to_one_when_calendar_raises():
    from src.margin_interest import days_charged_until_next_trading_day

    def boom(_d):
        raise RuntimeError("calendar endpoint down")
    assert days_charged_until_next_trading_day(boom, _date(2026, 9, 25)) == 1


def test_days_charged_degrades_to_one_when_no_trading_day_within_bound():
    """A calendar that says 'closed' forever is a broken read, not a
    market closure — bounded search, then the old flat figure."""
    from src.margin_interest import days_charged_until_next_trading_day
    assert days_charged_until_next_trading_day(lambda d: False, _date(2026, 9, 25)) == 1


def test_estimate_period_usd_is_daily_times_days_charged():
    from src.margin_interest import build_estimate
    est = build_estimate(5_000.0, 6.25, days_charged=3)
    assert est.days_charged == 3
    assert est.period_usd == pytest.approx(est.daily_usd * 3)
    # The per-day and annual figures are UNCHANGED by the multi-day carry.
    assert est.daily_usd == pytest.approx(build_estimate(5_000.0, 6.25).daily_usd)
    assert est.annual_usd == pytest.approx(build_estimate(5_000.0, 6.25).annual_usd)


def test_build_estimate_defaults_to_one_day_so_existing_callers_are_unchanged():
    from src.margin_interest import build_estimate
    est = build_estimate(5_000.0, 6.25)
    assert est.days_charged == 1
    assert est.period_usd == pytest.approx(est.daily_usd)


def test_format_daily_line_weeknight_has_no_multi_day_clause():
    from src.margin_interest import ESTIMATE_LABEL, format_daily_line
    line = format_daily_line(-5_000.0, 6.25, days_charged=1)
    assert line == (
        "💳 margin interest: $0.87/day (~$312/yr) on $5,000 carried overnight "
        f"at 6.25% — {ESTIMATE_LABEL}"
    )
    assert "days" not in line.split(" — ")[0]


def test_format_daily_line_friday_names_the_three_day_weekend_total():
    from src.margin_interest import ESTIMATE_LABEL, format_daily_line
    line = format_daily_line(-5_000.0, 6.25, days_charged=3)
    assert line == (
        "💳 margin interest: $0.87/day (~$312/yr) on $5,000 carried overnight "
        "at 6.25% — carried over the weekend that's 3 days ≈ $2.60"
        f" — {ESTIMATE_LABEL}"
    )


def test_format_daily_line_long_weekend_names_four_days():
    from src.margin_interest import format_daily_line
    line = format_daily_line(-5_000.0, 6.25, days_charged=4)
    assert "carried over the long weekend that's 4 days ≈ $3.47" in line


def test_format_daily_line_zero_and_fault_states_ignore_days_charged():
    """The multi-day clause belongs only on a real debit; the zero line and
    both 'not available' lines are byte-for-byte what they were."""
    from src.margin_interest import (
        RATE_UNAVAILABLE_LINE, UNAVAILABLE_LINE, format_daily_line,
    )
    assert format_daily_line(None, 6.25, days_charged=3) == UNAVAILABLE_LINE
    assert format_daily_line(-5_000.0, None, days_charged=3) == RATE_UNAVAILABLE_LINE
    assert format_daily_line(1_000.0, 6.25, days_charged=3) == format_daily_line(1_000.0, 6.25)
    assert format_daily_line(-0.99, 6.25, days_charged=3) == format_daily_line(-0.99, 6.25)


def test_margin_interest_lines_weeknight_shows_one_day(monkeypatch, tmp_path):
    """A single weeknight's $5,000 debit at 6.25%/360 = $0.8681, charged
    for 1 day — that whole period_usd is what the (freshly-empty)
    cumulative buckets show, since it's the only row ever persisted."""
    import src.notifier as n
    monkeypatch.setattr(n, "_REHEARSAL_MODE", False)
    _stub_broker_with_calendar(
        monkeypatch, -5_000.0, _date(2026, 9, 23), _weekday_calendar(), tmp_path,
    )
    lines = n._margin_interest_lines()
    assert "this week $0.87" in lines[0]
    assert "(est.)" in lines[0]


def test_margin_interest_lines_friday_shows_three_days_via_broker_calendar(monkeypatch, tmp_path):
    """Friday's carry is 3 days: $0.8681/day x 3 = $2.6042, and THAT total
    (not the per-day figure) is what the cumulative buckets sum."""
    import src.notifier as n
    monkeypatch.setattr(n, "_REHEARSAL_MODE", False)
    _stub_broker_with_calendar(
        monkeypatch, -5_000.0, _date(2026, 9, 25), _weekday_calendar(), tmp_path,
    )
    lines = n._margin_interest_lines()
    assert "this week $2.60" in lines[0]
    assert "(est.)" in lines[0]


def test_margin_interest_lines_calendar_failure_degrades_to_one_day_not_an_error(monkeypatch, tmp_path):
    """A broken calendar read must NOT turn a readable balance into
    'not available' — the cash WAS read; only the day count is unknown."""
    import src.notifier as n
    monkeypatch.setattr(n, "_REHEARSAL_MODE", False)

    def boom(_d):
        raise RuntimeError("calendar endpoint down")
    _stub_broker_with_calendar(monkeypatch, -5_000.0, _date(2026, 9, 25), boom, tmp_path)
    lines = n._margin_interest_lines()
    assert "this week $0.87" in lines[0]
    assert "not available" not in lines[0]


# ---------------------------------------------------------------------------
# 11. Cumulative view — owner ask 2026-09-24 (this week / month / 6-month
#     window / all-time), preferring broker-actual over the ESTIMATE
#     fallback, and skipping zero-interest months.
# ---------------------------------------------------------------------------

from src.margin_interest import (  # noqa: E402
    MAX_LOOKBACK_MONTHS,
    bucket_broker_activities,
    bucket_estimate_rows,
    compute_cumulative_margin_interest,
    format_cumulative_line,
)


def test_bucket_estimate_rows_splits_this_week_and_current_month():
    today = _date(2026, 9, 24)  # Thursday
    rows = [
        {"date": "2026-09-22", "period_usd": 1.0},  # this week + this month
        {"date": "2026-09-24", "period_usd": 2.0},  # this week + this month
        {"date": "2026-09-01", "period_usd": 5.0},  # this month only
    ]
    result = bucket_estimate_rows(rows, today)
    assert result.this_week_usd == pytest.approx(3.0)
    assert result.current_month_usd == pytest.approx(8.0)
    assert result.current_month_label == "September 2026"
    assert result.source == "estimate"
    assert result.is_estimate is True


def test_bucket_estimate_rows_lists_prior_months_newest_first_skips_zero():
    today = _date(2026, 9, 24)
    rows = [
        {"date": "2026-08-05", "period_usd": 10.0},   # August: nonzero
        {"date": "2026-07-10", "period_usd": 0.0},    # July: zero -> skipped
        {"date": "2026-06-15", "period_usd": 4.0},    # June: nonzero
        {"date": "2026-03-01", "period_usd": 99.0},   # outside the 6-month window
    ]
    result = bucket_estimate_rows(rows, today)
    labels = [m["label"] for m in result.prior_months]
    assert labels == ["August 2026", "June 2026"]
    assert result.prior_months[0]["usd"] == pytest.approx(10.0)
    assert result.prior_months[1]["usd"] == pytest.approx(4.0)
    assert "July 2026" not in labels
    # The March row is outside the 6-total-month window (current + 5 prior)
    # and must not silently inflate all_time either — all_time sums
    # everything GIVEN to this function, so a caller must not hand it more
    # than it wants counted; this test pins that the WINDOW (not all_time)
    # is what's bounded to 6 months.
    assert result.all_time_usd == pytest.approx(10.0 + 0.0 + 4.0 + 99.0)


def test_bucket_estimate_rows_window_is_six_months_total():
    assert MAX_LOOKBACK_MONTHS == 6


def test_bucket_estimate_rows_all_time_since_is_earliest_persisted_date():
    today = _date(2026, 9, 24)
    rows = [
        {"date": "2026-09-24", "period_usd": 1.0},
        {"date": "2026-08-01", "period_usd": 2.0},
    ]
    result = bucket_estimate_rows(rows, today)
    assert result.all_time_since == "2026-08-01"
    assert result.all_time_usd == pytest.approx(3.0)


def test_bucket_estimate_rows_empty_is_no_data_not_a_fabricated_zero():
    result = bucket_estimate_rows([], _date(2026, 9, 24))
    assert result.source == "no_data"
    assert result.this_week_usd == 0.0
    assert result.all_time_usd == 0.0


def test_bucket_broker_activities_returns_none_when_broker_never_confirmed():
    """No INT row ever -> the caller must fall back to the estimate, never
    read this as 'broker confirms zero interest charged, ever'."""
    assert bucket_broker_activities([], _date(2026, 9, 24)) is None


def test_bucket_broker_activities_sums_confirmed_charges_by_bucket():
    today = _date(2026, 9, 24)
    activities = [
        {"date": "2026-09-24", "net_amount": -1.50},   # this week/month
        {"date": "2026-08-15", "net_amount": -3.00},   # prior month
        {"date": "2026-01-01", "net_amount": -20.00},  # all-time, pre-window
    ]
    result = bucket_broker_activities(activities, today)
    assert result.source == "broker_actual"
    assert result.is_estimate is False
    assert result.this_week_usd == pytest.approx(1.50)
    assert result.current_month_usd == pytest.approx(1.50)
    assert result.prior_months == [{"label": "August 2026", "usd": pytest.approx(3.00)}]
    assert result.all_time_usd == pytest.approx(24.50)
    assert result.all_time_since == "2026-01-01"


def test_compute_cumulative_prefers_broker_actual_over_estimate():
    today = _date(2026, 9, 24)
    broker_activities = [{"date": "2026-09-24", "net_amount": -1.0}]
    estimate_rows = [{"date": "2026-09-01", "period_usd": 999.0}]
    result = compute_cumulative_margin_interest(broker_activities, estimate_rows, today)
    assert result.source == "broker_actual"
    assert result.all_time_usd == pytest.approx(1.0)


def test_compute_cumulative_falls_back_to_estimate_when_broker_never_confirmed():
    today = _date(2026, 9, 24)
    result = compute_cumulative_margin_interest([], [{"date": "2026-09-24", "period_usd": 5.0}], today)
    assert result.source == "estimate"
    assert result.is_estimate is True
    assert result.all_time_usd == pytest.approx(5.0)


def test_compute_cumulative_no_data_when_neither_source_has_anything():
    result = compute_cumulative_margin_interest([], [], _date(2026, 9, 24))
    assert result.source == "no_data"


def test_format_cumulative_line_no_caveat_paragraph_only_est_tag():
    """The old ESTIMATE_LABEL paragraph is gone; the ENTIRE estimate marker
    is the short '(est.)' tag, per the owner's 2026-09-24 ask to remove the
    caveat block from every rendering."""
    result = bucket_estimate_rows(
        [{"date": "2026-09-24", "period_usd": 1.23}], _date(2026, 9, 24),
    )
    line = format_cumulative_line(result)
    assert "(est.)" in line
    assert ESTIMATE_LABEL not in line
    assert "unconfirmed" not in line.lower()
    assert "this week" in line
    assert "all-time" in line


def test_format_cumulative_line_broker_actual_carries_no_est_tag():
    result = bucket_broker_activities(
        [{"date": "2026-09-24", "net_amount": -1.23}], _date(2026, 9, 24),
    )
    line = format_cumulative_line(result)
    assert "(est.)" not in line


def test_format_cumulative_line_no_data_state():
    result = compute_cumulative_margin_interest([], [], _date(2026, 9, 24))
    line = format_cumulative_line(result)
    assert "no data yet" in line
