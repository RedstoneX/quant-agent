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

3. **Sell size relative to the holding is REPORTED, never a cutoff.** The
   research line — "on the sell side only large sales that are *also* large
   relative to the insider's total position predict negative returns" —
   traces to Scott & Xu, *Some Insider Sales Are Positive Signals*
   (Financial Analysts Journal 60(3), 2004) — 512,133 combined transactions,
   80,742 company-quarters, 1987-2002 — which measures "shares traded as a
   percentage of shares owned" in three bands: under 10%, 10-50% and over
   50% of the holding. Their size- and B/P-adjusted result: sales over
   100,000 shares earn -0.81% quarterly excess return only in the over-50%
   band (-0.06% and +0.08% in the two lower bands, insignificant), while
   sales under 100,000 shares earn a *positive* +0.68% in the under-10% band
   and +0.44% in the 10-50% band, both significant at the 1% level. The same
   variable also differentiates purchases: +0.38% / +1.06% / +1.42% across
   the three bands, and initial purchases (no prior holding, so no ratio
   exists) earn an insignificant +0.10%.
   <https://rpc.cfainstitute.org/research/financial-analysts-journal/2004/some-insider-sales-are-positive-signals>

   This module previously turned that variable into a sell cutoff — below
   some fraction of the holding a sale was relabelled ROUTINE, weight 0.0,
   and dropped out of the ranking. **That was removed on 2026-09-13** and no
   replacement cutoff was invented, for three reasons the paper itself
   supplies.

   * *No band edge is a "not a directional view" line.* The only boundary
     the paper's prose marks as a significance boundary is 50%, not 10%:
     "The group of stocks with net total sales exceeding 100,000 shares had
     an average excess return of -0.55 percent, but of that group, those
     stocks for which shares sold accounted for more than half of shares
     owned had average excess return of -1.17 percent. Excess returns on
     stocks with the same level of shares sold but a lower percentage of
     holdings were negative but statistically insignificant."
   * *The low band is not noise — it is signal with the opposite sign.*
     "Small sales that represented small percentages of shares owned not
     only did not predict poor performance but were associated with
     significantly positive abnormal returns." A ROUTINE label means "no
     predictive power" (Cohen/Malloy/Pomorski); applying it to a row this
     source calls significantly positive states the opposite of the
     evidence and then discards it at weight 0.0.
   * *The unit does not transfer.* The paper's ratio is a net,
     per-stock-quarter figure over a six-month formation window against
     holdings aggregated across every insider who reported one. One Form 4
     row is not that object. Departure #3 already refused, on exactly this
     ground, to gate purchases on the same bands; the same refusal is owed
     to sells.

   What remains: `holdings_fraction` and its band are reported on every
   row, for buys and sells alike, banded with the paper's own boundaries,
   with the paper's finding for that band written into the detail text. The
   seat weighs it. Nothing filters on it. The open question this leaves —
   that `signal_weight` is a single "how much attention" scalar with no way
   to say "attention, and the sign is the other way" — is WORK.md item 63,
   not something to be closed by picking a number.

The classifier is pure Python, deterministic, and makes no model call.

Every numeric threshold below (calendar-routine years, cadence window) is an
operator-tunable setting, not a fixed number in this
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

# The bands label a row; they never admit or reject one. Since 2026-09-13 no
# holdings ratio anywhere in this module changes a classification — see
# departure #3 in the module docstring.
BANDS_ARE_REPORTING_ONLY = True

BAND_UNDER_LOW = "under_10pct"
BAND_MID = "10_to_50pct"
BAND_OVER_HIGH = "over_50pct"
BAND_NO_PRIOR_HOLDING = "no_prior_holding"
BAND_UNKNOWN = ""

# What Scott & Xu measured for each sell band, written into the detail text so
# the seat is handed the SIGN of the evidence and not just the ratio. Figures
# are their Table 6 (size- and B/P-adjusted quarterly excess returns); the
# split by absolute share count is theirs too, and this module does not
# reproduce it because a per-row share count is not their net per-quarter one.
_SELL_BAND_EVIDENCE = {
    BAND_UNDER_LOW: (
        "Scott & Xu (FAJ 2004) measure this band as mildly BULLISH, not "
        "neutral: sales under 100,000 shares at under 10% of shares owned "
        "earned +0.68% size/B-P-adjusted quarterly excess return "
        "(significant at 1%), and large sales in this band were "
        "insignificant (-0.06%)."
    ),
    BAND_MID: (
        "Scott & Xu (FAJ 2004) measure this band as +0.44% quarterly excess "
        "return for sales under 100,000 shares (significant at 1%) and an "
        "insignificant +0.08% for larger ones — no negative prediction."
    ),
    BAND_OVER_HIGH: (
        "Scott & Xu (FAJ 2004): this is the only band that predicts negative "
        "returns, and only for large sales — -0.81% quarterly excess return "
        "(significant at 5%) above 100,000 shares, an insignificant +0.06% "
        "below it."
    ),
}


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
        planned = (
            " The 10b5-1 flag is set but is deliberately not treated as a "
            "noise marker: planned and discretionary high-value sales show "
            "similar opportunism."
            if is_10b5_1 else ""
        )
        # No cutoff. The sale survived both Cohen/Malloy/Pomorski routine
        # tests, so it is discretionary in the only sense this taxonomy
        # defines; the holding ratio is handed on as evidence, with the sign
        # its band carries in the source, rather than being collapsed into a
        # yes/no. See departure #3 and WORK.md item 63.
        band = _band(fraction, no_prior_holding=False)
        evidence = _SELL_BAND_EVIDENCE.get(band, "")
        return InsiderSignalClass.of(
            OPPORTUNISTIC, "discretionary_sale",
            f"Discretionary sale of {fraction:.1%} of the insider's holding, "
            f"matching no routine pattern. {evidence}{planned}",
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
