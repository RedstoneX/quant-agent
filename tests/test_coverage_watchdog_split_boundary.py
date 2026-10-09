"""Boundary witnesses: the four parts split out of `src/coverage_watchdog.py` build
and run alone -- no pipeline, no broker, no database; a plain state dict or a
temp file is all they need.

`src/coverage_watchdog.py` keeps the checks and re-exports every moved name;
`models` holds the five frozen dataclasses, `claims_repair`, `claims_other`
the once-a-day alert claims and `awaiting_print` the tape-has-not-printed
marker, all moved verbatim. Clause 5 of tests/boundary_harness.py: each part
has a test that imports it and never names the pipeline.
"""

from __future__ import annotations

import pytest

from src import coverage_watchdog
from src.coverage_watchdog_parts import awaiting_print, claims_other, claims_repair, models
from tests.boundary_harness import check_boundary

FUNCTION_ONLY_PARTS = [
    "src.coverage_watchdog_parts.claims_repair",
    "src.coverage_watchdog_parts.claims_other",
    "src.coverage_watchdog_parts.awaiting_print",
]


@pytest.mark.parametrize("module", FUNCTION_ONLY_PARTS)
def test_part_passes_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_models_part_fails_only_the_dataclass_init_clause():
    # Frozen dataclasses generate their __init__, which clause 1 cannot see.
    verdict = check_boundary("src.coverage_watchdog_parts.models")
    assert set(verdict.failures) <= {1}, verdict.failures


def test_every_moved_name_is_still_importable_from_the_original_module():
    for part in (models, claims_repair, claims_other, awaiting_print):
        for name, obj in vars(part).items():
            if getattr(obj, "__module__", None) == part.__name__:
                assert getattr(coverage_watchdog, name) is obj, name
    assert coverage_watchdog.AWAITING_FIRST_PRINT_CODES is awaiting_print.AWAITING_FIRST_PRINT_CODES


def test_exit_declined_claim_runs_from_a_temp_file_and_fires_once_a_day(tmp_path):
    path = tmp_path / "state.json"
    assert claims_other.claim_exit_declined_alert(["aapl", "msft"], path=path) == ["AAPL", "MSFT"]
    assert claims_other.claim_exit_declined_alert(["AAPL"], path=path) == []


def test_alerted_symbol_readers_work_on_a_bare_dict():
    state = {"unguarded_alerted_symbols": {"day": "2026-10-08", "symbols": [" abc ", ""]}}
    assert claims_other._unguarded_alerted_symbols(state, "2026-10-08") == {"ABC"}
    assert claims_other._unguarded_alerted_symbols(state, "2026-10-09") == set()
    assert claims_repair._exposure_alerted_symbols({}, "2026-10-08") == set()


def test_awaiting_print_state_round_trips_through_a_bare_dict():
    state: dict = {}
    awaiting_print.note_awaiting_first_print("aapl", state=state)
    assert awaiting_print.session_awaiting_print_symbols(state=state) == {"AAPL"}
    awaiting_print.clear_awaiting_first_print(["aapl"], state=state)
    assert awaiting_print.session_awaiting_print_symbols(state=state) == set()
