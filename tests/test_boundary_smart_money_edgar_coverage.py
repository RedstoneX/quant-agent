"""Witness for the standalone EDGAR coverage arithmetic."""

from src.data.smart_money_edgar_coverage import (
    UNVERIFIED_EDGAR_REASONS,
    blank_edgar_coverage,
    edgar_coverage,
    edgar_total,
    well_formed_hit,
)
from tests.boundary_harness import check_boundary


def test_part_source_never_names_the_provider_module():
    import src.data.smart_money_edgar_coverage as part

    with open(part.__file__) as handle:
        text = handle.read()
    assert "smart_money import" not in text and "src.data.smart_money\n" not in text


def test_part_passes_the_boundary_harness():
    verdict = check_boundary("src.data.smart_money_edgar_coverage")
    assert not verdict.failures, verdict.failures


def test_edgar_total_reads_count_and_refuses_garbage():
    assert edgar_total({"total": {"value": 12, "relation": "eq"}}) == 12
    assert edgar_total({"total": {"value": -1}}) is None
    assert edgar_total("junk") is None


def test_well_formed_hit_needs_a_dict_with_an_id():
    assert well_formed_hit(None) is False
    assert well_formed_hit("junk") is False


def test_blank_record_reads_unverified():
    blank = blank_edgar_coverage()
    assert blank["verified"] is False and blank["known"] is False
    assert blank["reasons"] == ["never_recorded"]


def test_coverage_of_handed_in_stats_is_verified_when_every_filing_is_walked():
    cov = edgar_coverage(
        {
            "edgar_total": 10,
            "edgar_enumerated": 10,
            "edgar_rows_received": 10,
            "edgar_days_queried": 5,
            "edgar_days_in_window": 5,
            "edgar_days_with_total": 5,
        }
    )
    assert cov["ratio"] == 1.0
    assert not (set(cov["reasons"]) & UNVERIFIED_EDGAR_REASONS)


def test_non_dict_stats_fall_back_to_the_blank_record():
    assert edgar_coverage(None) == blank_edgar_coverage()
