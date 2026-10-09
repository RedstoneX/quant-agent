"""Boundary witness: the gross ceiling and its de-levering ladder, split out of
`src/risk/rules.py` into `src/risk/gross_ladder.py`, builds and runs alone with
no pipeline, broker, database or RiskRuleEngine. Clause 5 of
tests/boundary_harness.py: this test imports the part and never names the
pipeline. Follows tests/test_risk_rules_parts_boundary.py.
"""

from __future__ import annotations

from src.risk import gross_ladder, rules
from tests.boundary_harness import check_boundary


def test_gross_ladder_part_fails_only_the_dataclass_init_clause():
    verdict = check_boundary("src.risk.gross_ladder")
    assert set(verdict.failures) <= {1}, verdict.failures


def test_gross_ladder_resolves_a_ceiling_with_no_engine():
    ceiling = gross_ladder.resolve_gross_ceiling(-9.0, base_x=2.0)
    assert isinstance(ceiling, gross_ladder.GrossCeiling)
    assert ceiling.ceiling_x == 1.5
    assert gross_ladder.resolve_gross_ceiling(None, base_x=2.0).alert_owner


def test_rules_still_reexports_the_moved_names():
    assert rules.GROSS_LADDER is gross_ladder.GROSS_LADDER
    assert rules.GROSS_LADDER_ALERT_PCT == gross_ladder.GROSS_LADDER_ALERT_PCT
    assert rules.GrossCeiling is gross_ladder.GrossCeiling
    assert rules.resolve_gross_ceiling is gross_ladder.resolve_gross_ceiling
