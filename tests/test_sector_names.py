import re
from pathlib import Path

import pytest

from src.data.sector_names import NASDAQ_TO_DESK, UNCLASSIFIED, nasdaq_to_desk
from src.sector_reference import _canonicalize_sector
from src.sector_vocab import _ALLOWED_SECTORS

FIX = Path(__file__).parent / "fixtures" / "nasdaq_listing"


def _fixture_sectors():
    out = set()
    for f in ("stocks.json", "etf.json"):
        out |= set(re.findall(r'"sector":\s*"([^"]*)"', (FIX / f).read_text()))
    return {s for s in out if s}


def test_fixture_sector_names_translate():
    names = _fixture_sectors()
    assert {"Finance", "Energy", "Technology"} <= names
    for n in names:
        assert _canonicalize_sector(n) in set(_ALLOWED_SECTORS), n


def test_every_map_value_is_canonical_or_named_unclassified():
    for k, v in NASDAQ_TO_DESK.items():
        assert v in _ALLOWED_SECTORS or v == UNCLASSIFIED, k


@pytest.mark.parametrize("raw", ["Finance", "finance", " Finance "])
def test_finance_is_financial_services(raw):
    assert _canonicalize_sector(raw) == "Financial Services"


def test_health_care_and_telecom():
    assert _canonicalize_sector("Health Care") == "Healthcare"
    assert _canonicalize_sector("Telecommunications") == "Communication Services"


def test_miscellaneous_is_named_unclassified():
    assert nasdaq_to_desk("Miscellaneous") == UNCLASSIFIED == "Unknown"
    assert _canonicalize_sector("Miscellaneous") == "Unknown"


@pytest.mark.parametrize("name", list(_ALLOWED_SECTORS))
def test_yahoo_names_unchanged(name):
    assert _canonicalize_sector(name) == name


def test_unknown_string_still_unknown():
    assert _canonicalize_sector("Zorp") == "Unknown"
    assert _canonicalize_sector(None) == "Unknown"
    assert nasdaq_to_desk("Zorp") is None
