"""An empty research account is not a crash (2026-10-01).

Carried from PR 982 into its own module: tests/test_intraday_scan.py is
tracked by the shrink-only file-size baseline and may not grow.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.config import IntradayScanConfig
from tests.pipeline_factory import build_pipeline

# ---------- an empty research account is not a crash (2026-10-01) ----------


class _OutOfCreditError(RuntimeError):
    """The shape OpenRouter raises on an exhausted balance: HTTP 402."""

    status_code = 402


def test_payment_refusal_is_named_out_of_credit_not_crashed():
    """MEASURED, production DB read-only 2026-10-01: all 9
    `intraday_scan_crashed` outcomes across 116 recorded half-hourly checks
    (2026-09-28 18:20 ET .. 2026-09-29 19:47 ET) carried the SAME error --
    OpenRouter HTTP 402 "This request requires more credits". None was a
    fault in this desk's code, yet every one of them was reported to the
    owner as "the scan for movers crashed".

    A false statement about the desk's own state is a root-cause defect, so
    the refusal gets its own true name. It stays loud: still an unhealthy,
    non-deciding, owner-visible outcome, with the provider's own message
    preserved on the machine line. Nothing is swallowed, nothing is retried,
    and no threshold, timeout or budget number is introduced.
    """
    p = build_pipeline(broker=MagicMock(), db=MagicMock(), risk_engine=MagicMock())
    p.config = SimpleNamespace(
        trading=SimpleNamespace(universe=["AAPL"], lookback_days=100),
        intraday_scan=IntradayScanConfig(enabled=True),
    )
    p.broker.is_trading_day.return_value = True
    p.broker.get_account.return_value = {
        "cash": 10000.0,
        "portfolio_value": 10100.0,
        "last_equity": 10000.0,
        "non_marginable_buying_power": 10000.0,
    }
    p.broker.get_positions.return_value = []
    p._drain_pending_protection_restores = MagicMock()
    p._reconcile_stop_coverage = MagicMock(return_value=[])
    p._reconcile_orphan_pending_submits = MagicMock()
    p._is_trading_day = MagicMock(return_value=True)
    p._run_intraday_opportunity_scan = MagicMock(
        side_effect=_OutOfCreditError("Error code: 402 - This request requires more credits")
    )

    result = p.run_intra_check()

    nested = result["intraday_scan"]
    assert result["status"] == "ok"
    assert nested["status"] == "intraday_scan_out_of_credit"
    assert nested["status"] != "intraday_scan_crashed"
    # The provider's own words survive for the operator.
    assert "402" in nested["error"]
    # Free safety work is explicitly still reported as preserved.
    assert "stop-coverage repair" in nested["preserved"]


def test_out_of_credit_is_a_non_deciding_outcome_like_the_crash_it_replaces():
    """Renaming the cause must not quietly move it into the healthy set:
    a scan that never ran did not decide anything, whichever name it wears.
    """
    from src.notifier import _STATUS_LABELS
    from src.refusal_signature import NON_DECIDING_STATUSES

    assert "intraday_scan_out_of_credit" in NON_DECIDING_STATUSES
    assert "credit" in _STATUS_LABELS["intraday_scan_out_of_credit"]


def test_owner_feed_banner_says_out_of_credit_not_fault():
    from src.intraday_scan_outcome import scan_failure_banner

    assert "OUT OF CREDIT" in scan_failure_banner("intraday_scan_out_of_credit")
    assert "CRASHED" in scan_failure_banner("intraday_scan_crashed")
