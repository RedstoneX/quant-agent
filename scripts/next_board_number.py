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

Usage:
    scripts/next_board_number.py
    scripts/next_board_number.py --work-md docs/WORK.md
    scripts/next_board_number.py --no-github   # skip the open-PR read

Exit codes:
    0  a number was produced (the open-PR read may still have failed; see
       the printed warning)
    3  `docs/WORK.md` or its retired-numbers line could not be read at all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.board_numbers import (  # noqa: E402
    OpenPrClaims,
    read_open_pr_claims,
    retired_item_numbers,
    next_free_number,
)

DEFAULT_WORK_MD = "docs/WORK.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-md", default=DEFAULT_WORK_MD)
    parser.add_argument("--no-github", action="store_true",
                         help="Skip the open-pull-request read (offline/fast).")
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

    claims: OpenPrClaims | None = None
    if not args.no_github:
        claims = read_open_pr_claims()

    result = next_free_number(text, pr_claims=claims)

    print(f"Next free board item number: {result.next_number}")
    print(f"  (highest number known to this check: {result.highest_known})")
    if args.no_github:
        print("  Open pull requests were NOT checked (--no-github). Re-run "
              "without it, or check open branches yourself, before writing "
              "the number down.")
    elif not result.checked_open_prs:
        print(f"  WARNING: open pull requests could not be checked "
              f"({result.open_pr_problem}). This number only accounts for "
              f"the live board and the retired-numbers line — check open "
              f"branches yourself before writing it down.")
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
