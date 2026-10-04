"""Margin-interest response models, carved out of schemas.py (re-exported there)."""
from __future__ import annotations

from pydantic import BaseModel


class MarginInterestEstimate(BaseModel):
    """Margin interest ESTIMATE (spec §11.2) — MEASURES only, never a risk
    decision.

    `label` carries the ESTIMATE framing verbatim so no consumer of this
    response can render the figure without it — paper trading's own
    handling of margin interest is unconfirmed either way, see
    `src.margin_interest`.

    Three shapes, per `broker_reads.read_margin_interest` (owner decision
    2026-09-18, "every day, even if it's zero"):
      * a carried debit balance — every field set, `label` set;
      * nothing borrowed — an explicit `0.0` in the three dollar fields
        with the real `rate_pct`, `label` `None` (a certain zero is not an
        estimate) and `error` `None`;
      * a fault — dollar fields `None` and `error` set.
    `error` is the ONLY safe way to tell a real zero from a failed read;
    a `None` figure must never be rendered as zero."""
    debit_balance: float | None = None
    rate_pct: float | None = None
    daily_usd: float | None = None
    annual_usd: float | None = None
    label: str | None = None
    #: Calendar days tonight's carry spans before the next trading day —
    #: 1 on a normal weeknight, 3 over a weekend (Friday), 4 before a
    #: Monday holiday. Same figure the Telegram alert names
    #: (`src.margin_interest.days_charged_until_next_trading_day`); `None`
    #: only in the fault case, alongside the other numeric fields.
    days_charged: int | None = None
    #: `daily_usd * days_charged` — what tonight's carry actually costs in
    #: total, not just the per-day rate. `daily_usd` is deliberately left
    #: unchanged by the multi-day carry so the dashboard can show both the
    #: per-day figure and the real period total without conflating them.
    period_usd: float | None = None
    #: Result of comparing the estimate against the broker's own `INT`
    #: account-activity records — plain-language, e.g. "broker confirmed
    #: a margin interest charge of $X..." or "no INT activity ... not
    #: confirmed". `None` until a debit balance has actually been carried
    #: overnight at least once.
    broker_check_note: str | None = None
    error: str | None = None
    #: The owner-facing cumulative view (this week / current month / up to
    #: 6 months / all-time) that replaced the per-day/per-year figures
    #: above as the cockpit/Telegram headline, 2026-09-24. `None` only on a
    #: read failure — a genuine "nothing measured yet" state is
    #: `MarginInterestCumulative(source="no_data", ...)`, not `None`.
    cumulative: "MarginInterestCumulative | None" = None


class MarginInterestCumulative(BaseModel):
    """Owner ask, 2026-09-24: replace the per-day/per-year figures and the
    ESTIMATE-caveat paragraph with a running cumulative total — this week,
    the current month, each of up to five more recent months that had any
    interest (months with none are omitted, never padded in as zero), and
    an all-time total.

    `source` is `"broker_actual"` when every dollar counted is a
    broker-confirmed `INT` charge (Alpaca's own permanent activity ledger —
    covers the account's full history, no local storage needed),
    `"estimate"` when it falls back to our own persisted daily-accrual
    ESTIMATE rows (only ever covers days since this tracker started
    persisting — see `all_time_since`), or `"no_data"` when neither source
    has anything yet. `is_estimate` is the single small "est." marker the
    cockpit/Telegram show in place of the old caveat paragraph.
    """
    this_week_usd: float
    current_month_usd: float
    current_month_label: str
    prior_months: list[dict]
    all_time_usd: float
    #: The date the `all_time_usd` total is actually counted from. For the
    #: broker-actual source this is the earliest `INT` activity Alpaca has
    #: on record. For the estimate fallback it is the earliest day THIS
    #: TRACKER persisted a row — NOT necessarily the day the desk first
    #: went on margin, since no historical debit balance was ever stored
    #: before this feature existed. Always shown alongside the total so
    #: "all-time" is never misread as more complete than it is.
    all_time_since: str
    is_estimate: bool
    source: str
