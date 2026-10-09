"""Blocking half of the board-number-collision guard (board item allocation
work, 2026-09-30).

THE PROBLEM THIS CLOSES. `docs/WORK.md` item numbers used to be allocated by
an agent reading the file and taking the next unused integer. With several
agents building in parallel that check cannot win a race — a number is
claimed on a branch long before it reaches this file — and three collisions
happened in about an hour (item 188 twice, then item 189 twice).

These tests are the two halves of the guard that need no network call and
can therefore be REQUIRED (they run inside the `pytest` check branch
protection already gates on): the real board must never contain two items
with the same number, and a live item's number must never also sit on the
retired-numbers line. Both are always true of `docs/WORK.md` at whatever
commit this test runs against — for a `pull_request`-triggered CI run that
commit is GitHub's own merge of the PR branch onto the base branch's tip as
of run time, so a collision that already exists on current `main` is caught
here without needing to diff against any base ref.

The THIRD source of a collision — another currently open pull request that
has not merged yet — needs a live GitHub read and cannot be made blocking
without turning an ordinary network hiccup into a blocked merge queue. See
`scripts/board_numbers.py`'s module docstring and `scripts/
check_board_number_claims.py` (advisory, non-required CI job) for that half.
"""

from __future__ import annotations

from pathlib import Path

from scripts import board_numbers as bn

REPO_ROOT = Path(__file__).resolve().parents[1]
WORK_MD = REPO_ROOT / "docs" / "WORK.md"


# ---------------------------------------------------------------------------
# The real board, right now
# ---------------------------------------------------------------------------


def test_the_real_board_has_no_duplicate_item_numbers():
    text = WORK_MD.read_text()
    dupes = bn.duplicate_live_numbers(text)
    assert not dupes, (
        f"docs/WORK.md currently has more than one item numbered "
        f"{dupes} — two branches claimed the same board item number. "
        f"Renumber one of them (never the other's) and update the "
        f"retired-numbers line if a number was abandoned."
    )


def test_the_real_board_has_no_live_number_that_is_also_retired():
    text = WORK_MD.read_text()
    retired = bn.retired_item_numbers(text)
    assert retired.error is None, f"the retired-numbers line in docs/WORK.md could not be parsed: {retired.error}"
    reused = bn.live_numbers_that_are_retired(text)
    assert not reused, (
        f"item number(s) {reused} are both a live item on the board and "
        f"listed as retired — a retired number was reused. Give the live "
        f"item a fresh number instead."
    )


# ---------------------------------------------------------------------------
# The mechanics, pinned against small fixtures independent of the real file
# ---------------------------------------------------------------------------

_RETIRED_LINE = (
    "**Retired item numbers — never reuse.** APPEND-ONLY: closing an item "
    "adds one new line below.\n"
    "- retired queue: 1, 2, 3\n"
    "- retired gate: 4, 5\n"
)


def test_duplicate_live_numbers_are_found():
    text = f"""\
## THE FUNNEL QUEUE

**10. First item.**

body

**10. Second item, different words, same number.**

body

{_RETIRED_LINE}
"""
    assert bn.duplicate_live_numbers(text) == [10]


def test_no_duplicates_when_every_number_is_distinct():
    text = f"""\
**10. First item.**

**11. Second item.**

{_RETIRED_LINE}
"""
    assert bn.duplicate_live_numbers(text) == []


def test_struck_through_items_still_count_toward_duplicates():
    """A self-declared-finished item still parked on the board (not yet
    struck AND retired) still occupies its number — the board's own
    convention (`_QUEUE_ITEM_RE`) allows an optional `~~` before the number,
    and this must not create a blind spot where a struck heading's number
    looks free."""
    text = f"""\
**~~10. Finished, not yet cleaned up.~~**

**10. A different item reusing the same number.**

{_RETIRED_LINE}
"""
    assert bn.duplicate_live_numbers(text) == [10]


def test_a_live_number_that_is_also_retired_is_found():
    text = f"""\
**3. Reused a retired number.**

{_RETIRED_LINE}
"""
    assert bn.live_numbers_that_are_retired(text) == [3]


def test_a_live_number_from_the_pm_gate_scheme_is_not_flagged():
    """The two numbering schemes are separate (the board's own retired-line
    prose says so explicitly): a live queue item numbered 4 must not be
    flagged just because 4 is retired under the PM TEST GATE scheme."""
    text = f"""\
**4. A live queue item, unrelated to gate item 4.**

{_RETIRED_LINE}
"""
    assert bn.live_numbers_that_are_retired(text) == []


def test_unparseable_retired_line_is_reported_as_an_error_not_a_false_clean_bill():
    text = "**10. An item.**\n\n**Retired item numbers — never reuse.** garbled\n"
    retired = bn.retired_item_numbers(text)
    assert retired.error is not None
    # And the duplicate/retired-reuse check must not silently report "clean"
    # off an unparseable line — it reports no finding, which the caller (the
    # test above) treats as a hard failure via `retired.error`, never as
    # proof nothing is retired.
    assert bn.live_numbers_that_are_retired(text) == []


# ---------------------------------------------------------------------------
# Open-PR claims (advisory data source) and the positive script's math
# ---------------------------------------------------------------------------


def test_claims_from_patch_reads_only_added_heading_lines():
    patch = (
        "@@ -10,3 +10,7 @@\n"
        " unrelated context line\n"
        "-**190. An old title being edited.**\n"
        "+**190. A retitled item, same number, not a new claim source line.**\n"
        "+\n"
        "+**191. A brand new item.**\n"
    )
    # 190 appears on a '+' line too (the retitle), so it is legitimately
    # counted as "added" here; this only proves REMOVED ('-') lines are
    # never counted and that a genuinely new number is picked up.
    assert bn.claims_from_patch(patch) == {190, 191}


def test_next_free_number_takes_the_highest_of_all_three_sources():
    text = f"""\
**50. Live item.**

{_RETIRED_LINE}
"""
    # retired queue goes up to 3; live goes up to 50; an open PR claims 60.
    claims = bn.OpenPrClaims(by_pr={7: {60}})
    result = bn.next_free_number(text, pr_claims=claims)
    assert result.next_number == 61
    assert result.highest_known == 60
    assert result.checked_open_prs is True


def test_next_free_number_degrades_honestly_when_github_could_not_be_read():
    text = f"""\
**50. Live item.**

{_RETIRED_LINE}
"""
    claims = bn.OpenPrClaims(problem="GitHub could not be reached")
    result = bn.next_free_number(text, pr_claims=claims)
    # Falls back to the local sources only; never crashes, never pretends
    # the open-PR read succeeded.
    assert result.next_number == 51
    assert result.checked_open_prs is False
    assert result.open_pr_problem == "GitHub could not be reached"


def test_next_free_number_works_with_no_pr_claims_argument_at_all():
    text = f"""\
**50. Live item.**

{_RETIRED_LINE}
"""
    result = bn.next_free_number(text)
    assert result.next_number == 51
    assert result.checked_open_prs is False
