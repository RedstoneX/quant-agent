"""Direct, pipeline-free exercise of the target_revision lifts: a close that has
run past every level on the chart is refused as "no ceiling left" by the shared
re-derivation, and the bug-fix backfill reaches the same body."""
from src.risk.target_revision_backfill import assess_bugfix_backfill
from src.risk.target_revision_basis import REVISION_NO_CEILING_LEFT
from src.risk.target_revision_rederive import _rederive_on_todays_bars


def test_rederive_refuses_when_close_is_past_every_level():
    out = _rederive_on_todays_bars(
        sym="TEST", direction="long", is_short=False, entry=100.0, target=110.0,
        target_level=110.0, horizon=10, setup_type=None, levels=[104.0, 108.0],
        vol=1.0, close=130.0, levels_coverage="", trigger="level_break",
        sessions_held=3, allow_reanchor=False, min_target_atr_multiple=1.0,
        breakout_projection_atr_multiple=1.0, max_reach_atr_multiple=10.0,
        max_horizon_sessions=20, break_margin_atr_multiple=0.5,
    )
    assert out.code == REVISION_NO_CEILING_LEFT
    assert out.prior_price == 110.0


def test_backfill_reaches_the_same_refusal():
    out = assess_bugfix_backfill(
        symbol="test", direction="long", entry_price=100.0, stored_target=110.0,
        pinned_horizon_sessions=10, setup_type=None, levels=[104.0, 108.0],
        atr=1.0, close_price=130.0,
    )
    assert out.symbol == "TEST"
    assert out.code == REVISION_NO_CEILING_LEFT
