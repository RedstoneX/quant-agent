"""Margin interest tracker — spec `docs/QAMC_REMEDIATION_SPEC.md` §11.2.

**MEASURES, never gates.** Nothing here touches sizing, execution, or a risk
threshold. `allow_margin` stays whatever the risk config says; this module
only computes what a carried debit balance would cost, so the desk can see
the number rather than discover it later.

**The rule.** Alpaca charges margin interest ONLY on the END-OF-DAY
(overnight) debit balance — intraday leverage is free. A desk that runs
leveraged intraday and trims back to flat before the close pays nothing.
Formula (Alpaca's own day-count convention, 360 not 365)::

    daily_interest = (overnight_debit_balance * rate_pct / 100) / 360

That is a *design lever*, not a footnote: `overnight_debit_balance()` below
takes only the END-OF-DAY cash figure as input. There is no code path in
this module that can see an intraday high and charge for it — the design
lever is pinned by the function signature, not by a comment.

**Why every number here is a labelled ESTIMATE, not an observed cost.**
Alpaca's own paper-trading comparison lists short-borrow fees as "Coming
Soon". Whether paper trading ALSO simulates margin interest is not
documented in either direction — secondary sources say it does not, and
Alpaca's docs neither confirm nor deny it. Reporting this as an observed
cost would risk teaching the desk that its largest recurring cost, at a
sustained 2.0x, is zero. So every rendering of this figure — the morning
alert, the dashboard — carries `ESTIMATE_LABEL` verbatim, and
`compare_estimate_to_broker_activity()` exists specifically to settle the
open question empirically: on the first morning after a debit balance is
carried overnight, the account's own `INT` activity records either show a
charge or they do not. This module does not pre-judge which.

**FIRST MEASUREMENT, 2026-09-18 — ONE NIGHT, NOT PROOF.** The paper account
carried a debit balance of -$915.83 overnight from 2026-09-17 into
2026-09-18 (equity $9,778.72, long market value $10,694.55, i.e. gross
~1.09x). `/v2/account/activities/INT` returned ZERO rows, for `after`
2026-09-17, for `after` 2026-09-01, and with no `after` filter at all. The
endpoint itself was confirmed live in the same probe — the same client
returned 78 `FILL` rows for `after=2026-09-15` — so the empty INT result is
an answer, not a broken read. Read this as: **paper trading did not charge
margin interest on that night's debit.** It remains a single observation on
a small balance, so `ESTIMATE_LABEL` stays on every rendering and nothing in
this module or anywhere else gates on the result. Re-run the same query
after a larger or longer-carried debit before treating the cost as zero.

**Today's actual state.** `allow_margin` has been `True` (2.0x gross) since
2026-09-02. That flag on its own says nothing about whether a negative cash
balance is actually being carried on any given day — callers of this module
must key off `cash` itself, never off `allow_margin`, to decide whether
there is anything to report. `cash_only`'s hard-block on a plain BUY taking
cash negative applies precisely when `allow_margin` is `False`; a COVER is
deliberately exempt regardless (D10, `src/risk/rules.py`;
`src/agents/portfolio_manager.py`'s DE-LEVER MANDATE already treats
"cash negative with `allow_margin` False" as a real state a session can
reach). See `src/api/broker_reads.py::read_margin_interest` and
`src/notifier.py::_margin_interest_lines`, both of which read `cash`
unconditionally for exactly this reason. Every function below is written
to be silent and side-effect-free at an actual zero (or non-negative)
balance, per the spec's requirement that this tracker not add noise to a
book that owes nothing — 11.2's gross-exposure cap and de-levering ladder,
built separately, are what keep balances bounded now that margin is
deliberately turned on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from src.risk.constants import MARGIN_DEFICIT_FLOOR_USD

logger = logging.getLogger(__name__)

#: Alpaca's own day-count convention for margin interest — 360, not 365.
DAYS_PER_YEAR_ALPACA_CONVENTION = 360

#: Verbatim label every rendering of a margin-interest figure must carry.
#: See the module docstring for why: paper trading's treatment of margin
#: interest is unconfirmed in either direction, so this must never read as
#: an observed broker charge.
ESTIMATE_LABEL = (
    "ESTIMATE — paper trading's handling of margin interest is unconfirmed; "
    "not an observed broker charge"
)


@dataclass(frozen=True)
class MarginInterestEstimate:
    """A single day's margin-interest ESTIMATE for a carried debit balance.

    Every field here is derived, deterministic arithmetic from
    `debit_balance` and `rate_pct` — nothing is fetched or guessed. The
    `label` field is included on the object itself (not just in the
    formatted alert string) so any consumer that renders this figure,
    now or later, cannot accidentally drop the ESTIMATE framing.
    """

    debit_balance: float
    rate_pct: float
    daily_usd: float
    annual_usd: float
    label: str = ESTIMATE_LABEL
    #: How many calendar days tonight's end-of-day debit accrues before the
    #: next trading day. Alpaca charges for EVERY calendar day the balance
    #: is carried, trading day or not (owner-confirmed from Alpaca's own
    #: docs, 2026-09-23): a normal weeknight is 1, a Friday is 3
    #: (Fri+Sat+Sun), a Friday before a Monday holiday is 4. `daily_usd`
    #: and `annual_usd` are deliberately unchanged by this — the per-day
    #: figure is still the per-day figure; `period_usd` is the total.
    days_charged: int = 1

    @property
    def period_usd(self) -> float:
        """What tonight's carry actually costs: `daily_usd x days_charged`."""
        return self.daily_usd * self.days_charged


