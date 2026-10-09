"""Universe screen: exchange-traded funds, no borrow requirement, and the
screen's own per-session affordability bound (owner approval 2026-10-09)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import universe_screen as us
from src.config import UniverseScreenConfig
from tests.test_universe_screen import GOOD_ASSET, TH, _good_bars

ETF_ASSET = {**GOOD_ASSET, "symbol": "VTI", "name": "Vanguard Total Stock Market ETF"}


def _sources(asset, profile, filings=()):
    return us.ScreenSources(
        get_asset=lambda s: asset,
        get_bars=lambda s: _good_bars(),
        get_profile=lambda s: profile,
        get_filings=lambda s: list(filings) if filings is not None else None,
    )


def _etf(category):
    return {"quote_type": "ETF", "category": category}


def test_a_plain_equity_etf_is_admitted_with_its_category_as_sector():
    # No SEC filing record (None) would fail a stock; a fund never reads it.
    result = us.screen_symbol("VTI", _sources(ETF_ASSET, _etf("Large Blend"), filings=None), TH)
    assert result.passed, result.failures
    assert result.measured["sector"] == "Large Blend"
    assert result.measured["company_size"] == "not applicable: fund"
    assert result.measured["takeover"] == "not applicable: fund"


@pytest.mark.parametrize(
    "symbol,category",
    [
        ("SOXL", "Trading--Leveraged Equity"),
        ("TQQQ", "Trading--Leveraged Equity"),
        ("SQQQ", "Trading--Inverse Equity"),
        ("XYZ", "Leveraged Equity"),
    ],
)
def test_a_leveraged_or_inverse_fund_is_refused(symbol, category):
    asset = {**ETF_ASSET, "symbol": symbol}
    result = us.screen_symbol(symbol, _sources(asset, _etf(category)), TH)
    assert result.failures == ["leveraged_or_inverse_fund"]


@pytest.mark.parametrize(
    "category",
    ["Ultrashort Bond", "Short Government", "Long Government", "Intermediate Core Bond", "Money Market - Taxable"],
)
def test_a_bond_cash_or_treasury_fund_is_refused(category):
    result = us.screen_symbol("SGOV", _sources(ETF_ASSET, _etf(category)), TH)
    assert result.failures == ["not_equity_fund"]


@pytest.mark.parametrize("category", [None, "", "   "])
def test_a_fund_with_no_category_is_refused(category):
    result = us.screen_symbol("VTI", _sources(ETF_ASSET, _etf(category)), TH)
    assert result.failures == ["fund_category_unknown"]
    assert not result.inconclusive


def test_a_fund_yahoo_does_not_call_an_etf_is_still_refused():
    profile = {"quote_type": "EQUITY", "market_cap_usd": 5e9, "sector": "Financials"}
    result = us.screen_symbol("VTI", _sources(ETF_ASSET, profile), TH)
    assert result.failures == ["not_common_stock"]
    closed_end = {"quote_type": "CLOSEDEND", "market_cap_usd": 5e9, "sector": "Financials"}
    result = us.screen_symbol("ACME", _sources(GOOD_ASSET, closed_end), TH)
    assert result.failures == ["not_common_stock"]


def test_a_non_shortable_stock_is_admitted():
    asset = {**GOOD_ASSET, "shortable": False, "borrow_status": "hard_to_borrow", "easy_to_borrow": False}
    profile = {"quote_type": "EQUITY", "market_cap_usd": 5e9, "sector": "Industrials"}
    result = us.screen_symbol("ACME", _sources(asset, profile), TH)
    assert result.passed, result.failures
