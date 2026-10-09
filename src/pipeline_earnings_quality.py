"""Earnings-figure sanity and the XBRL cross-check.

Step 11 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210). Moved VERBATIM
out of `src/pipeline_stages.py`: one question is answered here, whether the
earnings seat's answer carries REAL extracted figures or only a
schema-valid shell, and whether those figures agree with the filed XBRL
facts. Nothing else reads these names, and every one of them is
re-exported from `src.pipeline_stages` so each original import path and
each test patch target is unchanged.

This module must not import `src.pipeline`.
"""

from __future__ import annotations

import logging
import re

#: The moved code logged under `src.pipeline_stages` before the move and
#: still does; binding the name rather than `__name__` keeps log records
#: byte-identical.
logger = logging.getLogger("src.pipeline_stages")


# 2026-09-04 fix: before this, `data_status["earnings"]` was "ok" purely on
# whether `earnings_future.result()` returned without raising — the exact
# shape of a real production incident where 12/67 filings once came back
# with a schema-valid `EarningsAnalysis` that had ZERO real extracted
# figures (a parsing bug at the root cause, since fixed). Exception-only
# status can never catch that class of failure, or a future one shaped like
# it, because it never inspects what the seat actually returned. Mirrors the
# macro/news/tech fixes above: coverage of REAL CONTENT is authoritative
# over "did the call merely not crash."
#
# `EarningsAnalysis`'s financial fields are all plain `str`, not
# `Optional[float]` — every one of profitability/cash_flow/balance_sheet
# defaults to the literal sentinel "not disclosed" (see src/models.py) when
# the LLM has nothing to report, and `revenue.total` (no default) can still
# just BE that sentinel string since the schema only requires "some string",
# not a real figure. That sentinel — not `None` and not `0`/falsy — is the
# only honest signal of "absent" this model exposes. A genuinely zero
# balance (e.g. a company with no debt) is reported as an explicit "$0" or
# "0", which is real content and must NOT be confused with "not disclosed" —
# the same "zero is overloaded" mistake this codebase already paid for once
# (docs/WORK.md item 13, PR #255 — a 0% target weight ambiguously meant
# refuse/close/short until it was split into distinct intents). Checking
# for the exact sentinel string, not truthiness, keeps those cases apart.
_EARNINGS_NOT_DISCLOSED = "not disclosed"


def _earnings_field_disclosed(value) -> bool:
    """True when a single EarningsAnalysis string field carries real content
    (anything but blank or the "not disclosed" sentinel, case/whitespace
    insensitive)."""
    text = str(value or "").strip()
    return bool(text) and text.lower() != _EARNINGS_NOT_DISCLOSED


def _earnings_analysis_has_real_figures(analysis: dict) -> bool:
    """Structural content check for one filing's `EarningsAnalysis.model_dump()`.

    Only the fields that should carry an actual filed number are checked —
    `revenue.total`/`yoy_growth`, all of `profitability`, all of `cash_flow`,
    and the two `balance_sheet` figures. `balance_sheet.assessment` is
    deliberately excluded: it is the analyst's own prose judgement, not a
    filed figure, and an LLM will always have SOMETHING to say there even
    when every real number above it is missing — including it would let a
    genuinely content-free filing still read as "has real content".
    """
    if not isinstance(analysis, dict):
        return False
    revenue = analysis.get("revenue") or {}
    profitability = analysis.get("profitability") or {}
    cash_flow = analysis.get("cash_flow") or {}
    balance_sheet = analysis.get("balance_sheet") or {}
    candidate_fields = [
        revenue.get("total"),
        revenue.get("yoy_growth"),
        profitability.get("gross_margin"),
        profitability.get("operating_margin"),
        profitability.get("net_income"),
        profitability.get("eps"),
        cash_flow.get("operating_cf"),
        cash_flow.get("free_cf"),
        cash_flow.get("capex"),
        balance_sheet.get("cash_and_equivalents"),
        balance_sheet.get("total_debt"),
    ]
    return any(_earnings_field_disclosed(v) for v in candidate_fields)