#: Safety bound on the forward calendar search in
#: `days_charged_until_next_trading_day`. The longest real gap on the NYSE
#: calendar is a 4-day holiday weekend; anything beyond a week is a broken
#: calendar read, not a market closure, and degrades to 1.
MAX_CALENDAR_LOOKAHEAD_DAYS = 7


def days_charged_until_next_trading_day(
    is_trading_day: Callable[[date], bool],
    today: date,
    max_lookahead_days: int = MAX_CALENDAR_LOOKAHEAD_DAYS,
) -> int:
    """Calendar days tonight's debit is charged for before the next session.

    `(next trading day STRICTLY after today) - today`, in days. Weeknight
    -> 1; Friday -> 3; Friday before a Monday holiday -> 4. `is_trading_day`
    is the broker's calendar check (`AlpacaBroker.is_trading_day`), passed
    in so this stays pure and testable with a stub.

    NEVER raises and never returns less than 1: a calendar that cannot be
    read (an exception, or no trading day found within the lookahead bound)
    degrades to 1 — the flat per-day figure the desk showed before this
    existed — because a broker hiccup must not be able to break the alert.
    """
    try:
        for offset in range(1, max_lookahead_days + 1):
            candidate = today + timedelta(days=offset)
            if is_trading_day(candidate):
                return offset
    except Exception as exc:  # noqa: BLE001 — a nicety must never break the alert
        logger.warning(
            "trading-calendar lookahead failed; assuming 1 day charged: %s", exc,
        )
        return 1
    logger.warning(
        "no trading day found within %d days of %s; assuming 1 day charged",
        max_lookahead_days, today,
    )
    return 1


def overnight_debit_balance(end_of_day_cash: float | None) -> float:
    """The amount borrowed on margin, from the broker's own EOD cash figure.

    QAMC's existing cash-only gate (`src/risk/rules.py`'s `cash_only` hard
    block) already treats a negative broker `cash` balance as "on margin"
    — the same sign convention applies here once margin is enabled: cash
    below zero means the broker floated the difference, and that amount IS
    the debit balance. A non-negative cash balance means nothing was
    borrowed — this returns exactly `0.0`, never a fabricated debit.

    Deliberately takes ONLY an end-of-day cash figure. There is no
    intraday variant of this function — passing an intraday low here
    would be a misuse, not a feature, because Alpaca does not charge for
    intraday leverage at all. Callers must snapshot cash at/after the
    session that represents "what was actually carried overnight" (see
    `run_morning`'s pre-trade account snapshot, which reads whatever the
    broker floated across the prior close).

    Below `MARGIN_DEFICIT_FLOOR_USD` a deficit is treated as settlement /
    rounding noise, not a real debit balance — the same floor
    `_force_delever` and the PM/midday DE-LEVER prompts already use, so
    "is this account meaningfully on margin" answers consistently
    everywhere it's asked.
    """
    if end_of_day_cash is None:
        return 0.0
    deficit = -end_of_day_cash
    return deficit if deficit > MARGIN_DEFICIT_FLOOR_USD else 0.0


