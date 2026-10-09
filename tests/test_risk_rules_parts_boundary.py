"""Boundary witnesses: the five parts split out of `src/risk/rules.py` build and
run alone — no pipeline, no broker, no database, no RiskRuleEngine, positions
and verdicts from local stubs.

`src/risk/rules.py` keeps the engine, the gross ceiling with its ladder, and
every number the ledger pins by that module's id; `sector_budget`,
`seat_agreement`, `unread_filing`, `conviction_bar` and `book_exposure` hold
the bodies, moved verbatim. Clause 5 of tests/boundary_harness.py: each part
has a test that imports it and never names the pipeline. Follows
tests/test_trailing_parts_boundary.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.quantities import ETF_LEVERAGE
from src.risk import book_exposure, conviction_bar, seat_agreement, sector_budget, unread_filing
from tests.boundary_harness import check_boundary

FUNCTION_ONLY_PARTS = [
    "src.risk.sector_budget",
    "src.risk.seat_agreement",
    "src.risk.unread_filing",
    "src.risk.conviction_bar",
]


@dataclass
class _Pos:
    symbol: str
    qty: float
    market_value: float
    sector: str = "Tech"


@pytest.mark.parametrize("module", FUNCTION_ONLY_PARTS)
def test_part_passes_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_book_exposure_part_fails_only_the_dataclass_init_clause():
    # `BookExposure` is a frozen dataclass: its __init__ is generated, which the
    # harness's clause 1 cannot see. Nothing else may fail.
    verdict = check_boundary("src.risk.book_exposure")
    assert set(verdict.failures) <= {1}, verdict.failures
    assert verdict.failures.get(1) == ["BookExposure: no __init__"]


def test_sector_budget_counts_long_and_short_sides_separately_from_stubs():
    held = [_Pos("AAA", 10, 1_000.0), _Pos("BBB", -5, -500.0), _Pos("CCC", 0, 0.0)]
    assert sector_budget.position_side(held[0]) == sector_budget.SECTOR_SIDE_LONG
    assert sector_budget.position_side(held[1]) == sector_budget.SECTOR_SIDE_SHORT
    gross = sector_budget.sector_side_gross(held)
    assert gross == {("Tech", "long"): 1_000.0, ("Tech", "short"): 500.0}
    pending = {}
    sector_budget.accumulate_pending_sector(pending, "Tech", "SHORT", 250.0)
    assert pending == {("Tech", "short"): 250.0}
    # Concentration scales the next size; it never vetoes below the hard cap.


def test_seat_agreement_aligns_stances_and_refuses_on_a_non_positive_score():
    bullish = next(iter(seat_agreement._BULLISH_STANCES))
    bearish = next(iter(seat_agreement._BEARISH_STANCES))
    assert seat_agreement.stance_is_aligned("technical", "AAA", bullish, wants_bullish=True)
    assert not seat_agreement.stance_is_aligned("technical", "AAA", bearish, wants_bullish=True)
    inverse = next(s for s, m in ETF_LEVERAGE.items() if m < 0)
    # A risk-off macro stance SUPPORTS owning an inverse ETF: polarity flips.
    assert seat_agreement.stance_is_aligned("macro", inverse, bearish, wants_bullish=True)
    assert seat_agreement.agreement_refuses_trade(0)
    assert seat_agreement.agreement_refuses_trade(-1)
    assert not seat_agreement.agreement_refuses_trade(1)


def test_unread_filing_reason_carries_its_prefix_and_the_symbol():
    reason = unread_filing.unread_filing_block_reason("AAA")
    assert reason.startswith(unread_filing.UNREAD_FILING_REASON_PREFIX)
    assert "AAA" in reason


def test_conviction_bar_part_exposes_its_prefix_and_helpers():
    assert conviction_bar.OWN_BAR_REASON_PREFIX == "R7 conviction bar"
    for name in (
        "own_bar_block_reason",
        "own_bar_opposition_reason",
        "_has_supported_directional_thesis",
        "_is_broadcast_macro_verdict",
    ):
        assert callable(getattr(conviction_bar, name))


def test_book_exposure_measures_a_stub_book_with_no_engine():
    held = [_Pos("AAA", 10, 1_000.0), _Pos("BBB", -5, -500.0), _Pos("CASHX", 1, 8_500.0)]
    assert book_exposure.gross_exposure(held, cash_park_symbol="CASHX") == 1_500.0
    book = book_exposure.book_exposure(held, 10_000.0, cash_park_symbol="CASHX")
    assert isinstance(book, book_exposure.BookExposure)
    assert book.equity == 10_000.0
    assert book.deployed_usd == 1_500.0
    assert book.net_usd == 500.0
    assert book.gross_usd == 1_500.0
    assert book_exposure.weight_pct_of(-500.0, "BBB", 10_000.0) == -5.0
    assert book_exposure.position_weight_pct(held[0], 10_000.0) == 10.0
    assert book_exposure._positive_float("abc", 3.0) == 3.0
    assert book_exposure.unmeasurable_gross_symbols(held) == []
