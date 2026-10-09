"""How a failed intraday opportunity scan is named (carried from PR 982).

Split out of `pipeline_intraday.py`, which is tracked by the shrink-only
file-size baseline and may not grow.
"""

from __future__ import annotations

import logging

from src.cost_circuit import is_payment_refusal

logger = logging.getLogger(__name__)

PRESERVED = "fill reconciliation and stop-coverage repair"

_BANNERS = {
    "intraday_scan_out_of_credit": (
        "\U0001f6d1 OUT OF CREDIT: the search for intraday opportunities "
        "stopped because the paid research account has no credit "
        "left, so it found nothing. This is not a fault in the "
        "desk. The automatic loss check above ran normally."
    ),
    "intraday_scan_crashed": (
        "\U0001f6d1 CRASHED: the search for intraday opportunities stopped "
        "with a fault, so it found nothing. The automatic loss "
        "check above ran normally."
    ),
}


# Rehearsal-report wording for the new status (ops/rehearsal/report.py merges
# it into STATUS_PLAIN; that file is at its shrink-only size cap).
OUT_OF_CREDIT_PLAIN = {
    "intraday_scan_out_of_credit": (
        "The intra-session check's opportunity scan stopped because the "
        "paid research account has no credit left, so it found nothing. "
        "This is not a fault in the desk. The deterministic loss check that "
        "runs before it already completed normally."
    ),
}


def scan_failure_banner(status: str) -> str:
    """The owner-feed sentence for a failed-scan status."""
    text = _BANNERS[status]
    if status == "intraday_scan_out_of_credit":
        from src.llm_balance_runway import balance_line

        text = f"{text} {balance_line()}"
    return text


def failed_scan_result(e: Exception, run_id: str) -> dict:
    """Name a scan failure truthfully; same loud, non-deciding outcome."""
    # MEASURED, production DB read-only 2026-10-01: of the 9
    # `intraday_scan_crashed` outcomes in 116 recorded half-hourly
    # checks, 9 of 9 were HTTP 402 "requires more credits" from
    # OpenRouter (2026-09-28 18:20 ET .. 2026-09-29 19:47 ET). Not
    # one was a fault in this desk's code. Reporting an empty
    # research account as "the scan for movers crashed" sends the
    # owner looking for broken software; the true state is that the
    # account has no credit and the provider refused before
    # generating. Same loud, unhealthy, non-deciding outcome --
    # nothing is swallowed, nothing is retried, no number is
    # invented -- only the name is made true.
    if is_payment_refusal(e):
        from src.llm_balance_runway import balance_line

        logger.error(
            "Intraday opportunity scan refused: the paid research account is out of credit (non-fatal): %s. %s",
            e,
            balance_line(),
        )
        status = "intraday_scan_out_of_credit"
    else:
        logger.error("Intraday opportunity scan crashed (non-fatal): %s", e)
        status = "intraday_scan_crashed"
    return {
        "status": status,
        "run_id": run_id,
        "error": str(e),
        "error_type": type(e).__name__,
        "preserved": PRESERVED,
    }