def estimate_daily_interest(debit_balance: float, rate_pct: float) -> float:
    """`(overnight debit balance x rate) / 360` — Alpaca's own formula.

    Zero in, zero out: a non-positive balance or rate produces exactly
    `0.0`, never a negative or fabricated figure.
    """
    if debit_balance <= 0 or rate_pct <= 0:
        return 0.0
    return debit_balance * (rate_pct / 100.0) / DAYS_PER_YEAR_ALPACA_CONVENTION


def build_estimate(
    debit_balance: float, rate_pct: float, days_charged: int = 1,
) -> MarginInterestEstimate | None:
    """The full estimate, or `None` when there is nothing to report.

    `None` — not a zero-valued object — is the "silent" contract: a zero
    (or noise-floor) debit balance must produce no charge and no alert
    line, which every caller implements by treating `None` as "say
    nothing" rather than by inspecting `daily_usd == 0`.
    """
    if debit_balance <= 0:
        return None
    daily = estimate_daily_interest(debit_balance, rate_pct)
    if daily <= 0:
        return None
    return MarginInterestEstimate(
        debit_balance=debit_balance,
        rate_pct=rate_pct,
        daily_usd=daily,
        # Same day-count convention both directions: the "annual" figure is
        # just what the daily rate compounds to over the same 360-day
        # cycle it was derived from (debit_balance * rate_pct / 100),
        # rather than mixing a 360-day daily accrual with a 365-day year.
        annual_usd=daily * DAYS_PER_YEAR_ALPACA_CONVENTION,
        days_charged=max(1, int(days_charged)),
    )


def format_alert_line(estimate: MarginInterestEstimate | None) -> str | None:
    """Plain-language morning-alert line, or `None` when there's nothing
    to report (silent on a zero/no debit balance — no noise policy).

    UNCHANGED, deliberately. The owner's 2026-09-18 "show it every day,
    even if it's zero" decision is implemented in `format_daily_line`
    below, NOT here: this formatter cannot tell "no debit balance" apart
    from "no rate configured" — `build_estimate` returns `None` for both —
    so making it speak on `None` would print a reassuring zero over a
    broken config. The always-speak policy belongs where the inputs are
    still distinguishable.
    """
    if estimate is None:
        return None
    line = (
        f"💳 margin interest: ${estimate.daily_usd:,.2f}/day "
        f"(~${estimate.annual_usd:,.0f}/yr) on ${estimate.debit_balance:,.0f} "
        f"carried overnight at {estimate.rate_pct:.2f}%"
    )
    if estimate.days_charged > 1:
        # Alpaca charges for every calendar day the balance is carried,
        # so a Friday's overnight is really three nights' worth. Said in
        # plain words, with the total, so the per-day figure above cannot
        # be read as tonight's bill.
        line += (
            f" — carried over the {_closure_name(estimate.days_charged)} "
            f"that's {estimate.days_charged} days "
            f"≈ ${estimate.period_usd:,.2f}"
        )
    return f"{line} — {ESTIMATE_LABEL}"


def _closure_name(days_charged: int) -> str:
    """Plain-words name for a multi-day carry: 3 -> weekend, 4 -> long
    weekend, anything else -> market closure (a midweek holiday)."""
    if days_charged == 3:
        return "weekend"
    if days_charged == 4:
        return "long weekend"
    return "market closure"


#: Rendered when the account's own cash figure could not be read at all.
#: Owner decision 2026-09-18 (below): degrading to silence is
#: indistinguishable from the tracker being dead, so a failed read says it
#: failed. Never a fabricated zero — "not available" and "$0.00" are
#: different claims about the world and must read differently.
UNAVAILABLE_LINE = (
    "💳 margin interest: not available — the account's cash balance "
    "could not be read this time"
)

#: Rendered when no usable interest rate is configured. Kept SEPARATE from
#: the zero line on purpose: `build_estimate` returns `None` both when
#: nothing was borrowed and when `rate_pct` is missing or zero, and
#: printing "nothing to pay" over a deleted or mis-keyed
#: `risk.margin_interest_rate_pct` would be a reassuring lie on exactly
#: the day the owner most needs to know the tracker is broken — the
#: inverse of what his "so I know it's still working" asked for.
RATE_UNAVAILABLE_LINE = (
    "💳 margin interest: not available — no borrowing rate is configured, "
    "so nothing can be worked out"
)


