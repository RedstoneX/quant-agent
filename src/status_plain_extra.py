"""Plain wording for the desk states that had none (filled 2026-10-02).

Split out of `ops/rehearsal/report.py`, which is tracked by the shrink-only
file-size baseline and may not grow; the report merges `EXTRA_PLAIN` into
`STATUS_PLAIN`. Each entry says what happened and whether the fault is in the
desk or outside it. Where one token has several producers, the wording says
so rather than picking one meaning.
"""

from __future__ import annotations

from src.intraday_scan_outcome import OUT_OF_CREDIT_PLAIN

UNWORDED_PLAIN = {
    # Produced by the pipeline's kill-switch check (src/pipeline.py).
    "kill_switch_halted": (
        "The desk stopped itself on purpose: the kill-switch file exists, so "
        "the run halted before touching the broker or any paid analysis. "
        "This is not a fault. Someone placed that file; the desk stays "
        "stopped until it is removed. Positions already held keep their "
        "protective stops; only new orders are refused."
    ),
    # Produced by the pre-market data refresh (earnings preprocess session),
    # as the `smart_money_refresh` sub-result, when the switch is off.
    "disabled": (
        "The pre-market refresh of insider and congressional trading data "
        "is switched off in the desk's settings, so it did not run. This is "
        "a setting, not a fault, but the research seats get no fresh "
        "smart-money data while it stays off."
    ),
    # Produced by the same refresh, and by the congressional-trading source,
    # when a data provider call raised an exception.
    "provider_error": (
        "The pre-market refresh of insider and congressional trading data "
        "failed because a data source (the SEC filings feed or the "
        "congressional-trades feed) could not be read. The cause is outside "
        "the desk, so smart-money research runs on older data until the "
        "next successful refresh."
    ),
    # Produced by two different background jobs, not by a trading session:
    # the daily account export and the stock-universe screening pass.
    "error": (
        "A background job failed. This token comes from two different jobs, "
        "so the reported reason below says which: the daily account export "
        "(no portfolio data, or the Telegram upload failed) or the "
        "stock-universe screening pass (the desk's own code raised). "
        "No trading session was involved and no order was affected."
    ),
}

EXTRA_PLAIN = {**OUT_OF_CREDIT_PLAIN, **UNWORDED_PLAIN}
