"""Risk-narrative tolerance is the emitted field's own precision, not a fixed gap."""

from src.models import TargetPosition


def _target(thesis: str, risk_pct: float) -> TargetPosition:
    return TargetPosition(
        symbol="NVDA", risk_allocation_pct=risk_pct, conviction="high",
        thesis=thesis,
    )


def test_risk_narrative_within_tolerance_not_flagged():
    """A prose claim that ROUNDS to the field as the field was emitted is the
    same number said differently, not a mismatch: a field of 2.0 asserts a
    tenth-of-a-point value, so [1.95, 2.05) is that same number."""
    t = _target("Risking about 1.95% on this name.", 2.0)
    assert t.risk_narrative_mismatch is False


def test_risk_narrative_quarter_sizing_gap_is_flagged():
    """The absolute 0.5pp tolerance this check used until 2026-10-05 let
    prose "2.5% risk" sit beside an emitted 2.0 unflagged -- a 25% sizing
    difference stated to the owner as if the two agreed."""
    t = _target("High conviction entry, risking 2.5% of the book here.", 2.0)
    assert t.risk_narrative_mismatch is True
    assert "2.5%" in t.risk_narrative_mismatch_detail
    assert t.risk_allocation_pct == 2.0


def test_risk_narrative_small_position_relative_gap_is_flagged():
    """The same absolute slack was worst on the smallest positions, where
    0.5pp is most of the position: prose "0.6% risk" beside an emitted 0.2
    is a 3x sizing difference and used to pass unseen."""
    t = _target("Small starter here, risking 0.6% of the book.", 0.2)
    assert t.risk_narrative_mismatch is True
    assert "0.6%" in t.risk_narrative_mismatch_detail


def test_risk_narrative_tolerance_matches_the_sizing_logic_surface():
    """Both risk-narrative surfaces answer "same number?" with one shared
    derivation, so neither can drift into calling a gap a mismatch the other
    calls a rounding."""
    from src.models import risk_pct_half_ulp
    import src.risk_narrative_check as rnc

    assert rnc.risk_pct_half_ulp is risk_pct_half_ulp
    assert float(risk_pct_half_ulp(2.0)) == 0.05
    assert float(risk_pct_half_ulp(0.25)) == 0.005
