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

**Today's actual state.** `allow_margin` is `False`, and the account has
never actually carried a negative cash balance, so `overnight_debit_balance()`
is `0.0` in production right now. That is NOT a guarantee `cash_only`
provides in general, though — it hard-blocks a plain BUY from taking cash
negative, but a COVER is deliberately exempt (D10, `src/risk/rules.py`;
`src/agents/portfolio_manager.py`'s DE-LEVER MANDATE already treats
"cash negative with `allow_margin` False" as a real state a session can
reach). Callers of this module must therefore key off `cash` itself, never
off `allow_margin`, to decide whether there is anything to report — see
`src/api/broker_reads.py::read_margin_interest` and
`src/notifier.py::_margin_interest_lines`, both of which read `cash`
unconditionally for exactly this reason. Every function below is written
to be silent and side-effect-free at an actual zero (or non-negative)
balance, per the spec's requirement that this tracker not add noise to a
book that owes nothing — 11.2's gross-exposure cap and de-levering ladder,
built separately, are what will keep balances bounded once margin is
deliberately turned on.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.risk.constants import MARGIN_DEFICIT_FLOOR_USD

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
    debit_balance: float, rate_pct: float,
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
    )


def format_alert_line(estimate: MarginInterestEstimate | None) -> str | None:
    """Plain-language morning-alert line, or `None` when there's nothing
    to report (silent on a zero/no debit balance — no noise policy)."""
    if estimate is None:
        return None
    return (
        f"💳 margin interest: ${estimate.daily_usd:,.2f}/day "
        f"(~${estimate.annual_usd:,.0f}/yr) on ${estimate.debit_balance:,.0f} "
        f"carried overnight at {estimate.rate_pct:.2f}% — {ESTIMATE_LABEL}"
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
