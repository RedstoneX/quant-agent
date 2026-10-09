"""XBRL facts fetched, formatted and compared with a plain `sec_get` callable.

Never imports `src.data.earnings` or `src.data.sec_client`; no network.
"""

import json

from src.data.xbrl_facts import (
    fetch_xbrl_raw,
    format_xbrl_text,
    xbrl_comparable_values,
)


def _companyfacts(concept_entries: dict) -> bytes:
    """Build a minimal SEC companyfacts payload. `concept_entries` maps
    concept name -> unit key -> list of {end, val, form} dicts."""
    facts = {}
    for concept, units in concept_entries.items():
        facts[concept] = {"units": units}
    return json.dumps({"facts": {"us-gaap": facts}}).encode()


def test_xbrl_facts_picks_the_period_matching_the_filing():
    """Basic case: one concept, one value, matching period."""
    sec_get = lambda url: _companyfacts(
        {
            "NetIncomeLoss": {
                "USD": [
                    {"end": "2026-03-31", "val": 8584000000, "form": "10-Q"},
                ]
            },
        }
    )

    out = format_xbrl_text(fetch_xbrl_raw(sec_get, "70858", "BAC", "2026-04-25"))

    assert "Net Income: $8,584,000,000 (period ending 2026-03-31)" in out
    assert "STRUCTURED FINANCIAL FACTS" in out


def test_xbrl_facts_prefers_the_fresher_concept_over_a_stale_one():
    """The real bug caught in manual testing: MSFT/AAPL have OLD entries
    under the `Revenues` tag (some from 2010/2018 — the tag was abandoned
    around ASC 606 adoption) and CURRENT entries under
    `RevenueFromContractWithCustomerExcludingAssessedTax`. Trying concepts
    in order and stopping at the first with ANY data picked the decade-old
    number. Must pick the freshest value across ALL given concept names."""
    sec_get = lambda url: _companyfacts(
        {
            "Revenues": {
                "USD": [
                    {"end": "2010-12-31", "val": 19953000000, "form": "10-Q"},
                ]
            },
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "USD": [
                    {"end": "2026-03-31", "val": 82886000000, "form": "10-Q"},
                ]
            },
        }
    )

    out = format_xbrl_text(fetch_xbrl_raw(sec_get, "789019", "MSFT", "2026-04-30"))

    assert "Total Revenue: $82,886,000,000 (period ending 2026-03-31)" in out
    assert "19,953,000,000" not in out
    assert "2010-12-31" not in out


def test_xbrl_facts_drops_a_field_stale_beyond_the_staleness_window():
    """The second bug caught in manual testing: BAC's cash tag, CVX's
    long-term-debt tag, and NFLX's gross-profit tag were each years stale
    with no current alternative concept tried. A number that old is worse
    than no number — it's the exact 'PM sizes off an ungrounded field'
    failure this whole fix exists to close, just relocated from text
    extraction into XBRL. Must be omitted entirely, not shown as current."""
    sec_get = lambda url: _companyfacts(
        {
            "NetIncomeLoss": {
                "USD": [
                    {"end": "2026-03-31", "val": 2210000000, "form": "10-Q"},
                ]
            },
            "LongTermDebtNoncurrent": {
                "USD": [
                    {"end": "2018-09-30", "val": 29854000000, "form": "10-K"},
                ]
            },
        }
    )

    out = format_xbrl_text(fetch_xbrl_raw(sec_get, "93410", "CVX", "2026-04-25"))

    assert "Net Income: $2,210,000,000 (period ending 2026-03-31)" in out
    assert "Long-Term Debt" not in out
    assert "29,854,000,000" not in out


def test_xbrl_facts_fails_open_on_network_error():
    """A SEC API hiccup must degrade to empty string, not crash the whole
    earnings check — the caller falls back to text-only extraction exactly
    as it did before this existed."""

    def boom(url):
        raise TimeoutError("SEC is down")

    sec_get = boom

    out = format_xbrl_text(fetch_xbrl_raw(sec_get, "789019", "MSFT", "2026-04-30"))

    assert out == ""


def test_xbrl_comparable_values_keeps_only_the_one_to_one_fields():
    """`xbrl_comparable_values` must expose ONLY the concepts that map
    one-to-one onto an `EarningsAnalysis` field (revenue, net_income, cash,
    eps) — not gross_profit/operating_income (the analyst reports MARGINS,
    a ratio it computes itself, not these raw dollar figures) and not
    long_term_debt (XBRL here is non-current debt only, while
    `total_debt` conventionally includes the current portion — comparing
    them would flag a definitional gap as a factual error)."""
    raw = {
        "revenue": (50_000_000_000.0, "2026-03-31"),
        "net_income": (5_000_000_000.0, "2026-03-31"),
        "gross_profit": (20_000_000_000.0, "2026-03-31"),
        "operating_income": (10_000_000_000.0, "2026-03-31"),
        "assets": (300_000_000_000.0, "2026-03-31"),
        "cash": (10_000_000_000.0, "2026-03-31"),
        "long_term_debt": (40_000_000_000.0, "2026-03-31"),
        "eps": (2.5, "2026-03-31"),
    }

    values = xbrl_comparable_values(raw)

    assert values == {
        "revenue": 50_000_000_000.0,
        "net_income": 5_000_000_000.0,
        "cash": 10_000_000_000.0,
        "eps": 2.5,
    }


def test_xbrl_comparable_values_empty_when_nothing_fetched():
    """`fetch_xbrl_raw` fails open to `{}` (no XBRL data for this
    filer/period) — `xbrl_comparable_values` must pass that straight
    through as `{}`, not error or invent a value."""

    assert xbrl_comparable_values({}) == {}
