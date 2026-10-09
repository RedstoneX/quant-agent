"""Board item 70, the second split (2026-10-04).

One named constant, `NOISE_BAND_ATR_MULTIPLE`, was still doing TWO exit
jobs. Its midday home measures the adverse move from AVERAGE ENTRY and
widens by sqrt(trading sessions held); its `check_structural_protection`
fallback home measures the adverse move from the RUNNING EXTREME SINCE
ENTRY and passes no hold length, so its band is flat. Two anchors, one
time-scaled and one not, from one name.

These tests pin the split and, just as importantly, pin that it changed
NOTHING the desk does: the two constants are equal today, and the fallback
decides exactly as it did before. Hermetic -- no network, no broker, no
database.
"""

from __future__ import annotations

import inspect

from src.risk.exit_guard import (
    FALLBACK_PROTECTION_ATR_MULTIPLE,
    adverse_move_is_noise,
    check_structural_protection,
)


def _fallback_call(src: str) -> str:
    """The text of the fallback's `adverse_move_is_noise(...)` call."""
    start = src.index("is_noise = adverse_move_is_noise(")
    depth = 0
    for i in range(src.index("(", start), len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError("unbalanced call site")


def test_the_fallback_home_has_its_own_named_constant() -> None:
    """Two jobs, two names -- not one name read twice."""
    assert FALLBACK_PROTECTION_ATR_MULTIPLE is not None
    assert isinstance(FALLBACK_PROTECTION_ATR_MULTIPLE, float)


def test_no_behaviour_change_the_value_is_unchanged() -> None:
    """Removing the midday gate (2026-10-09) did not retune this home."""
    assert FALLBACK_PROTECTION_ATR_MULTIPLE == 1.0


def test_the_fallback_call_site_reads_the_fallback_constant() -> None:
    """The fallback must pass its OWN multiple, not the midday band's.

    Source inspection, because the equal values make the two
    indistinguishable from behaviour alone -- which is exactly why one name
    could do two jobs unnoticed for so long.
    """
    src = inspect.getsource(check_structural_protection)
    call = _fallback_call(src)
    assert "multiple=FALLBACK_PROTECTION_ATR_MULTIPLE" in call


def test_the_fallback_band_stays_flat_no_hold_length_is_passed() -> None:
    """This home's band does not widen with time."""
    src = inspect.getsource(check_structural_protection)
    call = _fallback_call(src)
    assert "days_held" not in call


def test_the_recorded_payload_names_the_fallback_multiple() -> None:
    """The settlement recording must report the multiple actually in force."""
    src = inspect.getsource(check_structural_protection)
    assert "band_multiple=FALLBACK_PROTECTION_ATR_MULTIPLE" in src


def test_the_fallback_decision_is_unchanged_at_the_boundary() -> None:
    """Behaviour proof: inside the band stays protected, outside lifts."""
    atr = 2.0
    width = FALLBACK_PROTECTION_ATR_MULTIPLE * atr

    def _check(price: float):
        return check_structural_protection(
            thesis_invalid_if=None,
            current_price=price,
            entry_price=100.0,
            stop_loss=90.0,
            atr=atr,
            computed_levels=[],
            min_level_touches=2,
            level_cluster_tolerance_pct=0.5,
        )

    inside = _check(100.0 - width * 0.5)
    outside = _check(100.0 - width * 1.5)
    assert inside.protected is True
    assert outside.protected is False
    # And the mechanism agrees with the same multiple read directly.
    assert adverse_move_is_noise(100.0, 100.0 - width * 0.5, atr, multiple=FALLBACK_PROTECTION_ATR_MULTIPLE) is True
