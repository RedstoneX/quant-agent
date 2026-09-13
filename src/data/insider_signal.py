"""Routine-versus-opportunistic classification for SEC Form 4 transactions.

Evidence basis: ``docs/RESEARCH_FINDINGS.md`` section 1, which follows Cohen,
Malloy & Pomorski, *Decoding Inside Information* (JF 2012). More than half of
Form 4 activity is **routine** — the same insider trading in the same calendar
month year after year — and routine trades carry no predictive power. Removing
them is what leaves the ~82bps/month opportunistic residual.

Three deliberate departures from the folk version of this filter, all of them
taken from ``RESEARCH_FINDINGS.md`` rather than from intuition:

1. **A 10b5-1 checkbox is NOT on its own a routine marker.** The research
   document is explicit: "10b5-1 plans are not a clean noise filter. For
   high-value sales, planned and discretionary transactions show similar
   opportunism, and the 2022 SEC reform did not reduce abnormal returns on
   insider selling." So the flag only ever *supports* a routine label for a
   disposition that is already proportionally immaterial; a large sale is
   never demoted for being planned. The flag is still recorded in the reason
   text so an operator can see it was considered.

2. **Compensation codes need no branch here.** ``SECForm4Provider`` already
   restricts parsing to non-derivative ``P``/``S`` rows, so option exercises
   (``M``), awards (``A``), tax withholding (``F``), gifts (``G``) and
   dispositions to the issuer (``D``/``X``) never reach this module.
   ``_non_open_market`` remains as a contract guard, not as a live filter.

3. **Sell materiality is proportional, not absolute.** "On the sell side only
   large sales that are *also* large relative to the insider's total position
   predict negative returns." The primary source behind that line is Scott &
   Xu, *Some Insider Sales Are Positive Signals* (Financial Analysts Journal
   60(3), 2004) — 512,133 combined transactions, 80,742 company-quarters,
   1987-2002 — which measures "shares traded as a percentage of shares
   owned" in three bands: under 10%, 10-50% and over 50% of the holding.
   Their size- and B/P-adjusted result: sales over 100,000 shares earn
   -0.81% quarterly excess return only in the over-50% band (-0.06% and
   +0.08% in the two lower bands, insignificant), while sales under 100,000
   shares in the under-10% band earn a *positive* +0.68%. The same variable
   also differentiates purchases: +0.38% / +1.06% / +1.42% across the three
   bands, and initial purchases (no prior holding, so no ratio exists) earn
   an insignificant +0.10%.
   <https://rpc.cfainstitute.org/research/financial-analysts-journal/2004/some-insider-sales-are-positive-signals>

   Two things follow, and both are honoured here. The materiality boundary
   is the paper's own lowest band edge (10%), not a number of this desk's
   invention. And the ratio itself is *reported* on every row, purchases
   included, rather than being turned into an extra admission cutoff: the
   paper measures net shares traded per stock-quarter against holdings
   aggregated across insiders over a six-month formation window, which is
   not the same object as one Form 4 row, so its band returns do not
   transfer to a per-transaction gate.

The classifier is pure Python, deterministic, and makes no model call.

Every numeric threshold below (materiality fraction, calendar-routine years,
cadence window) is an operator-tunable setting, not a fixed number in this
file — the owner rejects hardcoded thresholds on sight, and a threshold able
to change classification output is exactly the kind of number that belongs
in config. Defaults live on `SmartMoneyConfig` in `src/config.py` (fields
prefixed `insider_`) and arrive here as an `InsiderSignalThresholds`,
constructed once by the caller (`SECForm4Provider.__init__`) from the loaded
`AppConfig`, mirroring how `SECForm4Provider` already receives every other
smart-money threshold as an explicit constructor argument rather than
reading config itself. Passing `thresholds=None` (the default, and every
call site in this module's own tests) falls back to
`InsiderSignalThresholds()`, whose field defaults equal the values this
module used before 2026-08-28 — so unconfigured behaviour is unchanged.
"""
from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

OPPORTUNISTIC = "opportunistic"
ROUTINE = "routine"
INDETERMINATE = "indeterminate"