# Phrases in the LLM's own self-reported `EarningsAnalysis.data_quality`
# that indicate a genuine self-reported problem, not routine hedging. The
# owner's own framing (2026-09-04): a seat must never be able to claim "ok"
# while its own free-text field says something is wrong — "if there's no
# data, can it still be... everything's good, just plain lying... since
# it's critical". This is prose, not a structured field, so it is judged by
# substring match on phrases that mean "I could not actually get this data",
# not by one hardcoded exact string.
_EARNINGS_DATA_QUALITY_RED_FLAGS = (
    "unable to find",
    "unable to extract",
    "unable to locate",
    "could not extract",
    "could not find",
    "could not locate",
    "no figures available",
    "no financial data",
    "no data available",
    "not available in the filing",
    "filing incomplete",
    "incomplete filing",
    "insufficient data",
    "no meaningful data",
    "unable to determine",
    "unable to assess",
    "data not found",
    "figures not found",
)


def _earnings_data_quality_flags_problem(data_quality) -> bool:
    """True when the analyst's own `data_quality` self-report describes a
    real extraction problem, even if every structured field technically
    validated. The bare "not disclosed" default (no self-report at all) is
    NOT a red flag by itself — see `_earnings_field_disclosed`'s docstring;
    only prose that actively says something went wrong counts."""
    text = str(data_quality or "").strip().lower()
    if not text or text == _EARNINGS_NOT_DISCLOSED:
        return False
    return any(phrase in text for phrase in _EARNINGS_DATA_QUALITY_RED_FLAGS)


# --- XBRL cross-check: catches a WRONG (not merely empty/self-flagged) figure ---
#
# 2026-09-05: the emptiness/self-report check above closes "the AI returned
# nothing useful." It cannot catch the harder case the owner specifically
# flagged: a confident, plausible-looking, non-empty figure that simply
# doesn't match what the filer actually filed — the AI isn't reporting any
# doubt, so nothing above would ever see a problem. `EarningsDataProvider`
# (src/data/earnings.py) already fetches SEC's own structured XBRL figures
# for exactly this filing and hands the earnings analyst the real numbers
# directly in its prompt (the "STRUCTURED FINANCIAL FACTS" block) — so this
# doesn't need a second data source, just a comparison against ground
# truth that was already fetched.
#
# Scope is deliberately narrow: only NUMERIC, FACTUAL fields that have one
# real correct answer to check against. Prose/judgment fields (strategic
# risk, management execution, investment thesis, valuation context) have no
# single right answer — forcing a "ground truth" for those would be
# inventing a fake one, so they are out of scope on purpose, not an
# oversight.

_UNIT_MULTIPLIERS = {
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "m": 1e6,
    "mm": 1e6,
    "million": 1e6,
    "k": 1e3,
    "thousand": 1e3,
}

# A number, optionally $-prefixed / comma-separated / %-suffixed / unit-
# suffixed, after any surrounding parens (accounting negative notation) and
# whitespace have already been stripped by the caller.
_FIGURE_RE = re.compile(
    r"^-?\d[\d,]*(?:\.\d+)?\s*(billion|million|thousand|bn|mm|b|m|k)?$",
    re.IGNORECASE,
)


