"""The two PIVOT_WINDOWs are independent, and the comments must say so.

`src/risk/trailing.py::PIVOT_WINDOW` is 3. `src/data/levels.py::PIVOT_WINDOW`
is 5. Until 2026-09-13 the comment beside the first claimed it matched the
second "so 'a higher low' means the same thing in both places". It never did.

Item 45 (WORK.md) resolved as: no published source derives a window, so do
NOT pick one — adopt the archetype (a bar that strictly dominates N bars on
both sides, verdict N bars late), keep each module's existing convention, and
make the comments honest. These tests pin that outcome, so a future reader
cannot quietly re-introduce the false equivalence or "tidy" the two constants
into agreement without deleting a test that says why not.
"""

from __future__ import annotations

import datetime as _dt
import inspect
from pathlib import Path

from src.data.levels import PIVOT_WINDOW as LEVELS_WINDOW, _find_pivots
from src.models import OHLCV
from src.risk.trailing import PIVOT_WINDOW as TRAILING_WINDOW, _swing_lows

import src.data.levels as levels_mod
import src.risk.trailing as trailing_mod


def _bars(lows: list[float]) -> list[OHLCV]:
    """Daily bars whose highs sit a flat 2 above each low."""
    start = _dt.date(2026, 1, 5)
    return [
        OHLCV(
            date=start + _dt.timedelta(days=i),
            open=lo + 1, high=lo + 2, low=lo, close=lo + 1, volume=1_000,
        )
        for i, lo in enumerate(lows)
    ]


# A V exactly 3 bars wide on each side, sitting inside a wider descent so the
# 4th and 5th bars out are LOWER than the pivot. Window 3 confirms it; window
# 5 does not. Index 5 is the candidate, at 96.
_THREE_WIDE_V = [90, 91, 102, 101, 100, 96, 100, 101, 102, 91, 90]


# ---------------------------------------------------------------------------
# The constants themselves


def test_the_two_windows_are_deliberately_different():
    """Not a bug to be fixed by copying. Item 45's explicit instruction."""
    assert TRAILING_WINDOW == 3
    assert LEVELS_WINDOW == 5
    assert TRAILING_WINDOW != LEVELS_WINDOW


# ---------------------------------------------------------------------------
# The behavioural consequence, measured rather than asserted


def test_window_3_confirms_a_swing_low_window_5_rejects():
    """The disagreement is real: same bars, different verdicts."""
    bars = _bars(_THREE_WIDE_V)

    assert _swing_lows(bars) == [96.0]

    supports = [p for p in _find_pivots(bars, LEVELS_WINDOW) if p[2] == "S"]
    assert supports == []


def test_window_5_lows_are_a_subset_of_window_3_lows_never_the_reverse():
    """Item 45 said the two can each see a low the other misses ("and vice
    versa"). Measured, that is only half true for interior bars: a bar that
    dominates 5 bars either side necessarily dominates 3, so every
    `levels.py` support pivot is also a `trailing.py` swing low. The
    asymmetry runs one way, which is why the looser window is the trailing
    one and why nothing here is a safety gap.
    """
    lows = [120, 118, 116, 114, 112, 100, 112, 114, 116, 118, 120,   # wide V
            130,
            90, 91, 102, 101, 100, 96, 100, 101, 102, 91, 90,        # narrow V
            130]
    bars = _bars(lows)

    window5 = {p[1] for p in _find_pivots(bars, LEVELS_WINDOW) if p[2] == "S"}
    window3 = set(_swing_lows(bars))

    assert window5, "fixture must contain at least one window-5 support pivot"
    assert window5 <= window3
    assert window3 - window5, "fixture must show the looser window seeing more"


# ---------------------------------------------------------------------------
# No shared consumer — the reason the disagreement is currently harmless


def test_no_module_imports_both_pivot_windows():
    """Nothing downstream compares one module's swing low to the other's.

    If this ever fails, the disagreement has stopped being harmless and item
    45 must be reopened as a real defect rather than a comment fix.
    """
    src = Path(trailing_mod.__file__).resolve().parent.parent
    offenders = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "risk.trailing import" in text and "data.levels import" in text:
            if "PIVOT_WINDOW" in text:
                offenders.append(str(path.relative_to(src.parent)))
    assert offenders == [], f"a shared consumer appeared: {offenders}"


def test_pivot_window_is_not_a_public_knob_on_the_trailing_path():
    """`levels.py` exposes `pivot_window` as a call argument; `trailing.py`'s
    `compute_trailing_stop` does not, so no caller can accidentally pass one
    module's window into the other.
    """
    trailing_params = inspect.signature(
        trailing_mod.compute_trailing_stop
    ).parameters
    assert "pivot_window" not in trailing_params

    levels_params = inspect.signature(levels_mod.find_structural_levels).parameters
    assert levels_params["pivot_window"].default == LEVELS_WINDOW


# ---------------------------------------------------------------------------
# The comments must stay honest


def _comment_block(path: Path, marker: str) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.startswith(marker))
    out = []
    for ln in reversed(lines[:idx]):
        if ln.startswith("#"):
            out.append(ln)
        elif ln.strip() == "":
            continue
        else:
            break
    return "\n".join(reversed(out))


def test_trailing_comment_no_longer_claims_the_windows_match():
    block = _comment_block(Path(trailing_mod.__file__), "PIVOT_WINDOW = 3")
    # The old claim may only survive as history, explicitly marked false.
    assert "Matches `src/data/levels.py`" not in block
    assert "was false the day it was" in block
    assert "convention with no derivation" in block
    # The sources actually fetched must stay cited next to the number.
    assert "ta-lib.org/functions/fractal.html" in block
    assert "luxalgo.com/library/concept/swing-high-low" in block
    # And the consumer must be named, so a future reader can check the
    # "no shared consumer" claim without re-deriving it.
    assert "compute_trailing_stop" in block


def test_levels_comment_states_it_is_a_convention_and_names_its_consumers():
    block = _comment_block(Path(levels_mod.__file__), "PIVOT_WINDOW = 5")
    assert "convention with no derivation" in block
    assert "find_structural_levels" in block
    assert "structure_coverage" in block
    # Must say plainly that trailing.py differs, so nobody "fixes" it.
    assert "src/risk/trailing.py" in block