_WEIGHTS = {OPPORTUNISTIC: 1.0, INDETERMINATE: 0.5, ROUTINE: 0.0}

# Roles that carry published signal. CFO purchases beat CEO purchases; a
# reporting owner with no officer/director/10% standing is a weaker source.
_INSIDE_ROLE_MARKERS = ("officer", "director", "tenpercentowner")


@dataclass(frozen=True)
class InsiderSignalThresholds:
    """The classifier's operator-tunable numbers, in one place.

    Field defaults are the values this module hardcoded before 2026-08-28;
    changing behaviour now requires an explicit `SmartMoneyConfig` override
    (`config/settings.yaml` -> `smart_money.insider_*`), not a code edit. See
    the corresponding fields on `SmartMoneyConfig` in `src/config.py` for the
    evidence basis behind each default — kept in sync by
    `SECForm4Provider.__init__`, the only production call site that builds
    one of these from loaded config.
    """

    calendar_routine_years: int = 3
    min_cadence_trades: int = 3
    cadence_min_mean_gap_days: float = 20.0
    cadence_max_mean_gap_days: float = 120.0
    cadence_max_gap_dispersion: float = 0.25
    min_material_sell_fraction: float = 0.10


_DEFAULT_THRESHOLDS = InsiderSignalThresholds()


@dataclass(frozen=True)
class InsiderPriorTrade:
    """One earlier open-market trade by the same insider in the same issuer."""

    transaction_date: date
    direction: str


class InsiderHistory:
    """Per-(insider, issuer) prior open-market trades.

    Intentionally a plain in-memory index rather than a database read: the
    classifier must stay free, deterministic and unit-testable.
    """

    def __init__(
        self,
        trades: Mapping[tuple[str, str], Sequence[InsiderPriorTrade]] | None = None,
    ):
        self._trades: dict[tuple[str, str], list[InsiderPriorTrade]] = {}
        for key, values in (trades or {}).items():
            actor_cik, symbol = key
            normalized = (str(actor_cik or "").strip(), str(symbol or "").strip().upper())
            if not normalized[0] or not normalized[1]:
                continue
            self._trades[normalized] = sorted(
                values, key=lambda item: item.transaction_date,
            )

    def __bool__(self) -> bool:
        return bool(self._trades)

    def as_mapping(self) -> dict[tuple[str, str], list[InsiderPriorTrade]]:
        """Copy of the index, for callers merging in another source."""
        return {key: list(values) for key, values in self._trades.items()}

    def prior_trades(
        self,
        actor_cik: str,
        symbol: str,
        *,
        direction: str,
        before: date,
    ) -> list[InsiderPriorTrade]:
        key = (str(actor_cik or "").strip(), str(symbol or "").strip().upper())
        return [
            trade for trade in self._trades.get(key, ())
            if trade.direction == direction and trade.transaction_date < before
        ]

    @classmethod
    def from_observations(cls, observations) -> "InsiderHistory":
        """Build an index from anything exposing the Form 4 row attributes."""
        grouped: dict[tuple[str, str], list[InsiderPriorTrade]] = {}
        for row in observations or []:
            actor_cik = str(getattr(row, "actor_cik", "") or "").strip()
            symbol = str(getattr(row, "symbol", "") or "").strip().upper()
            direction = str(getattr(row, "direction", "") or "").strip()
            transaction_date = getattr(row, "transaction_date", None)
            if not actor_cik or not symbol or not isinstance(transaction_date, date):
                continue
            if direction not in {"buy", "sell"}:
                continue
            grouped.setdefault((actor_cik, symbol), []).append(
                InsiderPriorTrade(transaction_date=transaction_date, direction=direction)
            )
        return cls(grouped)


@dataclass(frozen=True)
class InsiderSignalClass:
    """A label, the machine-readable rule that produced it, and the why."""

    label: str
    reason: str
    detail: str
    weight: float = field(default=1.0)

    @classmethod
    def of(cls, label: str, reason: str, detail: str) -> "InsiderSignalClass":
        return cls(label=label, reason=reason, detail=detail, weight=_WEIGHTS[label])


