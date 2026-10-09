"""The board's size budget: how much `docs/WORK.md` may grow in one change.

Lifted verbatim out of `scripts/status_board.py` (board item 200 / 201
work): the share-of-headroom growth allowance, the byte cap it is measured
against, the early warning, and the merge blocker. Pure arithmetic over two
sizes; imports nothing from the board generator, so it can be read, tested
and reasoned about on its own. `scripts/status_board.py` re-exports every
name so existing callers and tests are unchanged.
"""

from __future__ import annotations


#: PROVISIONAL — no owner ruling states this fraction; it is a judgement
#: call, made here rather than left as an unstated assumption in a test.
#:
#: `work_md_growth_budget` spends this share of whatever headroom remains
#: below the 100,000-byte cap (`test_work_md_stays_under_a_hundred_thousand_
#: bytes` owns that number; this module never re-types it, see
#: `WORK_MD_GROWTH_CAP_BYTES` below) on ONE change. That makes the allowance
#: shrink automatically as the file fills — half the remaining room at 30%
#: full is enormous (unrestricted in practice), half the remaining room at
#: 95% full is a couple thousand bytes (enough for a short item, not enough
#: to dump an afternoon's findings without pruning first) — without pinning
#: separate numbers at arbitrary bands (60%, 85%, 95%, ...) that would each
#: need their own justification. 0.5 was picked only because it is the
#: simplest value that produces that shape; it is not measured from
#: anything. Owner ruling 2026-09-17: the old rule (a change may never leave
#: docs/WORK.md larger than it found it) was replacing a housekeeping
#: problem with a recording-defects problem, and had to go — see
#: `test_work_md_growth_is_bounded_and_shrinks_as_the_cap_fills`.
WORK_MD_GROWTH_SHARE = 0.5

#: Kept EQUAL to the cap `test_work_md_stays_under_a_hundred_thousand_bytes`
#: enforces — that test owns the number, this is a second, independent
#: place it is used, and `test_work_md_growth_cap_matches_the_byte_ceiling`
#: reads the cap test's own source (the same way
#: `scripts/check_board_hygiene.py:read_cap_bytes` already does) and fails
#: if the two ever disagree, so this cannot drift silently if the ceiling
#: test is ever edited.
WORK_MD_GROWTH_CAP_BYTES = 100_000


def work_md_growth_budget(
    before_size: int, cap: int = WORK_MD_GROWTH_CAP_BYTES, share: float = WORK_MD_GROWTH_SHARE
) -> int:
    """How many bytes `docs/WORK.md` may grow in a single change, given its
    size before that change.

    Replaces the 2026-09-14 rule that a change could never leave the file
    larger than it found it. That rule was written for a real problem —
    finished work piling up unpruned — but had no escape hatch, so it also
    blocked recording a brand-new, genuine defect on a night when far more
    defects were found than were closed, while the file sat at ~30,000 of
    its 100,000-byte cap: comfortably under it, with nothing to prune.
    Owner's ruling (2026-09-17, in substance): the rule was badly written;
    he wants housekeeping enforced, not recording blocked. This function is
    the mechanical replacement.

    Returns a budget that SHRINKS as the file fills, rather than a flat
    allowance: `share` of whatever headroom remains below `cap`. Near-empty,
    the budget is effectively unrestricted for a normal edit; near the cap,
    it is small enough that anything but a short item forces pruning first.
    The 100,000-byte hard cap itself is untouched and still the final
    backstop (`test_work_md_stays_under_a_hundred_thousand_bytes`) — this
    only shapes how a single change may approach it. Never negative: a
    `before_size` at or past `cap` returns 0.

    This does not, by itself, make housekeeping happen — it only bounds how
    much can be added without it. The mechanical push to actually retire
    finished work is `find_finished_items_still_on_board` /
    `find_closed_items_not_marked_done`, run unconditionally against the
    real board on every change (`test_the_real_backlog_has_no_finished_
    item_still_on_the_board`, `test_the_real_backlog_has_no_item_
    contradicting_its_own_title`) — not gated on growth, so a change that
    adds nothing still fails if it leaves a self-declared-finished item
    sitting on the board.
    """
    headroom = max(cap - before_size, 0)
    return int(headroom * share)


#: The share of the cap at which CI starts SAYING SO out loud, instead of
#: the first warning being a merge that will not go through. PROVISIONAL —
#: no owner ruling states a share; 0.8 is picked so the nudge lands while
#: `work_md_growth_budget` still allows 10,000 bytes of growth (half the
#: remaining fifth), i.e. while an ordinary change still fits and pruning
#: is a choice rather than a precondition. Deliberately EARLIER than
#: `scripts/check_board_hygiene.py:NEAR_CAP_SHARE` (0.90), which is the
#: owner-facing ops report: the people who can prune see it first.
WORK_MD_WARN_SHARE = 0.8


def work_md_cap_warning(
    size: int, cap: int = WORK_MD_GROWTH_CAP_BYTES, share: float = WORK_MD_WARN_SHARE
) -> str | None:
    """A loud, early notice that `docs/WORK.md` is filling up, or None.

    Board item 200: the first signal that the cap was binding used to be a
    failed merge. This fires while there is still room to act, and is
    emitted from the same test that enforces the growth budget so it shows
    up in the same place a budget failure already speaks.
    """
    if size < share * cap:
        return None
    return (
        f"docs/WORK.md is at {size:,} of its {cap:,}-byte cap "
        f"({size / cap:.0%} full, {max(cap - size, 0):,} bytes left). A "
        f"single change may still grow it by "
        f"{work_md_growth_budget(size, cap):,} bytes, and that allowance "
        "keeps shrinking. Retire finished items into "
        "docs/INCIDENT_HISTORY.md, or move argument and history out of open "
        "items into docs/board_notes/, before the cap starts refusing "
        "work."
    )


def work_md_cap_blocker(before_size: int, after_size: int, cap: int = WORK_MD_GROWTH_CAP_BYTES) -> str | None:
    """Whether the hard cap should REFUSE this change, as a message, or None.

    Board item 200: the cap used to be a bare `size <= cap` on the file as
    it stands, which deadlocks. Once the file is over the cap — by one
    change that squeaked past a stale base measurement, or by two changes
    racing — that assertion fails for EVERY subsequent change, including
    the retirement that would bring it back under. Nothing can merge, and
    the only exits are force-merging or raising the cap, which is exactly
    what the cap exists to prevent.

    So: over the cap is a failure, EXCEPT for a change that strictly
    shrinks the file. A shrinking change is always allowed to land, however
    far over the cap the file still is — it is the only kind of change that
    can end the state. Growth is refused as hard as before, and a change
    that first pushes the file over the cap is refused at the change that
    does it, not at the next one.
    """
    if after_size <= cap:
        return None
    if after_size < before_size:
        return None  # a prune: always lands, this is the way out
    return (
        f"docs/WORK.md is {after_size:,} bytes, over the {cap:,}-byte cap "
        f"(it was {before_size:,} before this change) — finished or decided "
        "content has likely crept back in; MOVE it to "
        "docs/INCIDENT_HISTORY.md, or move argument and history into "
        "docs/board_notes/, rather than deleting it, and never raise this "
        "number to make room. A change that SHRINKS the file is exempt from "
        "this cap even while it is still over, so the prune that fixes this "
        "can always merge."
    )
