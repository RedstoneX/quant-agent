"""The route-admissibility report finds the two defects it claims to find."""

from __future__ import annotations

from pathlib import Path

from scripts.measure_ledger_route_admissibility import (
    DEFAULT_LEDGER,
    count_routes,
    inspect,
    is_arbitrary,
    report,
    split_entries,
)

_CLEAN = """numbers:
  - id: a.clean.row
    settles_by:
      kind: measurement
      closes_when: the distribution separates and the value is read off it
    value: 1
    status: arbitrary
"""

_EXTREME = """numbers:
  - id: a.extreme.row
    settles_by:
      kind: measurement
      closes_when: the worst observed gap multiple still fits the envelope
    value: 1
    status: arbitrary
"""

_APPETITE = """numbers:
  - id: a.appetite.row
    settles_by:
      kind: ratified-bound
      closes_when: the owner states the dollar loss he will accept per name
    value: 1
    status: arbitrary
"""

_DUPLICATE = """numbers:
  - id: a.duplicated.row
    settles_by:
      kind: measurement
      closes_when: the newer route, written above the older one
    value: 1
    status: sourced
    settles_by:
      kind: measurement
      closes_when: the older route, which YAML keeps because it is last
"""


def test_clean_row_produces_no_finding() -> None:
    assert inspect(_CLEAN) == []


def test_extreme_closing_condition_is_barred() -> None:
    kinds = [f.kind for f in inspect(_EXTREME)]
    assert kinds == ["BARRED-EXTREME"]


def test_owner_appetite_closing_condition_is_barred() -> None:
    kinds = [f.kind for f in inspect(_APPETITE)]
    assert kinds == ["BARRED-APPETITE"]


def test_duplicate_route_is_flagged_even_when_not_arbitrary() -> None:
    findings = inspect(_DUPLICATE)
    assert [f.kind for f in findings] == ["DEAD-ROUTE"]
    assert "1 recorded route(s) are silently dropped" in findings[0].detail


def test_a_sourced_row_is_not_judged_for_admissibility() -> None:
    sourced = _EXTREME.replace("status: arbitrary", "status: sourced")
    assert inspect(sourced) == []


def test_split_entries_and_helpers_agree_with_the_raw_text() -> None:
    entries = split_entries(_DUPLICATE)
    assert [row_id for row_id, _ in entries] == ["a.duplicated.row"]
    assert count_routes(entries[0][1]) == 2
    assert not is_arbitrary(entries[0][1])


def test_report_says_so_when_there_is_nothing_to_say() -> None:
    assert "No inadmissible" in report([])


def test_the_live_ledger_still_carries_the_two_barred_envelope_routes() -> None:
    """max_position_risk_pct records two routes and NEITHER can settle it."""
    text = Path(DEFAULT_LEDGER).read_text()
    kinds = {
        f.kind
        for f in inspect(text)
        if f.row_id == "src.config.RiskConfig.max_position_risk_pct"
    }
    assert kinds == {"BARRED-EXTREME", "BARRED-APPETITE", "DEAD-ROUTE"}