# Scott & Xu (FAJ 2004) report their percentage-of-shares-owned result in
# exactly these three bands. They are the paper's own column boundaries, so
# they are module constants rather than operator-tunable settings: moving
# them would silently detach the band label from the citation it exists to
# carry. Nothing downstream gates on them — see BANDS_ARE_REPORTING_ONLY.
_BAND_LOW = 0.10
_BAND_HIGH = 0.50

# The bands label a row; they never admit or reject one. The only relative
# number that changes a classification is
# ``InsiderSignalThresholds.min_material_sell_fraction``.
BANDS_ARE_REPORTING_ONLY = True

BAND_UNDER_LOW = "under_10pct"
BAND_MID = "10_to_50pct"
BAND_OVER_HIGH = "over_50pct"
BAND_NO_PRIOR_HOLDING = "no_prior_holding"
BAND_UNKNOWN = ""


def _sell_fraction(shares: float | None, post_shares: float | None) -> float | None:
    """Fraction of the insider's pre-transaction holding that was disposed."""
    if shares is None or post_shares is None or shares <= 0:
        return None
    pre_shares = shares + post_shares
    if pre_shares <= 0:
        return None
    return shares / pre_shares


def _buy_fraction(shares: float | None, post_shares: float | None) -> float | None:
    """Purchase size as a fraction of the holding the insider already had.

    Scott & Xu's purchase measure is "net insider purchases as a percentage
    of shares already owned", so the denominator is the *pre*-transaction
    holding — ``post - shares`` for an acquisition. A non-positive
    denominator means the insider held nothing beforehand; that is the
    paper's separately-reported "initial purchase" case, which has no ratio,
    so ``None`` is returned and the band says so rather than pretending the
    fraction is infinite.
    """
    if shares is None or post_shares is None or shares <= 0:
        return None
    pre_shares = post_shares - shares
    if pre_shares <= 0:
        return None
    return shares / pre_shares


def _band(fraction: float | None, *, no_prior_holding: bool) -> str:
    if fraction is None:
        return BAND_NO_PRIOR_HOLDING if no_prior_holding else BAND_UNKNOWN
    if fraction < _BAND_LOW:
        return BAND_UNDER_LOW
    if fraction <= _BAND_HIGH:
        return BAND_MID
    return BAND_OVER_HIGH


def holdings_fraction(observation) -> tuple[float | None, str]:
    """Trade size relative to the insider's own holding, plus its band.

    Reported for both directions and for every row, independently of which
    classification rule fired: the ratio is evidence the seat should see even
    when the label was decided by something else (a calendar-routine sale
    still has a size relative to the position). ``(None, "")`` means the
    filing did not carry enough to compute one.

    Sells use the pre-transaction holding ``shares + post_transaction_shares``
    as the denominator; buys use ``post_transaction_shares - shares``. Both
    reconstruct the same quantity — what the insider held before acting —
    from the two numbers SEC Form 4 always reports.
    """
    if str(getattr(observation, "stream", "") or "") != "insider":
        return None, BAND_UNKNOWN
    direction = str(getattr(observation, "direction", "") or "")
    shares = getattr(observation, "shares", None)
    post_shares = getattr(observation, "post_transaction_shares", None)
    if direction == "sell":
        fraction = _sell_fraction(shares, post_shares)
        return fraction, _band(fraction, no_prior_holding=False)
    if direction == "buy":
        fraction = _buy_fraction(shares, post_shares)
        no_prior = (
            fraction is None
            and shares is not None
            and post_shares is not None
            and shares > 0
            and post_shares - shares <= 0
        )
        return fraction, _band(fraction, no_prior_holding=no_prior)
    return None, BAND_UNKNOWN


def _has_inside_role(roles) -> bool:
    for role in roles or ():
        collapsed = str(role or "").strip().lower().replace(" ", "").replace("_", "")
        if any(marker in collapsed for marker in _INSIDE_ROLE_MARKERS):
            return True
    return False


