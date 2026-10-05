"""SEC XBRL structured facts: fetch the company-facts JSON, format and compare.

`fetch_xbrl_raw` takes the SEC GET as a plain callable, so nothing here knows
about the provider or the network.
"""

import json
import logging
from datetime import date
from typing import Callable

from src.data.sec_client import SEC_BASE

logger = logging.getLogger(__name__)


def fetch_xbrl_raw(sec_get: Callable[[str], bytes], cik: str, ticker: str, filing_date: str) -> dict[str, tuple[float, str]]:
    """Pull hard financial-statement figures from SEC's structured XBRL
    data instead of hoping a text-regex found the right heading in the
    filing's rendered HTML.

    ROOT CAUSE this replaces: `_extract_key_sections` below regexes for
    headings like "consolidated statements of operations" in flattened
    plain text, and filing layout / heading phrasing varies enough by
    filer that no amount of regex tuning holds up filing-to-filing.
    Measured on production: ~20 of 67 filings recovered under 1,600
    characters from a 184,000-character document this way, and 12 —
    including MSFT, AAPL, GOOGL, BAC, CVX, NFLX — extracted exactly ZERO
    financial figures. The earnings analyst had never seen a single
    number for those names.

    XBRL is the SEC-mandated structured version of these exact numbers,
    served for free with no auth beyond the same User-Agent every other
    SEC call here already sends. This is fetched INDEPENDENTLY of
    whatever `_extract_key_sections` finds below, so it grounds the
    numeric side of the analysis even when the text matcher fails
    completely — it doesn't make the matcher smarter, it makes the
    matcher's failure mode harmless for the numbers that matter most.
    It does NOT cover MD&A / risk-factors prose: XBRL doesn't tag prose,
    so that stays on the text-matching path exactly as before.

    Fails open: any error (network, missing CIK in XBRL, no matching
    concept) returns {} and callers proceed exactly as before this
    existed — a SEC API hiccup can only leave the analysis as good as
    before, never worse. Two callers share this one fetch so a filing
    is only hit once per `_check_symbol` run, not twice:
    `format_xbrl_text` (below) turns this into the prompt's
    text block, and `xbrl_comparable_values` turns it into the
    parsed-number ground truth `_classify_earnings_status`
    (src/pipeline_stages.py) cross-checks the analyst's own figures
    against.

    Returns a plain `{concept_key: (value, period_end_iso)}` dict —
    `concept_key` is the internal name used below ("revenue",
    "net_income", "gross_profit", "operating_income", "assets", "cash",
    "long_term_debt", "eps"), not the raw XBRL tag name.
    """
    padded_cik = cik.zfill(10)
    url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{padded_cik}.json"
    try:
        data = json.loads(sec_get(url))
    except Exception as e:  # noqa: BLE001 — fail open, see docstring
        logger.warning(
            "XBRL companyfacts fetch failed for %s (CIK %s): %s", ticker, cik, e,
        )
        return {}

    facts = data.get("facts", {}).get("us-gaap", {})
    if not facts:
        return {}

    try:
        target = date.fromisoformat(filing_date)
    except (TypeError, ValueError):
        target = None

    def _best_value(concept_names: list[str], unit_key: str):
        # Gather candidates across ALL given concept names, not just the
        # first one that has any data at all. Filers change which XBRL
        # tag they report under over time — e.g. many large companies
        # stopped using `Revenues` around ASC 606 adoption (~2018) in
        # favor of `RevenueFromContractWithCustomerExcludingAssessedTax`.
        # `Revenues` still HAS entries for those filers, just a decade
        # stale — stopping at "first concept with any data" silently
        # picked a ~10-year-old number for MSFT/AAPL revenue in testing.
        # Comparing recency across every concept and picking the single
        # freshest match closes that.
        dated: list[tuple[date, dict]] = []
        stale_fallbacks: list[tuple[date, dict]] = []
        for concept in concept_names:
            entries = facts.get(concept, {}).get("units", {}).get(unit_key, [])
            if not entries:
                continue
            candidates = [e for e in entries if e.get("form") in ("10-Q", "10-K")]
            if not candidates:
                candidates = entries
            for e in candidates:
                end = e.get("end")
                if not end:
                    continue
                try:
                    end_d = date.fromisoformat(end)
                except ValueError:
                    continue
                if target is None or end_d <= target:
                    dated.append((end_d, e))
                else:
                    stale_fallbacks.append((end_d, e))
        if dated:
            dated.sort(key=lambda pair: pair[0])
            chosen_end, chosen = dated[-1]
        elif stale_fallbacks:
            stale_fallbacks.sort(key=lambda pair: pair[0])
            chosen_end, chosen = stale_fallbacks[-1]
        else:
            return None
        # Some filers stop reporting a given XBRL tag (switch to a
        # differently-named one, or fold it into a different line item)
        # without ever filing a final value under the old tag — that
        # stale entry still LOOKS like real data and would otherwise be
        # presented as current. Measured while building this: BAC's
        # cash tag was 5+ years stale, CVX's long-term-debt tag ~8
        # years, NFLX's gross-profit tag ~5 years, despite each having
        # CURRENT data available under the concept the analysis prompt
        # actually needs. A number this old is worse than no number —
        # it's the exact "PM sizes off an ungrounded field" failure
        # mode this whole fix exists to close, just moved from text
        # extraction into XBRL. One fiscal year plus one quarter of
        # slack (~455 days) comfortably covers a filer that's merely
        # running one quarter behind without accepting a genuinely
        # abandoned tag.
        MAX_STALENESS_DAYS = 455
        if target is not None and (target - chosen_end).days > MAX_STALENESS_DAYS:
            return None
        val = chosen.get("val")
        end = chosen.get("end", "?")
        if val is None:
            return None
        return val, end

    raw: dict[str, tuple[float, str]] = {}
    revenue = _best_value(
        ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"], "USD",
    )
    if revenue is not None:
        raw["revenue"] = revenue
    for concept, key in (
        ("NetIncomeLoss", "net_income"),
        ("GrossProfit", "gross_profit"),
        ("OperatingIncomeLoss", "operating_income"),
        ("Assets", "assets"),
        ("CashAndCashEquivalentsAtCarryingValue", "cash"),
        ("LongTermDebtNoncurrent", "long_term_debt"),
    ):
        v = _best_value([concept], "USD")
        if v is not None:
            raw[key] = v
    eps = _best_value(["EarningsPerShareDiluted"], "USD/shares")
    if eps is not None:
        raw["eps"] = eps
    return raw

