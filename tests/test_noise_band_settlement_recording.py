"""Board item 70 — the noise band's settlement recording must not be censored.

The entry-anchored ATR noise band is `arbitrary`: `NOISE_BAND_ATR_MULTIPLE`
is 1.0 with no published measurement of the quantity it bounds (the adverse
move at which a move from entry stops being ordinary daily wobble). Item 70's
2026-09-30 pass added a machine-readable settlement recording so the number
could one day be settled on the desk's own evidence instead of re-searched.

Measured against production on 2026-10-01 that recording had produced ZERO
observations, and the structural reason is visible in the code rather than in
the data: the band was written to the durable record ONLY on the branch where
it BLOCKED an exit. A sample truncated at exactly the threshold under
examination is the one sample that can never locate that threshold — every
adverse move the band judged large enough to let through was discarded, so the
record could only ever show moves smaller than 1.0 ATR and would "confirm" any
multiple whatsoever.

These tests pin the two recording gaps:

  1. the midday reviewer (`src/pipeline_exits.py`) must write the observation
     on BOTH outcomes, tagged `blocked=true|false`, with the adverse move also
     expressed as an ATR multiple — the unit the constant is denominated in;

  2. the band's SECOND home, the `check_structural_protection` fallback in
     `src/risk/exit_guard.py`, must emit the same `rule=atr_noise_band`
     payload the break margin already emits. That home applies a FLAT 1.0 ATR
     band (it passes no `days_held`) while the reviewer's widens with
     sqrt(sessions held), so the same named constant yields two different
     widths for one holding on one day — a fact no record stated until now.

RECORDING ONLY. No protection decision and no sell decision changes here; the
`blocked` / `inside_band` flags are the identical predicate calls the previous
code made, and both tests assert the decisions still come out as before.
"""

from unittest.mock import MagicMock

from src.models import Position, PositionAction, PositionReasoningChain, PositionReview
from tests.pipeline_factory import build_pipeline
from src.risk.exit_guard import (
    NOISE_BAND_ATR_MULTIPLE,
    StructuralProtectionCheck,
    check_structural_protection,
)


def _position(symbol="AAA", qty=10, avg_entry=100.0, current_price=110.0):
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=qty * (current_price - avg_entry),
        sector="Technology",
    )


def _pipeline():
    from src.trading_calendar import trading_sessions_held as _weekday_sessions_held

    p = build_pipeline(db=MagicMock(), broker=MagicMock())
    p.broker.get_current_stop_price.return_value = None
    p.broker.trading_sessions_held.side_effect = _weekday_sessions_held
    p._atr_for_symbol = MagicMock(return_value=2.0)
    return p


def _review_with(action="SELL", symbol="AAA", reason="thesis_invalid triggered"):
    return PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="stable",
            thesis_progress_check="broken",
            thesis_integrity_check="invalidation hit",
            winners_discipline_check="n/a",
            session_disposition_check="midday",
            execution_rationale="exit",
        ),
        actions=[PositionAction(action=action, symbol=symbol, reason=reason)],
        overall_assessment="one exit",
        risk_level="moderate",
    )


def _band_rows(db):
    """Every durable row this run wrote that carries the band's payload."""
    rows = []
    for call in db.record_intraday_evaluation.call_args_list:
        detail = call.kwargs.get("detail") or ""
        if "rule=atr_noise_band" in detail:
            rows.append((call.kwargs.get("status"), detail))
    return rows


# ---------------------------------------------------------------------------
# Home 1: the midday reviewer. The uncensored half is the new assertion.
# ---------------------------------------------------------------------------


def test_an_adverse_move_that_clears_the_band_is_still_recorded():
    """THE CENSORED HALF. Entry 100, price 94, ATR 2 -> a 6.00 adverse move,
    3.0 ATR, far outside a 1.0 ATR band. The exit proceeds (unchanged), and
    the observation must now reach the durable record anyway: this is exactly
    the kind of reading that was being thrown away, and exactly the kind that
    would tell the desk whether 1.0 sits anywhere sensible."""
    pipeline = _pipeline()
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)

    pipeline._midday_execute_llm_actions(
        positions=[_position("AAA", qty=10, avg_entry=100.0, current_price=94.0)],
        review=_review_with(
            symbol="AAA",
            reason="thesis_invalid triggered — lost the level",
        ),
        run_id="r1",
    )

    rows = _band_rows(pipeline.db)
    assert rows, "the band evaluation left no durable row at all"
    status, detail = rows[0]
    assert status == "exit_noise_band_evaluated_not_blocked"
    assert "blocked=false" in detail
    # 6.00 / 2.0 == 3.0 ATR, stated in the constant's own unit.
    assert "adverse_atr_multiple=3.0000" in detail
    assert "band_multiple=1.0000" in detail