def _calendar_routine_years(
    prior: Sequence[InsiderPriorTrade],
    transaction_date: date,
    thresholds: InsiderSignalThresholds,
) -> int:
    """Consecutive preceding years with a trade in the same calendar month."""
    months = {(trade.transaction_date.year, trade.transaction_date.month) for trade in prior}
    streak = 0
    for offset in range(1, thresholds.calendar_routine_years + 1):
        if (transaction_date.year - offset, transaction_date.month) not in months:
            break
        streak += 1
    return streak


def _cadence_gap_stats(
    prior: Sequence[InsiderPriorTrade],
    transaction_date: date,
    thresholds: InsiderSignalThresholds,
) -> tuple[float, float] | None:
    """Mean gap and coefficient of variation across the trade series."""
    dates = sorted({trade.transaction_date for trade in prior} | {transaction_date})
    if len(dates) < thresholds.min_cadence_trades + 1:
        return None
    gaps = [
        float((later - earlier).days)
        for earlier, later in zip(dates, dates[1:])
        if (later - earlier).days > 0
    ]
    if len(gaps) < thresholds.min_cadence_trades:
        return None
    mean_gap = statistics.fmean(gaps)
    if mean_gap <= 0:
        return None
    dispersion = statistics.pstdev(gaps) / mean_gap
    return mean_gap, dispersion