# Human-readable labels for the text block the LLM prompt reads, in the
# same fixed order the block has always rendered in — kept in one place
# so `format_xbrl_text` and any future consumer of
# `fetch_xbrl_raw` render the same concepts under the same names.
XBRL_TEXT_LABELS = (
    ("revenue", "Total Revenue"),
    ("net_income", "Net Income"),
    ("gross_profit", "Gross Profit"),
    ("operating_income", "Operating Income"),
    ("assets", "Total Assets"),
    ("cash", "Cash & Equivalents"),
    ("long_term_debt", "Long-Term Debt"),
    ("eps", "Diluted EPS"),
)

def format_xbrl_text(raw: dict[str, tuple[float, str]]) -> str:
    """Pure formatter half of `format_xbrl_text` — no network."""
    if not raw:
        return ""
    lines: list[str] = []
    for key, label in XBRL_TEXT_LABELS:
        entry = raw.get(key)
        if entry is None:
            continue
        value, end = entry
        if key == "eps":
            lines.append(f"{label}: ${value:.2f} (period ending {end})")
        else:
            lines.append(f"{label}: ${value:,.0f} (period ending {end})")
    if not lines:
        return ""
    return (
        "=== STRUCTURED FINANCIAL FACTS (SEC XBRL, not text-extracted) ===\n"
        + "\n".join(lines)
        + "\n"
    )

# Concept keys (from `fetch_xbrl_raw`) that map ONE-TO-ONE onto an
# `EarningsAnalysis` field — the same real-world figure, not a derived
# or differently-scoped one. Deliberately excludes `gross_profit` /
# `operating_income` (the analyst reports MARGINS — ratios it computes
# itself — not the raw dollar figures XBRL tags, so there's no
# apples-to-apples number to compare) and `long_term_debt` (XBRL here
# is non-current debt only, while `EarningsBalanceSheet.total_debt`
# conventionally includes the current portion too — comparing them
# would flag a definitional gap as if it were a factual error). Cash
# flow statement fields aren't fetched by `fetch_xbrl_raw` at all yet,
# so there is nothing to compare them against.
XBRL_COMPARABLE_KEYS = ("revenue", "net_income", "cash", "eps")

def xbrl_comparable_values(raw: dict[str, tuple[float, str]]) -> dict[str, float]:
    """The subset of `fetch_xbrl_raw`'s output usable as real,
    directly-comparable ground truth — see `XBRL_COMPARABLE_KEYS` for
    which fields and why only those. Pure/no network: callers already
    have `raw` from one `fetch_xbrl_raw` call and derive both the
    prompt text block and this from it, rather than fetching twice.

    Returns {} (not None) when nothing comparable was available — the
    SEC XBRL fetch failed open, or none of the comparable concepts had
    current data — which the mismatch check downstream already treats
    the same as "nothing to compare," never as a mismatch.
    """
    return {
        key: raw[key][0] for key in XBRL_COMPARABLE_KEYS if key in raw
    }