def _money(usd: float) -> str:
    """`$1,234.56` / `-$1,234.56` — sign outside the dollar mark, which is
    how the rest of the owner-facing lines in this desk render money."""
    return f"-${abs(usd):,.2f}" if usd < 0 else f"${usd:,.2f}"


def format_daily_line(
    end_of_day_cash: float | None, rate_pct: float | None,
    days_charged: int = 1,
) -> str:
    """ALWAYS exactly one owner-facing line. Never `None`, never silent.

    OWNER DECISION 2026-09-18, verbatim: "Yes, every day, even if it's
    zero, that way I know it's still working." This DELIBERATELY OVERRIDES
    the spec's original no-noise policy (§11.2: say nothing on a zero debit
    balance), which `format_alert_line` and `build_estimate` still
    implement above and which stays correct for their own contracts. The
    policy was wrong for the one reader it serves: to him an absent line
    and a dead tracker look identical, so the zero has to be said out loud
    — the zero IS the evidence the thing still runs.

    Takes the raw inputs rather than a built estimate precisely so it can
    keep apart the states `build_estimate` collapses into a single `None`:

      * cash not readable        -> `UNAVAILABLE_LINE`
      * no/zero rate configured  -> `RATE_UNAVAILABLE_LINE` (a FAULT, not a zero)
      * nothing borrowed         -> the zero line below
      * a real debit balance     -> `format_alert_line`, unchanged

    `days_charged` (default 1) is threaded through to the estimate so a
    Friday's line names the 3-day weekend charge; it has no effect on the
    zero and fault states, which are exactly as before.

    The zero line carries the MEASURED overnight cash figure rather than a
    bare "$0.00/day", for two reasons. First, a constant string is
    indistinguishable from a stuck one, so a zero that never changes would
    prove nothing — a cash balance that moves day to day does. Second, the
    desk ignores a debit below `MARGIN_DEFICIT_FLOOR_USD` as settlement
    noise, so a flat "nothing was borrowed" would be factually false on a
    99-cent deficit; showing the number instead of asserting the absence
    cannot be false at any balance.

    The zero line carries NO `ESTIMATE_LABEL`, on purpose: it reports the
    broker's own cash figure and the fact that nothing was borrowed, which
    is measured. The label belongs on the non-zero figure, which genuinely
    is a projection; pasting it onto a certain zero would blunt it exactly
    where it has to bite.
    """
    if end_of_day_cash is None:
        return UNAVAILABLE_LINE
    if rate_pct is None or rate_pct <= 0:
        return RATE_UNAVAILABLE_LINE
    debit_balance = overnight_debit_balance(end_of_day_cash)
    line = format_alert_line(build_estimate(debit_balance, rate_pct, days_charged))
    if line is not None:
        return line
    if end_of_day_cash < 0:
        # A deficit the desk treats as settlement noise, not borrowing.
        return (
            f"💳 margin interest: $0.00/day — overnight cash "
            f"{_money(end_of_day_cash)}, too small to charge on"
        )
    return (
        f"💳 margin interest: $0.00/day — overnight cash "
        f"{_money(end_of_day_cash)}, nothing borrowed"
    )


@dataclass(frozen=True)
class IntActivityComparison:
    """Result of checking the ESTIMATE against the broker's own truth.

    `observed_usd` is `None` when the broker reported no `INT` activity at
    all for the relevant date — that is itself informative (it is evidence,
    not proof, that paper trading may not simulate this charge) and is
    surfaced as `charge_confirmed=False` with a note that says exactly
    that, rather than being silently treated as "confirmed zero".
    """

    estimate_usd: float
    observed_usd: float | None
    charge_confirmed: bool
    note: str


