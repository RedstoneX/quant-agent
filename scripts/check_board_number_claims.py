#!/usr/bin/env python3
"""Advisory cross-PR check: does this pull request's new board item number
collide with a number another currently-open pull request also just added?

NOT a required check, and never wired into branch protection — see
`scripts/board_numbers.py`'s module docstring for why. Answering this
question needs a live read of every other open pull request's own diff,
which is a network call: it can rate-limit, time out, or simply go stale
the instant after it returns, because another PR can merge in between. A
REQUIRED check that depends on that read would turn an ordinary GitHub
hiccup into a blocked merge queue — its own outage. So this runs as a
separate, non-required CI job (`.github/workflows/test.yml`, job
`board-number-advisory`) purely for visibility in the run log: a real
collision prints loudly and the job goes red, but a failed GitHub read
prints a plain "could not check" and the job stays GREEN, because "GitHub
could not be reached" is not evidence that a collision exists.

The two locally-verifiable halves — no duplicate live item number, no live
number that is also retired — are BLOCKING, enforced by
`tests/test_board_item_numbers.py` as part of the required `pytest` check.
This script only ever covers the third, advisory half.

Usage:
    scripts/check_board_number_claims.py --pr-number 742
    scripts/check_board_number_claims.py --pr-number 742 --work-md docs/WORK.md

`--pr-number` identifies which open PR is "this one" so it is excluded from
its own collision check (comparing a PR's numbers against itself is not a
collision) and so its own newly-added numbers can be read straight from the
GitHub list rather than a local diff, working whether the script runs in
the PR's own checkout or anywhere else.

Exit codes:
    0  no collision found, or the open-PR read failed (advisory only —
       never turns a network hiccup into red)
    1  a real cross-PR collision was found
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.board_numbers import read_open_pr_claims  # noqa: E402


def find_collisions(
    pr_number: int,
    claims_by_pr: dict[int, set[int]],
) -> list[str]:
    """One sentence per OTHER open PR that added a number this PR also
    added. `pr_number` itself is never compared against itself."""
    mine = claims_by_pr.get(pr_number, set())
    if not mine:
        return []
    out: list[str] = []
    for other_pr, other_nums in sorted(claims_by_pr.items()):
        if other_pr == pr_number:
            continue
        overlap = sorted(mine & other_nums)
        if overlap:
            plural = "s" if len(overlap) > 1 else ""
            out.append(
                f"PR {pr_number} and PR {other_pr} both add board item "
                f"number{plural} {', '.join(str(n) for n in overlap)} — "
                f"one of them must renumber before either merges."
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pr-number", type=int, required=True)
    args = parser.parse_args(argv)

    claims = read_open_pr_claims()
    if claims.problem:
        print(
            f"check_board_number_claims: could not read open pull "
            f"requests ({claims.problem}); advisory check skipped, not "
            f"failed",
            file=sys.stderr,
        )
        return 0

    if args.pr_number not in claims.by_pr:
        print(
            f"check_board_number_claims: PR {args.pr_number} adds no new "
            f"board item number (or could not be read); nothing to check "
            f"against other open PRs"
        )
        return 0

    collisions = find_collisions(args.pr_number, claims.by_pr)
    if not collisions:
        print(f"check_board_number_claims: no other open pull request claims {sorted(claims.by_pr[args.pr_number])}")
        return 0

    for line in collisions:
        print(f"::warning::{line}")
        print(line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