def test_a_blocked_exit_keeps_its_status_and_gains_the_atr_multiple():
    """The blocking branch must be untouched in behaviour and in its status
    string — the only durable change is the two extra fields."""
    pipeline = _pipeline()
    pipeline._atr_for_symbol = MagicMock(return_value=1.6)

    orders = pipeline._midday_execute_llm_actions(
        positions=[_position("OKLO", qty=25, avg_entry=42.59, current_price=41.51)],
        review=_review_with(
            symbol="OKLO",
            reason="thesis_invalid triggered — lost the level",
        ),
        run_id="r1",
    )

    assert orders == []
    rows = _band_rows(pipeline.db)
    assert rows
    status, detail = rows[0]
    assert status == "exit_blocked_inside_atr_noise_band"
    assert "blocked=true" in detail
    # 1.08 / 1.6 == 0.675 ATR.
    assert "adverse_atr_multiple=0.6750" in detail


def test_both_outcomes_are_written_in_the_same_shape():
    """The two rows must be comparable field for field, or the sample is
    still useless: the whole point is to put blocked and not-blocked
    observations on one axis."""
    fields = (
        "rule=atr_noise_band",
        "blocked=",
        "adverse=",
        "adverse_atr_multiple=",
        "entry=",
        "price=",
        "atr14=",
        "band_multiple=",
        "band_width=",
        "sessions_held=",
        "sessions_measured=",
    )
    details = []
    for entry, price, atr in ((100.0, 94.0, 2.0), (42.59, 41.51, 1.6)):
        pipeline = _pipeline()
        pipeline._atr_for_symbol = MagicMock(return_value=atr)
        pipeline._midday_execute_llm_actions(
            positions=[_position("AAA", qty=10, avg_entry=entry, current_price=price)],
            review=_review_with(symbol="AAA", reason="thesis_invalid triggered"),
            run_id="r1",
        )
        rows = _band_rows(pipeline.db)
        assert rows
        details.append(rows[0][1])

    assert len(details) == 2
    for detail in details:
        for field in fields:
            assert field in detail, f"{field} missing from {detail[:120]}"


# ---------------------------------------------------------------------------
# Home 2: the structural-protection fallback.
# ---------------------------------------------------------------------------


def _fallback(entry: float, price: float, atr: float) -> StructuralProtectionCheck:
    """No thesis_invalid_if and no qualifying level -> the noise-band
    fallback is the only thing left to judge the holding."""
    return check_structural_protection(
        thesis_invalid_if=None,
        current_price=price,
        entry_price=entry,
        stop_loss=price * 0.9,
        atr=atr,
        computed_levels=[],
        computed_level_touches={},
        min_level_touches=5,
        level_cluster_tolerance_pct=0.5,
    )


def test_the_fallback_records_the_band_it_actually_applied():
    """Entry 100, price 99, ATR 2 -> 0.5 ATR adverse, inside the band, still
    protected (unchanged). The detail must now say so in machine-readable
    form, including that this home's band does NOT widen with hold length."""
    result = _fallback(100.0, 99.0, 2.0)

    assert result.protected is True
    assert result.basis == "noise_band_intact"
    assert "rule=atr_noise_band" in result.detail
    assert "home=structural_protection_fallback" in result.detail
    assert "adverse_atr_multiple=0.5" in result.detail
    assert "inside_band=true" in result.detail
    assert "band_scales_with_hold_length=false" in result.detail


def test_the_fallback_records_the_breach_too():
    """Entry 100, price 94, ATR 2 -> 3.0 ATR adverse, protection lifts
    (unchanged), and the observation is recorded rather than discarded."""
    result = _fallback(100.0, 94.0, 2.0)

    assert result.protected is False
    assert result.basis == "noise_band_broken"
    assert "rule=atr_noise_band" in result.detail
    assert "adverse_atr_multiple=3" in result.detail
    assert "inside_band=false" in result.detail


def test_the_two_homes_disagree_about_the_band_width_and_now_say_so():
    """Not a style point. The reviewer widens the band by sqrt(sessions
    held); this fallback passes no hold length, so it stays flat at
    NOISE_BAND_ATR_MULTIPLE forever. A 9-session holding is judged against
    3.0 ATR in one home and 1.0 ATR in the other under one constant's name.
    Until both payloads existed, nothing in the record could show that."""
    from src.risk.exit_guard import noise_band_atr

    assert noise_band_atr(9) == 3.0
    assert NOISE_BAND_ATR_MULTIPLE == 1.0

    detail = _fallback(100.0, 99.0, 2.0).detail
    assert f"band_multiple={NOISE_BAND_ATR_MULTIPLE:g}" in detail
    assert "band_scales_with_hold_length=false" in detail
