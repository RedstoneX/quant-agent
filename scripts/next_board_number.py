#!/usr/bin/env python3
"""Print the next genuinely free board item number for `docs/WORK.md`.

THE PROBLEM THIS CLOSES. An agent used to get this by reading `docs/WORK.md`
by eye — the live items, the retired-numbers line, and (if it remembered)
open branches — and taking the next unused integer. With several agents
building in parallel that check cannot win a race: a number is claimed on a
branch long before it reaches the file. Three collisions happened in about
an hour on 2026-09-29/30 (items 188 and 189, twice each) even though every
agent involved had checked correctly. This script is the one place that
check is now made, reusing `scripts/board_numbers.py` — the same module a
CI test and an advisory GitHub check also read, so this script's answer can
never quietly disagree with what CI will accept.

It is still not a GUARANTEE — see `scripts/board_numbers.py`'s module
docstring for why the open-pull-request half can only ever be advisory. Two
agents can still run this within the same few seconds and get the same
answer. What it closes is the ORDINARY case: an agent that runs this
immediately before writing a new item will see every number any other
agent has already pushed to a branch, not just what has reached `main`. The
mechanical backstop for the race that survives that is the blocking check
in `tests/test_board_item_numbers.py`, which fails the merge outright if two
items still end up sharing a number.

IT FAILS CLOSED, AND IT USED NOT TO. Until 2026-09-30 a failed open-pull-
request read printed a WARNING and returned a number anyway with exit 0.
That happened for real: the read hit a GitHub request limit, the warning
scrolled past, two pull requests both claimed item 192, and a human caught
it by hand. A tool whose entire job is to prevent a collision must not hand
back an answer it cannot stand behind — the desk's rule is that everything
mechanically enforced holds and everything relying on somebody noticing a
warning slips, and a warning is precisely what this printed. So an
unreadable open-PR list is now an ERROR: nothing usable is printed, and the
exit code is non-zero.

The offline case is served by stating out loud that you accept the risk.
`--accept-unchecked-number` is deliberately not called `--no-github`: the
flag names the consequence rather than the mechanism, so a caller reaching
for it has to acknowledge what they are getting, and the number it prints
is labelled UNCHECKED in the output itself so the label travels with the
number when it is pasted somewhere else.

THE BOARD IS READ FROM `origin/main`, NOT FROM THE WORKING TREE. The board
is shared state and the working tree is not: the main development checkout
on this box is shared between sessions and nobody pulls it. On 2026-10-01
it was 120 commits behind and two agents in parallel were both handed 222,
for unrelated defects, because that number had already landed on the board
before either of them asked. The open-pull-request half of this check
could not catch it — that half looks FORWARD at numbers claimed on
branches. So the shared ref is read too, with `git show` and never a
`git fetch`, and the local copy still counts so an item written here but
not yet pushed keeps its number. An unreadable ref falls back to the
working tree alone and SAYS SO, because an agent blocked here cannot file
its item at all.

Usage:
    scripts/next_board_number.py
    scripts/next_board_number.py --work-md docs/WORK.md
    scripts/next_board_number.py --accept-unchecked-number  # offline only

Exit codes:
    0  a number was produced AND every source behind it was read
    3  `docs/WORK.md` or its retired-numbers line could not be read at all
    4  the open pull requests could not be read, so no safe number exists;
       re-run when GitHub is reachable, or pass
       `--accept-unchecked-number` to take one anyway
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.board_locator import working_board
from scripts.board_numbers import (  # noqa: E402
    OpenPrClaims,
    read_open_pr_claims,
    read_ref_work_md,
    retired_item_numbers,
    next_free_number,
)
from scripts.guard_reference import ReferenceUnavailable

DEFAULT_BOARD_REF = "origin/main"


def _get_default_work_md() -> str:
    """Get the board path using board_locator, or return a default."""
    try:
        work_md_path, _ = working_board()
        return work_md_path
    except ReferenceUnavailable:
        # Fallback to docs/WORK.md if board cannot be located
        return "docs/WORK.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-md", default=_get_default_work_md())
    parser.add_argument(
        "--board-ref", default=DEFAULT_BOARD_REF,
        help="The shared ref holding the authoritative board (default: "
             "origin/main). Read with `git show` only - never fetched.",
    )
    parser.add_argument(
        "--accept-unchecked-number", action="store_true",
        help="Skip the open-pull-request read and accept a number that "
             "may already be claimed on somebody else's branch. The "
             "printed number is labelled UNCHECKED.",
    )
    args = parser.parse_args(argv)

    work_md = Path(args.work_md)
    if not work_md.is_file():
        print(f"next_board_number: {work_md} not found", file=sys.stderr)
        return 3
    text = work_md.read_text()

    retired = retired_item_numbers(text)
    if retired.error:
        print(f"next_board_number: could not read the retired-numbers line: "
              f"{retired.error}", file=sys.stderr)
        return 3

    # THE SHARED COPY OF A SHARED FILE. See `read_ref_work_md` for the
    # collision this closes: the working tree alone cannot see a number
    # that landed on the board before this agent asked.
    ref_board = read_ref_work_md(work_md, ref=args.board_ref)
    extra_texts: list[str] = []
    if ref_board.text is not None:
        ref_retired = retired_item_numbers(ref_board.text)
        if ref_retired.error:
            ref_board.text = None
            ref_board.problem = ("its retired-numbers line could not be "
                                 f"read: {ref_retired.error}")
        else:
            extra_texts.append(ref_board.text)

    unchecked = args.accept_unchecked_number
    claims: OpenPrClaims | None = None
    if not unchecked:
        claims = read_open_pr_claims()

    result = next_free_number(text, pr_claims=claims,
                              extra_texts=extra_texts)

    # FAIL CLOSED. The caller did not opt out, and the half of the check
    # that catches the parallel-agent race could not run. Printing the
    # number with a warning beside it is what let items 192 collide; the
    # only safe output here is no number at all.
    if not unchecked and not result.checked_open_prs:
        print(
            "next_board_number: the open pull requests could not be read "
            f"({result.open_pr_problem}), so this check CANNOT give you a "
            "safe number. Most board numbers are claimed on a branch long "
            "before they reach docs/WORK.md, and that is exactly the half "
            "that just failed to read.\n"
            "  Re-run when GitHub is reachable. If you must proceed "
            "offline, pass --accept-unchecked-number, which says you "
            "accept a number that may already be taken and prints it "
            "labelled as such.",
            file=sys.stderr,
        )
        return 4

    label = " (UNCHECKED)" if unchecked else ""
    print(f"Next free board item number{label}: {result.next_number}")
    print(f"  (highest number known to this check: {result.highest_known})")
    if extra_texts:
        print(f"  Board read from {ref_board.ref} (the shared, authoritative "
              "copy) AND from the working tree.")
    else:
        print(f"  Board read from the WORKING TREE ONLY - {ref_board.ref} "
              f"could not be read ({ref_board.problem}). A shared working "
              "tree can sit many commits behind, so a number that already "
              "landed on the board may not be visible here.")
    if unchecked:
        print("  UNCHECKED: open pull requests were NOT read "
              "(--accept-unchecked-number), so another agent may already "
              "have claimed this number on a branch. Do not write it down "
              "without checking open branches yourself.")
    else:
        n = len(claims.by_pr) if claims else 0
        print(f"  Checked {n} open pull request(s) with a docs/WORK.md diff.")
    print()
    print("This is still not a guarantee against another agent claiming the "
          "same number at the same moment — write the item and open the "
          "pull request promptly. If two items still collide, the "
          "`pytest` check on the pull request will fail the merge; whoever "
          "merges second renumbers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
