"""One borrow reader: borrow_status first, easy_to_borrow only as fallback, else fail closed."""

from unittest.mock import MagicMock

from src.execution.broker_parts.account_asset_eligibility import AssetEligibilityReads


class _Reads(AssetEligibilityReads):
    def __init__(self, record):
        self.client = MagicMock()
        self._shortable_cache = {}
        self._record = record

    def get_asset_record(self, symbol):
        return self._record


def _borrow(record):
    return _Reads({"shortable": True, **record}).get_shortability("AAPL")


def test_borrow_status_easy_ok():
    r = _borrow({"borrow_status": "easy_to_borrow", "easy_to_borrow": False})
    assert r["easy_to_borrow"] and r["reason"] == "eligible"


def test_borrow_status_hard_refused():
    r = _borrow({"borrow_status": "hard_to_borrow", "easy_to_borrow": True})
    assert not r["easy_to_borrow"] and r["reason"] == "hard_to_borrow"


def test_legacy_fallback_when_status_absent():
    r = _borrow({"easy_to_borrow": True})
    assert r["easy_to_borrow"] and r["reason"] == "eligible"


def test_both_absent_refused_with_reason():
    r = _borrow({})
    assert not r["easy_to_borrow"]
    assert "missing" in r["reason"] and "borrow_status" in r["reason"]


def test_lookup_missing_fails_closed():
    assert _Reads(None).get_shortability("AAPL")["reason"] == "asset_lookup_failed"