def classify_transaction(
    observation,
    history: InsiderHistory | None = None,
    thresholds: InsiderSignalThresholds | None = None,
) -> InsiderSignalClass:
    """Label one Form 4 row routine or opportunistic, with the reason.

    Rules are evaluated in precedence order and the first match wins, so the
    reason on the result is always the single rule that decided it.

    ``thresholds`` carries every operator-tunable number this classifier
    uses; ``None`` (the default) falls back to ``InsiderSignalThresholds()``,
    whose field defaults are this module's pre-2026-08-28 hardcoded values.
    """
    thresholds = thresholds or _DEFAULT_THRESHOLDS
    if str(getattr(observation, "stream", "") or "") != "insider":
        return InsiderSignalClass.of(
            INDETERMINATE, "not_form4",
            "Not an SEC Form 4 row; the routine test does not apply.",
        )

    code = str(getattr(observation, "transaction_code", "") or "").upper()
    direction = str(getattr(observation, "direction", "") or "")
    if code not in {"P", "S"}:
        return InsiderSignalClass.of(
            ROUTINE, "non_open_market_code",
            f"Transaction code {code or 'missing'!r} is not an open-market "
            "purchase or sale (grants, option exercises, tax withholding and "
            "gifts carry no directional signal).",
        )

    shares = getattr(observation, "shares", None)
    price = getattr(observation, "price_per_share", None)
    value = getattr(observation, "transaction_value_usd", None)
    if value is None or shares is None:
        return InsiderSignalClass.of(
            INDETERMINATE, "incomplete_amounts",
            "Filing omits share count or transaction value; routine status "
            "cannot be established.",
        )
    if price is not None and price <= 0:
        return InsiderSignalClass.of(
            ROUTINE, "zero_price_transaction",
            "Reported at a zero price, so no capital was risked or realised "
            "at market.",
        )

    transaction_date = getattr(observation, "transaction_date", None)
    actor_cik = str(getattr(observation, "actor_cik", "") or "").strip()
    symbol = str(getattr(observation, "symbol", "") or "").strip().upper()
    prior: list[InsiderPriorTrade] = []
    if history and isinstance(transaction_date, date) and actor_cik and symbol:
        prior = history.prior_trades(
            actor_cik, symbol, direction=direction, before=transaction_date,
        )

    if prior and isinstance(transaction_date, date):
        streak = _calendar_routine_years(prior, transaction_date, thresholds)
        if streak >= thresholds.calendar_routine_years:
            return InsiderSignalClass.of(
                ROUTINE, "calendar_routine",
                f"Same insider traded {symbol} in {transaction_date:%B} in each "
                f"of the {streak} preceding years — a routine trader under "
                "Cohen/Malloy/Pomorski, which carries no predictive power.",
            )
        stats = _cadence_gap_stats(prior, transaction_date, thresholds)
        if stats is not None:
            mean_gap, dispersion = stats
            if (
                thresholds.cadence_min_mean_gap_days
                <= mean_gap
                <= thresholds.cadence_max_mean_gap_days
                and dispersion <= thresholds.cadence_max_gap_dispersion
            ):
                return InsiderSignalClass.of(
                    ROUTINE, "recurring_cadence",
                    f"{len(prior) + 1} {direction} transactions spaced every "
                    f"~{mean_gap:.0f} days with {dispersion:.0%} variation — a "
                    "scheduled programme, not a discretionary decision.",
                )

    is_10b5_1 = getattr(observation, "is_10b5_1", None)
    if direction == "sell":
        fraction = _sell_fraction(shares, getattr(observation, "post_transaction_shares", None))
        if fraction is None:
            return InsiderSignalClass.of(
                INDETERMINATE, "unknown_holding",
                "Post-transaction holding is missing, so the sale cannot be "
                "sized against the insider's position.",
            )
        if fraction < thresholds.min_material_sell_fraction:
            if is_10b5_1:
                return InsiderSignalClass.of(
                    ROUTINE, "planned_small_disposition",
                    f"Sold {fraction:.1%} of the holding under a pre-arranged "
                    "10b5-1 plan — proportionally immaterial and scheduled.",
                )
            return InsiderSignalClass.of(
                ROUTINE, "immaterial_stake_sale",
                f"Sold {fraction:.1%} of the holding; Scott & Xu find sales "
                "below 10% of shares owned do not predict negative returns "
                "(small ones predict positive returns).",
            )
        planned = (
            " The 10b5-1 flag is set but is deliberately not treated as a "
            "noise marker: planned and discretionary high-value sales show "
            "similar opportunism."
            if is_10b5_1 else ""
        )
        return InsiderSignalClass.of(
            OPPORTUNISTIC, "material_stake_sale",
            f"Sold {fraction:.1%} of the holding — large relative to the "
            f"insider's own position.{planned}",
        )

    role_note = (
        "" if _has_inside_role(getattr(observation, "actor_roles", None))
        else " Reporting owner holds no officer, director or 10% role, so the "
        "signal is weaker than a named-officer purchase."
    )
    # Reported, never gated on: Scott & Xu find purchase excess returns rise
    # with the fraction of the existing holding added, but their measure is a
    # net per-stock-quarter one and does not license a per-row cutoff.
    buy_fraction, buy_band = holdings_fraction(observation)
    if buy_fraction is not None:
        size_note = (
            f" Adds {buy_fraction:.1%} to the insider's existing holding."
        )
    elif buy_band == BAND_NO_PRIOR_HOLDING:
        size_note = (
            " The insider held none of this stock beforehand, so there is no "
            "size-relative-to-holdings ratio; Scott & Xu report initial "
            "purchases earning no significant excess return."
        )
    else:
        size_note = ""
    return InsiderSignalClass.of(
        OPPORTUNISTIC, "opportunistic_purchase",
        f"Discretionary open-market purchase of ${value:,.0f} matching no "
        f"routine pattern.{size_note}{role_note}",
    )


def classify_observations(
    observations,
    history: InsiderHistory | None = None,
    thresholds: InsiderSignalThresholds | None = None,
) -> list:
    """Return copies of ``observations`` carrying their classification.

    ``history`` defaults to an index built from the observations themselves,
    which is enough for the cadence test inside one cache window but not for
    the three-year calendar test — pass the long-horizon index for that.
    ``thresholds`` defaults to ``InsiderSignalThresholds()``; see
    ``classify_transaction``.
    """
    rows = list(observations or [])
    index = history if history is not None else InsiderHistory.from_observations(rows)
    classified = []
    for row in rows:
        verdict = classify_transaction(row, index, thresholds)
        fraction, band = holdings_fraction(row)
        classified.append(row.model_copy(update={
            "signal_class": verdict.label,
            "signal_class_reason": verdict.reason,
            "signal_class_detail": verdict.detail,
            "signal_weight": verdict.weight,
            "holdings_fraction": fraction,
            "holdings_fraction_band": band,
        }))
    return classified
