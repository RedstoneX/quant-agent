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
   predict negative returns."

The classifier is pure Python, deterministic, and makes no model call.
"""
from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

# A disposition smaller than this share of the insider's pre-transaction
# holding is diversification/liquidity noise rather than a directional view.
MIN_MATERIAL_SELL_FRACTION = 0.05

# Cohen/Malloy/Pomorski define a routine insider as one who traded in the same
# calendar month in each of the three preceding years.
CALENDAR_ROUTINE_YEARS = 3

# Fallback cadence test, used when three years of history are not on hand.
# Deliberately narrower than the calendar test: it needs at least this many
# prior trades whose spacing is close to uniform.
MIN_CADENCE_TRADES = 3
CADENCE_MIN_MEAN_GAP_DAYS = 20.0
CADENCE_MAX_MEAN_GAP_DAYS = 120.0
# Coefficient of variation of the gaps. 0.25 admits a monthly or quarterly
# programme that drifts by a few days; it rejects lumpy discretionary trading.
CADENCE_MAX_GAP_DISPERSION = 0.25

OPPORTUNISTIC = "opportunistic"
ROUTINE = "routine"
INDETERMINATE = "indeterminate"

_WEIGHTS = {OPPORTUNISTIC: 1.0, INDETERMINATE: 0.5, ROUTINE: 0.0}

# Roles that carry published signal. CFO purchases beat CEO purchases; a
# reporting owner with no officer/director/10% standing is a weaker source.
_INSIDE_ROLE_MARKERS = ("officer", "director", "tenpercentowner")


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


def _sell_fraction(shares: float | None, post_shares: float | None) -> float | None:
    """Fraction of the insider's pre-transaction holding that was disposed."""
    if shares is None or post_shares is None or shares <= 0:
        return None
    pre_shares = shares + post_shares
    if pre_shares <= 0:
        return None
    return shares / pre_shares


def _has_inside_role(roles) -> bool:
    for role in roles or ():
        collapsed = str(role or "").strip().lower().replace(" ", "").replace("_", "")
        if any(marker in collapsed for marker in _INSIDE_ROLE_MARKERS):
            return True
    return False


def _calendar_routine_years(
    prior: Sequence[InsiderPriorTrade], transaction_date: date,
) -> int:
    """Consecutive preceding years with a trade in the same calendar month."""
    months = {(trade.transaction_date.year, trade.transaction_date.month) for trade in prior}
    streak = 0
    for offset in range(1, CALENDAR_ROUTINE_YEARS + 1):
        if (transaction_date.year - offset, transaction_date.month) not in months:
            break
        streak += 1
    return streak


def _cadence_gap_stats(
    prior: Sequence[InsiderPriorTrade], transaction_date: date,
) -> tuple[float, float] | None:
    """Mean gap and coefficient of variation across the trade series."""
    dates = sorted({trade.transaction_date for trade in prior} | {transaction_date})
    if len(dates) < MIN_CADENCE_TRADES + 1:
        return None
    gaps = [
        float((later - earlier).days)
        for earlier, later in zip(dates, dates[1:])
        if (later - earlier).days > 0
    ]
    if len(gaps) < MIN_CADENCE_TRADES:
        return None
    mean_gap = statistics.fmean(gaps)
    if mean_gap <= 0:
        return None
    dispersion = statistics.pstdev(gaps) / mean_gap
    return mean_gap, dispersion


def classify_transaction(
    observation,
    history: InsiderHistory | None = None,
) -> InsiderSignalClass:
    """Label one Form 4 row routine or opportunistic, with the reason.

    Rules are evaluated in precedence order and the first match wins, so the
    reason on the result is always the single rule that decided it.
    """
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
        streak = _calendar_routine_years(prior, transaction_date)
        if streak >= CALENDAR_ROUTINE_YEARS:
            return InsiderSignalClass.of(
                ROUTINE, "calendar_routine",
                f"Same insider traded {symbol} in {transaction_date:%B} in each "
                f"of the {streak} preceding years — a routine trader under "
                "Cohen/Malloy/Pomorski, which carries no predictive power.",
            )
        stats = _cadence_gap_stats(prior, transaction_date)
        if stats is not None:
            mean_gap, dispersion = stats
            if (
                CADENCE_MIN_MEAN_GAP_DAYS <= mean_gap <= CADENCE_MAX_MEAN_GAP_DAYS
                and dispersion <= CADENCE_MAX_GAP_DISPERSION
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
        if fraction < MIN_MATERIAL_SELL_FRACTION:
            if is_10b5_1:
                return InsiderSignalClass.of(
                    ROUTINE, "planned_small_disposition",
                    f"Sold {fraction:.1%} of the holding under a pre-arranged "
                    "10b5-1 plan — proportionally immaterial and scheduled.",
                )
            return InsiderSignalClass.of(
                ROUTINE, "immaterial_stake_sale",
                f"Sold {fraction:.1%} of the holding; only sales that are large "
                "relative to the insider's position predict negative returns.",
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
    return InsiderSignalClass.of(
        OPPORTUNISTIC, "opportunistic_purchase",
        f"Discretionary open-market purchase of ${value:,.0f} matching no "
        f"routine pattern.{role_note}",
    )


def classify_observations(observations, history: InsiderHistory | None = None) -> list:
    """Return copies of ``observations`` carrying their classification.

    ``history`` defaults to an index built from the observations themselves,
    which is enough for the cadence test inside one cache window but not for
    the three-year calendar test — pass the long-horizon index for that.
    """
    rows = list(observations or [])
    index = history if history is not None else InsiderHistory.from_observations(rows)
    classified = []
    for row in rows:
        verdict = classify_transaction(row, index)
        classified.append(row.model_copy(update={
            "signal_class": verdict.label,
            "signal_class_reason": verdict.reason,
            "signal_class_detail": verdict.detail,
            "signal_weight": verdict.weight,
        }))
    return classified