def compare_estimate_to_broker_activity(
    estimate: MarginInterestEstimate | None,
    activities: list[dict],
) -> IntActivityComparison | None:
    """Settle, empirically, whether paper trading charges margin interest.

    `activities` is the broker's own `INT`-type account activity records
    for the relevant overnight period (see
    `AlpacaBroker.get_margin_interest_activities`), already filtered by the
    caller to the date(s) that matter. This function does not know or care
    where they came from — a test can hand it a stubbed list directly.

    Returns `None` only when there is no estimate to compare against (no
    debit balance was carried, so there is nothing to settle). Does NOT
    pre-judge whether paper simulates the charge: an empty `activities`
    list is reported as "not confirmed", never assumed to mean "confirmed
    absent" or silently ignored.
    """
    if estimate is None:
        return None
    if not activities:
        return IntActivityComparison(
            estimate_usd=estimate.daily_usd,
            observed_usd=None,
            charge_confirmed=False,
            note=(
                "no INT activity on the account for a night a debit balance "
                "was carried — paper trading may not simulate margin "
                "interest, but one clear night is not proof; keep watching"
            ),
        )
    # Alpaca's ledger convention: an INT activity's net_amount is a charge
    # against cash, i.e. negative. Sum first (a single overnight period can
    # legitimately post more than one INT row), then flip the sign so a
    # real charge reads as a positive dollar amount here.
    observed = -sum(float(a.get("net_amount", 0.0) or 0.0) for a in activities)
    if observed > 0:
        note = (
            f"broker confirmed a margin interest charge of ${observed:,.2f} "
            f"(estimate was ${estimate.daily_usd:,.2f}) — paper DOES appear "
            "to simulate this cost"
        )
    else:
        note = (
            f"INT activity present but net ${observed:,.2f} — not a "
            "confirmed charge"
        )
    return IntActivityComparison(
        estimate_usd=estimate.daily_usd,
        observed_usd=observed,
        charge_confirmed=observed > 0,
        note=note,
    )


# ---------------------------------------------------------------------------
# Cumulative view — owner ask, 2026-09-24.
#
# Replaces the per-day/per-year figures above (still computed, still used
# to derive `days_charged`/the broker-check comparison, but no longer the
# owner-facing headline) with what he actually asked to see: this week's
# running total, the current month, each of up to five more recent months
# that had any interest (oldest cut at `MAX_LOOKBACK_MONTHS` total, zero
# months skipped so the list is never padded with months that cost
# nothing), and an all-time total.
#
# PREFERENCE ORDER, per-bucket: a broker-CONFIRMED `INT` activity charge is
# always the truth when one exists (`bucket_broker_activities`) — Alpaca
# keeps that ledger for the life of the account, so "all-time" in that path
# really can mean the whole account history, no local storage required.
# Only when the broker has NEVER posted a real INT row (paper trading's
# documented-empty behaviour, re-confirmed 2026-09-24 on a $6,114.51
# overnight debit — see the module docstring's first measurement) does this
# fall back to summing OUR OWN persisted daily ESTIMATE rows
# (`margin_interest_daily`, `src/storage/db.py`), which can only ever cover
# days since this tracker started writing them. `all_time_since` on the
# result names exactly which case applies and where its own history
# actually starts, so "all-time" can never be silently read as "since the
# desk went on margin" when the two differ — see
# `docs/qamc-margin-interest-cumulative-data-gap.md`-equivalent note in the
# PR description for why a true from-inception ESTIMATE total is not
# currently possible (no historical debit balance was ever persisted before
# this change).
#: Total months of history the cumulative view looks back across, current
#: month included — the owner's own ask ("maximum of six months ago").
MAX_LOOKBACK_MONTHS = 6


@dataclass(frozen=True)
class CumulativeMarginInterest:
    """The owner-facing cumulative margin-interest view.

    `prior_months` is oldest-excluded-if-zero, newest-first, each item
    `{"label": "August 2026", "usd": 12.34}` — `label` is derived from the
    date (`%B %Y`), never a hardcoded month name. `is_estimate` is `True`
    unless every dollar counted is a broker-confirmed `INT` charge; the
    cockpit/Telegram render a single small "est." marker off this flag
    rather than the old paragraph-length caveat.
    """

    this_week_usd: float
    current_month_usd: float
    current_month_label: str
    prior_months: list[dict]
    all_time_usd: float
    all_time_since: str
    is_estimate: bool
    source: str  # "broker_actual" | "estimate" | "no_data"


def _week_start(today: date) -> date:
    """Monday of `today`'s week."""
    return today - timedelta(days=today.weekday())


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _prior_month_start(month_start: date) -> date:
    """The 1st of the calendar month before `month_start` (itself a 1st)."""
    last_day_of_prev_month = month_start - timedelta(days=1)
    return last_day_of_prev_month.replace(day=1)


