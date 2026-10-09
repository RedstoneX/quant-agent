import json
from decimal import Decimal
from pathlib import Path

import pytest

from src.data import nasdaq_listing as nl

FIX = Path(__file__).parent / "fixtures" / "nasdaq_listing"


def _load(name):
    return json.loads((FIX / name).read_text())


def _fetch(url, headers, timeout_s):
    assert headers["Accept"] == "application/json" and "Mozilla" in headers["User-Agent"]
    assert timeout_s == nl.TIMEOUT_S
    return _load("stocks.json") if url == nl.STOCKS_URL else _load("etf.json")


def test_parse_counts_and_one_call_per_endpoint():
    calls = []

    def fetch(url, headers, timeout_s):
        calls.append(url)
        return _fetch(url, headers, timeout_s)

    listing = nl.load_listing(fetch=fetch)
    assert len(listing) == 7 and sorted(calls) == sorted([nl.STOCKS_URL, nl.FUNDS_URL])


def test_class_share_normalised():
    rec = nl.load_listing(fetch=_fetch).get("BRK-B")
    assert rec is not None and rec.market_cap == Decimal("900000000000")


def test_blank_and_zero_cap_are_none():
    listing = nl.load_listing(fetch=_fetch)
    assert listing.get("NOCAP").market_cap is None
    assert listing.get("ZERO").market_cap is None
    assert listing.get("NOCAP").sector_recorded is None


def test_preferred_flagged_common_not():
    listing = nl.load_listing(fetch=_fetch)
    assert listing.get("BAC^B").is_preferred
    assert not listing.get("AAPL").is_preferred


def test_etfs_are_funds_with_unknown_category():
    listing = nl.load_listing(fetch=_fetch)
    rec = listing.get("SPY")
    assert rec.is_fund and rec.fund_category == "UNKNOWN" and rec.market_cap is None
    assert not listing.get("AAPL").is_fund


def test_fetch_failure_raises():
    def boom(url, headers, timeout_s):
        raise TimeoutError("slow")

    with pytest.raises(nl.ListingUnavailable):
        nl.load_listing(fetch=boom)


def test_second_endpoint_failure_raises():
    def half(url, headers, timeout_s):
        if url == nl.FUNDS_URL:
            raise OSError("500")
        return _load("stocks.json")

    with pytest.raises(nl.ListingUnavailable):
        nl.load_listing(fetch=half)


@pytest.mark.parametrize("body", [{"data": {"rows": []}}, {"data": None}, "garbage", []])
def test_empty_or_bad_rows_raise(body):
    with pytest.raises(nl.ListingUnavailable):
        nl.load_listing(fetch=lambda u, h, t: body)