def _parse_reported_figure(value) -> float | None:
    """Best-effort real number out of one `EarningsAnalysis` free-text
    figure field — "$50B", "$5.2 billion", "12%", "($500M)", "$0",
    "not disclosed".

    Returns None for the absence sentinel (see `_earnings_field_disclosed`
    — "not disclosed" is "nothing to compare," never a mismatch) and for
    any text this parser doesn't recognize. An unparseable string is NOT
    evidence of a mismatch — it just means the model phrased it in a way
    this regex doesn't cover — so only a pair of numbers that both parsed
    AND actually disagree should ever be flagged (`_earnings_xbrl_mismatch_fields`).
    A real, disclosed zero ("$0") parses to 0.0, distinct from None.
    """
    if not _earnings_field_disclosed(value):
        return None
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = text.replace(",", "").replace("$", "").replace("%", "").strip()
    match = _FIGURE_RE.match(text)
    if not match:
        return None
    unit = (match.group(1) or "").lower()
    number_text = text[: match.start(1)] if match.group(1) else text
    try:
        number = float(number_text.strip())
    except ValueError:
        return None
    number *= _UNIT_MULTIPLIERS.get(unit, 1)
    return -abs(number) if negative else number


# 5% mirrors the traditional accounting-materiality rule of thumb (SEC Staff
# Accounting Bulletin No. 99 discusses 5% of the relevant base as the
# customary quantitative starting point before any qualitative override)
# and comfortably covers legitimate rounding: paraphrasing an exact XBRL
# figure to 2 significant digits for readability ("~$50B" for an exact
# $49.7B) is under 1% off, well inside this band. A gap bigger than 5% is
# not "rounded for readability" — the analyst's number and the filer's own
# reported number disagree about what happened.
_EARNINGS_XBRL_TOLERANCE_PCT = 0.05

# Floors stop the percentage test from exploding on a near-zero true value
# (breakeven net income, de-minimis cash) where even a trivial rounding
# difference would otherwise read as a huge relative mismatch — only kicks
# in when 5% of the real figure is smaller than this. $10M is small next to
# any filer this fetch actually has data for (see `_fetch_xbrl_raw`'s
# docstring in src/data/earnings.py: MSFT/AAPL/GOOGL/BAC/CVX/NFLX-scale
# filers), so it never masks a real error on a figure that size — it only
# absorbs rounding noise on a near-zero one. $0.02 is one cent above
# ordinary EPS reporting precision (nearest cent), covering print/rounding
# noise without covering an actually-wrong EPS figure.
_EARNINGS_XBRL_DOLLAR_FLOOR = 10_000_000.0
_EARNINGS_XBRL_EPS_FLOOR = 0.02

# (xbrl_key from EarningsDataProvider._xbrl_comparable_values, EarningsAnalysis
# section, field, absolute floor) — only fields where the XBRL concept and
# the model field are the SAME real-world figure by definition. See
# `EarningsDataProvider._XBRL_COMPARABLE_KEYS` (src/data/earnings.py) for
# why margins, total_debt, and cash-flow-statement fields are excluded —
# that exclusion is authoritative; this tuple must stay a subset of it.
_EARNINGS_XBRL_COMPARABLE_FIELDS = (
    ("revenue", "revenue", "total", _EARNINGS_XBRL_DOLLAR_FLOOR),
    ("net_income", "profitability", "net_income", _EARNINGS_XBRL_DOLLAR_FLOOR),
    ("eps", "profitability", "eps", _EARNINGS_XBRL_EPS_FLOOR),
    ("cash", "balance_sheet", "cash_and_equivalents", _EARNINGS_XBRL_DOLLAR_FLOOR),
)


def _earnings_xbrl_mismatch_fields(analysis: dict, xbrl_values: dict) -> list[str]:
    """Which of the analyst's own reported figures materially contradict
    the real SEC XBRL values already fetched for this same filing.

    Returns "section.field" names for every real disagreement — empty when
    nothing was comparable at all (no XBRL data for this filer/period —
    `xbrl_values` fails open to `{}`, see `EarningsReport.xbrl_facts`) or
    when every comparable field agreed within `_EARNINGS_XBRL_TOLERANCE_PCT`.
    A field the analyst reported as "not disclosed", or in a format
    `_parse_reported_figure` doesn't recognize, is skipped rather than
    flagged — only an ACTUAL, provable disagreement between two real parsed
    numbers counts.
    """
    if not isinstance(analysis, dict) or not xbrl_values:
        return []
    mismatches = []
    for xbrl_key, section, field_name, floor in _EARNINGS_XBRL_COMPARABLE_FIELDS:
        true_value = xbrl_values.get(xbrl_key)
        if true_value is None:
            continue
        reported = _parse_reported_figure((analysis.get(section) or {}).get(field_name))
        if reported is None:
            continue
        tolerance = max(abs(true_value) * _EARNINGS_XBRL_TOLERANCE_PCT, floor)
        if abs(reported - true_value) > tolerance:
            mismatches.append(f"{section}.{field_name}")
    return mismatches


