"""The "is this stop AT that level" tolerance is the level zone's OWN width.

docs/WORK.md item 46, fixed 2026-09-13. These tests exist to stop one
specific class of regression: a tolerance expressed in one unit drifting
away from the thing it claims to cover, expressed in another.

WHAT WENT WRONG. `risk.level_match_atr_tolerance` was `0.25`, i.e. a stop
counted as sitting AT a computed level if it was within 0.25 ATR of it, and
its config comment justified 0.25 by saying a computed level is a ZONE
(`find_structural_levels` clusters pivots within `CLUSTER_TOLERANCE_PCT` =
1% of price into one level) and that the tolerance therefore had to be "at
least that wide". Those are different units, so the justification was never
a statement about the setting — it was a statement about the instrument:

    0.25 * ATR >= 0.01 * price   <=>   ATR >= 4.0% of price

The claim only held on names whose ATR is at least 4% of price. Every
quieter name got a tolerance NARROWER than the zone, so a stop sitting
inside a level's real zone was not recognised as level-backed, was widened
to the ATR band instead, and the trade's reward:risk was judged against a
stop that had moved off the structure it was placed on (item 1's geometry).

THE FIX, AND WHY IT IS NOT ANOTHER NUMBER. No ATR multiple can be correct
here, because the ratio between an ATR multiple and a percentage of price
is a different number for every name on every day. So the constant is gone
rather than re-tuned: the match tolerance is read from
`src.data.levels.level_zone_halfwidth`, which derives it from the very
`CLUSTER_TOLERANCE_PCT` that built the zone. One number, one unit, one
file. `test_no_atr_multiple_survives_anywhere` is the mechanical guard.

The ATR question was not deleted, it was put back where it belongs: whether
a stop is far enough out to survive the name's noise is still
`min_stop_atr_multiple` / `absolute_min_stop_atr_multiple`, and whether a
level has BROKEN is still `NOISE_BAND_ATR_MULTIPLE`. Both are genuinely
volatility questions. "Which level is this stop on" is an identity question
about a zone, and is answered in the zone's unit.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest

from src.config import RiskConfig
from src.data.levels import (
    CLUSTER_TOLERANCE_PCT,
    _cluster,
    find_structural_levels,
    level_zone_halfwidth,
)
from src.models import OHLCV
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor
from src.risk.exit_guard import _structural_level_backing_stop

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. The relationship itself: the tolerance covers the zone, at EVERY price
#    and for EVERY volatility, because volatility is no longer an input.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("price", [1.0, 7.5, 42.37, 100.0, 1234.56, 9999.0])
def test_tolerance_is_exactly_the_zone_width(price):
    assert level_zone_halfwidth(price) == pytest.approx(
        price * CLUSTER_TOLERANCE_PCT / 100.0
    )


def test_tolerance_tracks_the_cluster_constant_not_a_copy_of_it():
    """Change `CLUSTER_TOLERANCE_PCT` and the tolerance moves with it.

    This is the whole point of the fix. Under the old ATR multiple, widening
    the clustering zone would have silently left the matcher behind.
    """
    widened = CLUSTER_TOLERANCE_PCT * 3
    assert level_zone_halfwidth(200.0, widened) == pytest.approx(
        level_zone_halfwidth(200.0) * 3
    )


def test_tolerance_covers_every_pivot_the_clusterer_would_have_merged():
    """The bound is provable, not asserted — check it against `_cluster`.

    `_cluster` chains a pivot in when it is within `CLUSTER_TOLERANCE_PCT`
    of the group's ANCHOR (its lowest member, since pivots are sorted
    ascending), so a cluster spans at most `anchor * pct/100`. `Level.price`
    is the cluster MEAN, which is >= anchor. Therefore the furthest real
    pivot in a zone is never more than `price * pct/100` from the reported
    level. This test builds the worst case the clusterer can actually
    produce and confirms the tolerance reaches every member of it.
    """
    anchor = 100.0
    edge = anchor * (1 + CLUSTER_TOLERANCE_PCT / 100.0)
    pivots = [(0, anchor, "S"), (1, (anchor + edge) / 2, "S"), (2, edge, "S")]

    clusters = _cluster(pivots, CLUSTER_TOLERANCE_PCT)
    assert len(clusters) == 1, "fixture must be ONE zone for the test to mean anything"

    level_price = sum(p[1] for p in clusters[0]) / len(clusters[0])
    tolerance = level_zone_halfwidth(level_price)
    for _, pivot_price, _ in clusters[0]:
        assert abs(pivot_price - level_price) <= tolerance


def test_zero_and_nonsense_prices_yield_no_tolerance():
    assert level_zone_halfwidth(0.0) == 0.0
    assert level_zone_halfwidth(-10.0) == 0.0
    assert level_zone_halfwidth(float("nan")) == 0.0
    assert level_zone_halfwidth(float("inf")) == 0.0


# ---------------------------------------------------------------------------
# 2. The defect, reproduced against the arithmetic that caused it.
# ---------------------------------------------------------------------------

OLD_ATR_MULTIPLE = 0.25  # the deleted `risk.level_match_atr_tolerance`
QUOTED_MEDIAN_ATR_PCT = 2.56  # docs/QAMC_REMEDIATION_SPEC.md, measured 2026-08-27


def test_old_multiple_undercovered_the_zone_at_the_quoted_median_atr():
    """The item's arithmetic, verified rather than repeated.

    NOTE ON `QUOTED_MEDIAN_ATR_PCT`: it is a snapshot of the live book on
    2026-08-27 recorded in prose in docs/QAMC_REMEDIATION_SPEC.md, with no
    reproducible artifact in this repo. It is used here ONLY to reproduce
    the historical defect and is load-bearing for nothing that ships — the
    fix is deliberately independent of what the median ATR is, which is the
    point of the next test.
    """
    price = 100.0
    old_tolerance = OLD_ATR_MULTIPLE * (QUOTED_MEDIAN_ATR_PCT / 100.0 * price)
    zone = level_zone_halfwidth(price)

    assert old_tolerance == pytest.approx(0.64)
    assert zone == pytest.approx(1.0)
    assert old_tolerance < zone
    assert zone / old_tolerance == pytest.approx(1.5625, rel=1e-6)


def test_old_multiple_only_covered_the_zone_above_four_percent_atr():
    """`0.25 * ATR >= 0.01 * price` <=> `ATR >= 4% of price`. The crossover."""
    price = 100.0
    zone = level_zone_halfwidth(price)
    crossover_atr_pct = CLUSTER_TOLERANCE_PCT / OLD_ATR_MULTIPLE
    assert crossover_atr_pct == pytest.approx(4.0)

    for atr_pct, covers in ((1.5, False), (2.56, False), (4.0, True), (9.0, True)):
        old_tolerance = OLD_ATR_MULTIPLE * (atr_pct / 100.0 * price)
        assert (old_tolerance >= zone - 1e-12) is covers, atr_pct


def test_new_tolerance_is_independent_of_volatility():
    """The fix is right for all four of those names, not three of them.

    Nothing in the matcher takes an ATR any more, so there is no volatility
    at which the tolerance can fall short of the zone.
    """
    level_price, stop = 100.0, 99.5  # inside the 1% zone, 0.5 away
    for atr in (0.5, 1.5, 2.56, 4.0, 9.0):  # 0.5%..9% of price
        matched = _structural_level_backing_stop(
            entry_price=105.0, stop_loss=stop, is_short=False,
            computed_levels=[level_price],
            computed_level_touches={level_price: 5},
            min_level_touches=5,
            level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
        )
        assert matched == level_price, f"unmatched at atr={atr}"


# ---------------------------------------------------------------------------
# 3. The two implementations agree. `src/risk/exit_guard.py` restates the
#    rule because it is deliberately stdlib-only; a copy is only safe if
#    something checks it.
# ---------------------------------------------------------------------------

def _constructor():
    return PortfolioConstructor(ConstructorConfig(min_level_touches_for_stop_honor=5))


class _Analysis:
    def __init__(self, levels, touches):
        self.computed_levels = levels
        self.computed_level_touches = touches
        self.atr_14 = 2.0


@pytest.mark.parametrize("gap", [0.0, 0.5, 0.99, 1.0, 1.01, 1.5, 5.0])
def test_constructor_and_exit_guard_match_identically(gap):
    """Straddles the 1% boundary at price 100 — inside, on it, and outside."""
    level_price = 100.0
    stop = level_price - gap
    entry = 110.0
    touches = {level_price: 5}

    from_constructor = _constructor()._level_backing_stop(
        _Analysis([level_price], touches), entry, stop, False,
    )
    from_exit_guard = _structural_level_backing_stop(
        entry_price=entry, stop_loss=stop, is_short=False,
        computed_levels=[level_price], computed_level_touches=touches,
        min_level_touches=5,
        level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
    )
    assert from_constructor == from_exit_guard

    expected = level_price if gap <= level_zone_halfwidth(level_price) else None
    assert from_constructor == expected


def test_boundary_scales_with_price_in_both_implementations():
    """A $1000 level's zone is $10 wide; a $10 level's is $0.10.

    Under the old ATR multiple a single tolerance could be inside one zone
    and outside another on the same day. It cannot now.
    """
    for level_price in (10.0, 100.0, 1000.0):
        just_inside = level_price - level_zone_halfwidth(level_price) * 0.99
        just_outside = level_price - level_zone_halfwidth(level_price) * 1.01
        touches = {level_price: 5}
        entry = level_price * 1.2
        c = _constructor()

        assert c._level_backing_stop(
            _Analysis([level_price], touches), entry, just_inside, False,
        ) == level_price
        assert c._level_backing_stop(
            _Analysis([level_price], touches), entry, just_outside, False,
        ) is None


# ---------------------------------------------------------------------------
# 4. Mechanical guards — the rules in section 1 only hold if nobody quietly
#    reintroduces the deleted setting or a private copy of the constant.
# ---------------------------------------------------------------------------

def test_deleted_setting_is_refused_loudly_not_ignored():
    """`extra="ignore"` would let a stale settings.yaml load in silence."""
    with pytest.raises(ValueError, match="level_match_atr_tolerance"):
        RiskConfig(level_match_atr_tolerance=0.25)


def test_no_atr_multiple_survives_anywhere():
    """No code, config or test may carry the deleted key as anything live.

    Prose mentions are fine and expected (the config tombstone, the incident
    write-up, this file's own docstring) — an assignment or a keyword
    argument is not.
    """
    offenders = []
    roots = [REPO / "src", REPO / "tests", REPO / "scripts", REPO / "config"]
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".yaml", ".yml"} or not path.is_file():
                continue
            for lineno, line in enumerate(
                path.read_text(errors="replace").splitlines(), 1
            ):
                stripped = line.strip()
                if stripped.startswith("#") or "level_match_atr_tolerance" not in line:
                    continue
                if "level_match_atr_tolerance=" in line or (
                    "level_match_atr_tolerance:" in line
                    and not stripped.startswith("#")
                ):
                    # The deletion guard in `RiskConfig` and this test's own
                    # assertion both legitimately name the key.
                    if path.name in {"config.py", "test_level_match_zone.py"}:
                        continue
                    offenders.append(f"{path.relative_to(REPO)}:{lineno}")
    assert not offenders, (
        "docs/WORK.md item 46 deleted `level_match_atr_tolerance`; a live "
        "reference reappeared at: " + ", ".join(offenders)
    )


def test_cluster_constant_has_exactly_one_definition():
    """One source of truth, or the drift this item fixed comes straight back."""
    definitions = []
    for root in (REPO / "src", REPO / "config"):
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".yaml", ".yml"} or not path.is_file():
                continue
            for lineno, line in enumerate(
                path.read_text(errors="replace").splitlines(), 1
            ):
                if line.startswith("CLUSTER_TOLERANCE_PCT"):
                    definitions.append(f"{path.relative_to(REPO)}:{lineno}")
    assert definitions == ["src/data/levels.py:56"], definitions


# ---------------------------------------------------------------------------
# 5. End to end against a real level the scanner actually computed, so the
#    relationship is pinned against `find_structural_levels`' own output and
#    not only against a hand-written price.
# ---------------------------------------------------------------------------

def _bars_with_a_double_bottom() -> list[OHLCV]:
    """Enough clean bars for the scan, with two clear pivot lows near 95."""
    bars: list[OHLCV] = []
    closes = (
        [105, 104, 103, 102, 101, 100, 99, 98, 97, 96]
        + [95.0]                       # pivot low 1
        + [96, 97, 98, 99, 100, 101, 102, 103, 104, 105]
        + [95.4]                       # pivot low 2 — inside 1% of 95.0
        + [96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107]
    )
    start = date(2026, 1, 5)
    for i, close in enumerate(closes):
        bars.append(OHLCV(
            date=start + timedelta(days=i), open=close, high=close + 0.4,
            low=close - 0.4, close=close, volume=1_000_000,
        ))
    return bars


def test_a_stop_inside_a_real_computed_zone_is_recognised():
    supports, _ = find_structural_levels(_bars_with_a_double_bottom())
    assert supports, "fixture must produce a support level"
    level = supports[0]
    assert level.touches >= 2

    tolerance = level_zone_halfwidth(level.price)
    entry = level.price * 1.1
    touches = {level.price: 5}
    c = _constructor()

    inside = level.price - tolerance * 0.9
    outside = level.price - tolerance * 1.1
    assert c._level_backing_stop(
        _Analysis([level.price], touches), entry, inside, False,
    ) == level.price
    assert c._level_backing_stop(
        _Analysis([level.price], touches), entry, outside, False,
    ) is None
