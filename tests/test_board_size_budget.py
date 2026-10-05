"""Tests for `scripts/board_size_budget.py` -- the board's size budget.

Lifted verbatim from `tests/test_status_board.py`; `sb` is now the budget
module itself. The byte ceiling is still owned by
`tests/test_status_board.py::test_work_md_stays_under_a_hundred_thousand_bytes`
(which `scripts/check_board_hygiene.py:read_cap_bytes` reads), so that test
and its git-reading neighbours stay where they are.
"""

from __future__ import annotations

from pathlib import Path

from scripts import board_size_budget as sb


def test_work_md_growth_budget_shrinks_as_the_file_fills():
    """Pins `sb.work_md_growth_budget`'s shape directly, independent of git
    plumbing: half of whatever headroom remains below the cap, so the
    allowance shrinks as the file fills rather than staying flat. See the
    function's own docstring in scripts/status_board.py for why 0.5 is
    provisional and what it replaced."""
    cap = sb.WORK_MD_GROWTH_CAP_BYTES
    assert sb.work_md_growth_budget(30_000, cap) == 35_000   # ~30% full
    assert sb.work_md_growth_budget(85_000, cap) == 7_500    # ~85% full
    assert sb.work_md_growth_budget(95_000, cap) == 2_500    # ~95% full
    assert sb.work_md_growth_budget(cap, cap) == 0           # at the cap
    assert sb.work_md_growth_budget(cap + 10_000, cap) == 0  # past it: never negative


def test_work_md_growth_share_is_pinned():
    """`WORK_MD_GROWTH_SHARE` is a provisional judgement call (see its
    comment in scripts/status_board.py) — pinned here so a future edit
    cannot quietly loosen or tighten it without a visible, reviewed test
    change."""
    assert sb.WORK_MD_GROWTH_SHARE == 0.5


def test_work_md_growth_cap_matches_the_byte_ceiling():
    """`WORK_MD_GROWTH_CAP_BYTES` must equal the cap
    `test_work_md_stays_under_a_hundred_thousand_bytes` enforces below —
    that test owns the number, this only guards against the two silently
    drifting apart if the ceiling is ever changed there and not here.
    Reads the ceiling test's own source, the same way
    `scripts/check_board_hygiene.py:read_cap_bytes` already does, rather
    than re-typing the number a third time."""
    from scripts.check_board_hygiene import read_cap_bytes

    repo = Path(__file__).resolve().parents[1]
    cap, error = read_cap_bytes(repo)
    assert error is None, error
    assert sb.WORK_MD_GROWTH_CAP_BYTES == cap


def test_the_cap_cannot_deadlock_the_board():
    """Board item 200. The hard cap was a bare check on the file as it
    stands, so the moment the file went over it, EVERY change failed —
    including the retirement that would bring it back under. The only
    exits were force-merging or raising the cap, and raising the cap is
    the one thing the cap exists to prevent.

    `sb.work_md_cap_blocker` is the rule now: over the cap is refused,
    except for a change that strictly shrinks the file, which always
    lands. Simulated here against over-cap sizes rather than trusting the
    real file, which is currently comfortably under.
    """
    cap = sb.WORK_MD_GROWTH_CAP_BYTES

    # Under the cap: nothing to say, growing or shrinking.
    assert sb.work_md_cap_blocker(90_000, 95_000, cap) is None
    assert sb.work_md_cap_blocker(95_000, 90_000, cap) is None
    assert sb.work_md_cap_blocker(99_999, cap, cap) is None  # exactly at it

    # The change that pushes it over is refused at that change.
    blocked = sb.work_md_cap_blocker(99_000, 101_000, cap)
    assert blocked is not None and "over the" in blocked

    # Already over: a prune lands however far over it still is.
    assert sb.work_md_cap_blocker(120_000, 119_999, cap) is None
    assert sb.work_md_cap_blocker(120_000, 101_000, cap) is None
    # ...and it lands all the way back under, obviously.
    assert sb.work_md_cap_blocker(120_000, 80_000, cap) is None

    # Already over and NOT shrinking: still refused, including a change
    # that leaves the size exactly as it found it.
    assert sb.work_md_cap_blocker(120_000, 120_000, cap) is not None
    assert sb.work_md_cap_blocker(120_000, 130_000, cap) is not None


def test_the_cap_warns_before_it_binds():
    """Board item 200's other half: the first warning must not be a
    blocked merge. `sb.work_md_cap_warning` fires at
    `sb.WORK_MD_WARN_SHARE` of the cap, while `work_md_growth_budget`
    still allows a normal change, and is emitted from the growth-budget
    test so the notice and the eventual failure speak in one place."""
    cap = sb.WORK_MD_GROWTH_CAP_BYTES
    assert sb.WORK_MD_WARN_SHARE == 0.8
    assert sb.work_md_cap_warning(70_000, cap) is None
    assert sb.work_md_cap_warning(79_999, cap) is None
    warned = sb.work_md_cap_warning(80_000, cap)
    assert warned is not None and "80%" in warned
    assert sb.work_md_cap_warning(95_000, cap) is not None
    # Fires early enough that pruning is still a choice, not a precondition.
    assert sb.work_md_growth_budget(int(sb.WORK_MD_WARN_SHARE * cap), cap) == 10_000