def _classify_earnings_status(earnings_results: list) -> str:
    """Turn this run's `earnings_results` into an honest `data_status["earnings"]`.

    `earnings_results` items are `{"symbol", "analysis", "xbrl_facts",
    "queued", ...}` dicts from `EarningsAnalystAgent`/`_load_earnings_analyses`
    — see `EarningsAnalystAgent._analyze_new`/`_load_analysis`. Only items
    that actually carry an `analysis` dict are judged for content here; a
    `queued=True` placeholder (a new filing preprocess hasn't analyzed yet)
    is already surfaced honestly by that flag and sized around downstream —
    that is a separate, already-handled state this pass does not touch.

    - No analyzed items at all (no filings today, or every filing is still
      queued) is the ordinary, most-common day and stays "ok" — earnings
      not existing is not a data failure.
    - Every analyzed item has real figures and a clean self-report → "ok".
    - Some real, some not → "partial" (mirrors tech's partial for a
      mixed batch).
    - An analyzed item exists but NONE of them have real content (bad
      structural content, a red-flagged self-report, or both) → a status
      distinct from both "ok" and "empty". Deliberately NOT "empty": the
      notifier's data-quality alert (`maybe_alert_data_quality`,
      src/notifier.py) treats any status outside `("ok", "empty")` as
      alert-worthy, and unlike smart_money's "empty" (a quiet day is
      genuinely benign), earnings content going missing AFTER a real
      filing existed and was run through the LLM is never benign — it is
      the exact silent-failure shape this fix exists to catch, so it must
      alert every time, not blend into the "nothing to see" bucket.
    - Any analyzed item's own reported figures materially CONTRADICT the
      real SEC XBRL data already fetched for that filing → "figures_contradicted",
      regardless of how many other filings this run were clean. Deliberately
      a distinct status, not folded into "content_missing": those two are
      different failure shapes an operator needs to read differently — one
      says "the seat gave us nothing," the other says "the seat gave us a
      confident, wrong answer," which is the specific silent-failure shape
      this cross-check exists to catch and is worse than empty, not the
      same as it. Deliberately dominant over "ok"/"partial" too — a batch
      that is otherwise clean but contains one provably wrong filing is not
      "mostly fine," and diluting it into "partial" (the routine, expected
      bucket for an ordinary mixed day) would bury exactly the alert this
      exists to raise.
    """
    analyzed = [item for item in earnings_results if isinstance(item, dict) and isinstance(item.get("analysis"), dict)]
    if not analyzed:
        return "ok"

    good = []
    contradicted = []
    for item in analyzed:
        a = item["analysis"]
        if not _earnings_analysis_has_real_figures(a) or _earnings_data_quality_flags_problem(a.get("data_quality")):
            continue
        mismatches = _earnings_xbrl_mismatch_fields(a, item.get("xbrl_facts") or {})
        if mismatches:
            contradicted.append((item.get("symbol", "?"), mismatches))
            continue
        good.append(a)

    if contradicted:
        logger.error(
            "Earnings: %d filing(s) this run reported figures that materially contradict SEC XBRL data — %s",
            len(contradicted),
            "; ".join(f"{sym}: {', '.join(fields)}" for sym, fields in contradicted),
        )
        return "figures_contradicted"
    if len(good) == len(analyzed):
        return "ok"
    if good:
        return "partial"
    return "content_missing"
