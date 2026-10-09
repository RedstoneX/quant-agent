"""src.cost_circuit.refusal -- moved verbatim from src/cost_circuit.py; see the package docstring."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, TypeVar
from src.cost_circuit.classification import _cause_chain


# === PAYMENT REFUSAL (2026-10-01) ========================================
# Measured against the production database on 2026-10-01: the paid research
# account ran out of credit and the provider answered HTTP 402 with "This
# request requires more credits, or fewer max_tokens. You requested up to
# 16000 tokens, but can only afford 843." The affordable figure fell across
# successive calls (13290, 7311, 843, 811, 775) -- an emptying balance, not a
# transient refusal. A refusal of this shape cannot succeed on a retry: only
# a human topping the account up changes it.
#
# The test is the STATUS CODE, never the English. A provider can reword its
# message at any time, and the desk has already been burned once by matching
# on text (the case-sensitive health matching that raised a daily false
# alarm). Text may only ever REFINE a status-code match, never stand in for
# one.
#
# NOT included: a 429 that says credits could not be verified. That one is
# genuinely transient -- the provider is saying it could not check the
# balance right now, not that the balance is gone -- so it keeps its retries.
_PAYMENT_REFUSAL_STATUS_CODES = frozenset({402})

#: Plain-English cause, reused by the circuit's trigger detail and by every
#: owner-facing message, so the desk never reports "cost unknown" for a
#: failure whose cause it actually knows.
OUT_OF_CREDIT_DETAIL = (
    "the paid research account is out of credit: the provider refused the "
    "call outright, so nothing was spent on it. Top the account up to turn "
    "paid analysis back on"
)


def out_of_credit_detail() -> str:
    """OUT_OF_CREDIT_DETAIL plus the remaining balance, one shared wording."""
    from src.llm_balance_runway import balance_line

    return f"{OUT_OF_CREDIT_DETAIL}. {balance_line()}"


#: `trigger_code` written instead of `failed_call_unknown_cost` when the
#: cause is a payment refusal.
OUT_OF_CREDIT_TRIGGER_CODE = "provider_out_of_credit"


def is_payment_refusal(error: BaseException | None) -> bool:
    """True when the provider refused the call for lack of credit.

    Keyed on the HTTP status code carried by the exception (or by anything
    in its cause chain), so a reworded provider message cannot change the
    answer.
    """
    if error is None:
        return False
    for node in _cause_chain(error):
        status = getattr(node, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool) and status in _PAYMENT_REFUSAL_STATUS_CODES:
            return True
    return False


def any_payment_refusal(
    error: BaseException | None,
    attempt_errors: "list[BaseException] | None" = None,
) -> bool:
    """True when the call failed, at any attempt, on a payment refusal."""
    if is_payment_refusal(error):
        return True
    return any(is_payment_refusal(exc) for exc in (attempt_errors or []))


def _fmt_settled(value: float | None) -> str:
    """A settled-cost dollar amount for any owner-facing alert built in this
    module -- never the raw f"${value:.4f}" this module used to print.

    2026-09-29: every alert below prints a genuinely tiny settled figure
    (sub-cent daily/session spend is common on the free-tier-heavy path),
    and four decimal places was this module's only way to show that
    without rounding it to "$0.00". But a `$` token with anything but 2
    decimal digits is exactly the shape `src/notifier.py`'s
    `_redact_malformed_numbers` exists to strip before anything reaches
    the owner, so every one of these alerts had its cost line redacted
    out from under it -- five times in one afternoon, per the log. Lazily
    imported (matches this module's existing `from src.notifier import
    TelegramNotifier` pattern) so this module keeps no module-level
    dependency on `src/notifier.py`.
    """
    from src.notifier import format_settled_money

    return format_settled_money(value)


class PaidAnalysisSuspended(RuntimeError):
    """Raised before a paid provider request when the circuit is open."""

    def __init__(self, trigger: str, state: dict[str, Any] | None = None):
        self.trigger = trigger
        self.state = state or {}
        super().__init__(f"paid analysis suspended: {trigger}")


class OptionalPaidAnalysisRetrySkipped(RuntimeError):
    """An optional repair was not reserved because its retry budget was spent.

    This is not a circuit failure: no provider request was attempted and the
    caller may safely retain already-completed primary analysis. All mandatory
    limits and every non-retry trigger continue to use ``PaidAnalysisSuspended``.
    """

    def __init__(self, trigger: str, state: dict[str, Any] | None = None):
        self.trigger = trigger
        self.state = state or {}
        super().__init__(f"optional paid-analysis retry skipped: {trigger}")


@dataclass
class CallReservation:
    """A logical-call handle. NOT a dollar reservation (item 14, 2026-09-02):
    it carries the call's identity (run/mode/agent/model) through the retry
    loop so `before_provider_attempt`/`complete_call`/`fail_call` can find
    the right session row, and `attempt_count` tracks provider attempts
    WITHIN this one logical call purely in-process (this object's lifetime
    is exactly one `BaseAgent._execute` call, all on one process/thread) so
    `max_provider_attempts_per_call` still bounds a retry/failover storm.
    """

    reservation_id: str
    run_id: str
    mode: str
    agent_name: str
    model: str
    attempt_count: int = 0