def _bucket_dated_amounts(
    dated_amounts: list[tuple[date, float]],
    today: date,
    is_estimate: bool,
    source: str,
) -> CumulativeMarginInterest:
    """Shared bucketing over `[(date, charge_usd), ...]` — used by both the
    broker-actual and the estimate path so the two can never drift apart on
    what "this week" or "current month" means.

    Empty input produces an honest all-zero/no-data result rather than
    raising — the caller (broker path vs. estimate path) decides what an
    empty list MEANS (no charge ever confirmed vs. nothing persisted yet).
    """
    if not dated_amounts:
        return CumulativeMarginInterest(
            this_week_usd=0.0, current_month_usd=0.0,
            current_month_label=today.strftime("%B %Y"),
            prior_months=[], all_time_usd=0.0,
            all_time_since=today.isoformat(),
            is_estimate=is_estimate, source="no_data",
        )
    week_start = _week_start(today)
    month_start = _month_start(today)
    this_week = sum(amt for d, amt in dated_amounts if week_start <= d <= today)
    current_month = sum(amt for d, amt in dated_amounts if month_start <= d <= today)

    prior_months: list[dict] = []
    cursor = month_start
    for _ in range(MAX_LOOKBACK_MONTHS - 1):
        prev_start = _prior_month_start(cursor)
        prev_end = cursor - timedelta(days=1)
        total = sum(amt for d, amt in dated_amounts if prev_start <= d <= prev_end)
        if total > 0:
            prior_months.append({
                "label": prev_start.strftime("%B %Y"),
                "usd": total,
            })
        cursor = prev_start

    all_time = sum(amt for _, amt in dated_amounts)
    all_time_since = min(d for d, _ in dated_amounts).isoformat()
    return CumulativeMarginInterest(
        this_week_usd=this_week,
        current_month_usd=current_month,
        current_month_label=today.strftime("%B %Y"),
        prior_months=prior_months,
        all_time_usd=all_time,
        all_time_since=all_time_since,
        is_estimate=is_estimate,
        source=source,
    )


def bucket_broker_activities(
    activities: list[dict], today: date,
) -> CumulativeMarginInterest | None:
    """Bucket the broker's own FULL `INT` activity history (no `after`
    filter — see `AlpacaBroker.get_margin_interest_activities`) into the
    cumulative view. `net_amount` is Alpaca's ledger convention: negative
    is a charge against cash, so it is flipped here the same way
    `compare_estimate_to_broker_activity` does.

    Returns `None` — not a zero-valued result — when the broker has never
    posted a single `INT` row, which the caller must read as "no broker
    confirmation exists yet; use the estimate instead", never as "broker
    confirms zero interest was ever charged".
    """
    parsed: list[tuple[date, float]] = []
    for a in activities:
        raw_date = a.get("date")
        if not raw_date:
            continue
        try:
            d = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            continue
        parsed.append((d, -float(a.get("net_amount", 0.0) or 0.0)))
    if not parsed:
        return None
    return _bucket_dated_amounts(parsed, today, is_estimate=False, source="broker_actual")


def bucket_estimate_rows(rows: list[dict], today: date) -> CumulativeMarginInterest:
    """Bucket our own persisted daily-accrual ESTIMATE rows (each
    `{"date": "YYYY-MM-DD", "period_usd": float}`, from the
    `margin_interest_daily` table) into the cumulative view.

    Unlike `bucket_broker_activities`, an empty `rows` list is a real,
    reportable state (`source="no_data"`) rather than `None` — there is no
    further fallback below this one.
    """
    parsed = [
        (date.fromisoformat(str(r["date"])[:10]), float(r.get("period_usd", 0.0) or 0.0))
        for r in rows
        if r.get("date")
    ]
    return _bucket_dated_amounts(parsed, today, is_estimate=True, source="estimate")


def compute_cumulative_margin_interest(
    broker_activities: list[dict],
    estimate_rows: list[dict],
    today: date,
) -> CumulativeMarginInterest:
    """The one function callers (`broker_reads.py`, `notifier.py`) use:
    prefer broker-confirmed `INT` history; fall back to our own persisted
    ESTIMATE rows only when the broker has never posted a real charge.
    """
    broker_view = bucket_broker_activities(broker_activities, today)
    if broker_view is not None:
        return broker_view
    return bucket_estimate_rows(estimate_rows, today)


def format_cumulative_line(cumulative: CumulativeMarginInterest) -> str:
    """One or two short owner-facing Telegram lines — no per-day figure, no
    caveat paragraph. `(est.)` is the entire estimate marker; the broker-
    confirmed path carries none.

    `source == "no_data"` renders a single honest line saying nothing has
    been measured yet, rather than a fabricated `$0.00`.
    """
    if cumulative.source == "no_data":
        return "💳 margin interest: no data yet — tracking starts today"

    def _tag(usd: float) -> str:
        # A certain zero is not an estimate at any rate — same rule
        # `format_daily_line` already applies to the per-day figure. Only a
        # genuinely nonzero, unconfirmed total carries the marker.
        return " (est.)" if cumulative.is_estimate and usd != 0 else ""

    line = (
        f"💳 margin interest — this week {_money(cumulative.this_week_usd)}, "
        f"{cumulative.current_month_label} {_money(cumulative.current_month_usd)}"
        f"{_tag(cumulative.current_month_usd)}, "
        f"all-time {_money(cumulative.all_time_usd)}{_tag(cumulative.all_time_usd)} "
        f"(since {cumulative.all_time_since})"
    )
    return line


# ---------------------------------------------------------------------------
# Historical backfill — owner ask, 2026-09-24.
#
# `margin_interest_daily` only exists from #637 forward: no historical
# daily debit balance was ever persisted, so "all-time" before that PR
# merged reads as "since the tracker started", not "since the desk went on
# margin". The owner wants that gap filled with a BEST-EFFORT ESTIMATE —
# his own words: accuracy is not required, it's paper money, get close and
# move on.
#
# There is no historical positions or cash endpoint on Alpaca — confirmed
# 2026-09-24: `portfolio_history` (`AlpacaBroker.get_full_portfolio_history`)
# gives only a total-equity time series (`timestamp`/`equity`/`profit_loss`
# — no cash, no market value, no positions breakdown, per the SDK's own
# `PortfolioHistory` model), and there is no endpoint at all for a
# historical positions snapshot. What Alpaca DOES keep for the life of the
# account is the account-ACTIVITIES ledger (`AlpacaBroker.
# get_all_account_activities`) — every deposit, fill, fee and withholding,
# each with a signed cash effect. Replaying that ledger from $0 reconstructs
# an approximate daily cash balance, and a negative cash balance IS the
# debit balance (`overnight_debit_balance`, unchanged). Checked against the
# account's own live `cash` figure on 2026-09-24: replaying the FULL
# activity history reproduces the broker's reported cash to within $0.04 on
# a ~$6,100 balance — plenty close for a number that already carries
# `ESTIMATE_LABEL`.
def _fill_cash_delta(activity: dict) -> float:
    """Cash effect of one `FILL` activity: `-price*qty` on a buy, `+price*
    qty` on a sell. Alpaca's `FILL` activities carry no `net_amount` field
    at all — this is the only way to get their cash effect. Any
    unparsable price/qty degrades to `0.0` rather than raising: a single
    bad ledger row must not break the whole reconstruction."""
    try:
        price = float(activity.get("price") or 0.0)
        qty = float(activity.get("qty") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    sign = -1.0 if activity.get("side") == "buy" else 1.0
    return sign * price * qty


def activity_cash_delta(activity: dict) -> float:
    """Signed cash effect of one broker account-activity record.

    `FILL` rows are computed from `price`/`qty`/`side` (see
    `_fill_cash_delta`); every other type Alpaca posts (`JNLC` deposits/
    withdrawals, `FEE`, `WH` withholding, `CFEE`, `DIV`, `INT`, ...)
    already carries a signed `net_amount` that IS the cash effect, used
    verbatim. An unparsable or missing `net_amount` degrades to `0.0`.
    """
    if activity.get("activity_type") == "FILL":
        return _fill_cash_delta(activity)
    try:
        return float(activity.get("net_amount") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def activity_effective_date(activity: dict) -> date | None:
    """The calendar date an activity's cash effect belongs to.

    Non-`FILL` activities carry Alpaca's own settlement `date` field,
    used directly. `FILL` activities carry no top-level `date` — only
    `transaction_time` — so the trade date is read off that instead.
    `None` when neither is present or parseable; callers must skip such a
    row rather than guess which day it belongs to.
    """
    raw = activity.get("date") or activity.get("transaction_time")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def reconstruct_daily_cash_balances(activities: list[dict]) -> dict[date, float]:
    """Best-effort end-of-day cash balance, replayed from the broker's own
    account-activity ledger. See the module-level note above this
    function for why this is the reconstruction method (no historical
    cash/positions endpoint exists) and how closely it was checked
    against a live account.

    Starts from `$0.0` (before the account's first deposit, cash is
    definitionally zero — there is nothing to reconstruct before it) and
    accumulates each activity's signed cash effect in date order,
    ties broken by activity id for a stable replay order.

    Returns an entry ONLY for dates something actually posted — a quiet
    day (no fills, no deposits) has no entry here by design. Callers
    needing "the balance as of any given day" must carry forward from the
    nearest earlier entry themselves (`nearest_prior_balance`) rather than
    this function inventing a flat row for every calendar day.
    """
    dated = [
        (d, a) for a in activities if (d := activity_effective_date(a)) is not None
    ]
    dated.sort(key=lambda pair: (pair[0], pair[1].get("id", "")))
    running = 0.0
    by_date: dict[date, float] = {}
    for d, a in dated:
        running += activity_cash_delta(a)
        by_date[d] = running
    return by_date


def nearest_prior_balance(by_date: dict[date, float], before: date) -> float | None:
    """The most recent reconstructed EOD cash balance strictly before
    `before` — carry-forward interpolation for a day the ledger posted
    nothing on. `None` when no earlier balance exists at all (before the
    account's first activity), which the caller must read as "nothing to
    report" rather than fabricate a zero for.
    """
    candidates = [d for d in by_date if d < before]
    if not candidates:
        return None
    return by_date[max(candidates)]


@dataclass(frozen=True)
class BackfilledDayEstimate:
    """One historical day's reconstructed margin-interest row — the same
    fields `margin_interest_daily` stores. `source` is always
    `"estimate_backfill"`, distinct from the live tracker's `"estimate"`/
    `"broker_actual"`, so a caller writing these can tell a reconstructed
    row apart from one the live tracker actually measured and never let
    the (less accurate) reconstruction overwrite it.
    """

    trading_day: date
    debit_balance: float
    rate_pct: float
    daily_usd: float
    days_charged: int
    period_usd: float
    source: str = "estimate_backfill"


def backfill_daily_estimates(
    trading_days: list[date],
    activities: list[dict],
    rate_pct: float,
    is_trading_day: Callable[[date], bool],
) -> list[BackfilledDayEstimate]:
    """The full historical reconstruction: one row per trading day in
    `trading_days`.

    The debit balance attributed to day `D` is the reconstructed cash
    balance carried INTO `D`'s open — the nearest known EOD balance
    strictly before `D` (`nearest_prior_balance`), never `D`'s own
    same-day activity. This matches the live tracker's own convention
    (`notifier._margin_interest_lines`: a morning read reflects what was
    carried across last night's close, before today's trading touches
    cash) and is why a day's row is dated `D`, not the night before it.

    A day with no earlier activity at all (before the account's first
    deposit) gets an honest `$0` row, not a skipped one — there was
    nothing to borrow against yet, which is the true state.

    `days_charged` reuses `days_charged_until_next_trading_day` exactly
    as the live tracker does, so a backfilled Friday/holiday row still
    carries the same 3-4 day multiplier a live-run row would have.

    Never raises on a single bad day: `is_trading_day` and the debit
    formula are already exception-safe (see their own docstrings); this
    function adds no new failure surface of its own.
    """
    by_date = reconstruct_daily_cash_balances(activities)
    rows: list[BackfilledDayEstimate] = []
    for d in sorted(trading_days):
        prior_cash = nearest_prior_balance(by_date, d)
        debit_balance = (
            overnight_debit_balance(prior_cash) if prior_cash is not None else 0.0
        )
        days_charged = days_charged_until_next_trading_day(is_trading_day, d)
        estimate = build_estimate(debit_balance, rate_pct, days_charged)
        rows.append(
            BackfilledDayEstimate(
                trading_day=d,
                debit_balance=debit_balance,
                rate_pct=rate_pct,
                daily_usd=estimate.daily_usd if estimate is not None else 0.0,
                days_charged=days_charged,
                period_usd=estimate.period_usd if estimate is not None else 0.0,
            )
        )
    return rows
