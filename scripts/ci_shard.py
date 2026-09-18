#!/usr/bin/env python3
"""Split the test suite into balanced shards, one shard per CI job.

Why this exists
---------------
CI already runs the suite across the runner's cores (``-n auto`` from
pytest-xdist, see ``[tool.pytest.ini_options]`` in pyproject.toml). A free
GitHub runner only has 4 cores, so that is the end of the within-job win. The
remaining lever is running several runners at once, each taking a slice of the
suite. This script decides the slices.

How it splits
-------------
By test *file*, never by individual test. A whole module always lands in one
shard, so module-scoped fixtures, class-scoped state and within-file ordering
behave exactly as they do in a single run. Splitting mid-module is what makes
sharding flaky; we do not do it.

Balance comes from a greedy longest-processing-time assignment over a cheap
weight: the number of ``def test_`` lines in the file, counted by regex without
importing anything. Parametrisation means that weight is an approximation of
the real test count, and it makes no claim to be the file's runtime. It only
has to be good enough to stop one shard getting all the big modules --
correctness does not depend on it.

Determinism
-----------
The file list is sorted and the weights come from the file contents, so every
shard job in a run computes the same split from the same commit without any
shared state, cache or durations file. ``--check`` asserts the split is a true
partition: every file in exactly one shard, nothing invented, nothing dropped.

Usage
-----
    python scripts/ci_shard.py --shards 4 --index 0    # files for shard 0
    python scripts/ci_shard.py --shards 4 --check      # verify the partition
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# A test function at any indentation, optionally async. Deliberately a regex
# and not collection: this must stay fast enough to run in every shard job.
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test_", re.MULTILINE)

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"


def test_files(tests_dir: Path = TESTS_DIR) -> list[Path]:
    """Every test module, sorted, relative to the repo root."""
    found = [
        p.relative_to(REPO_ROOT)
        for p in tests_dir.rglob("test_*.py")
        if "__pycache__" not in p.parts
    ]
    return sorted(found)


def weight(path: Path) -> int:
    """Approximate test count for one file. At least 1 so no file weighs zero."""
    try:
        text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 1
    return max(1, len(TEST_DEF.findall(text)))


def split(shards: int, tests_dir: Path = TESTS_DIR) -> list[list[Path]]:
    """Partition the test files into ``shards`` buckets of similar weight.

    Greedy longest-processing-time: heaviest file first, into whichever bucket
    is currently lightest. Ties break on bucket index so the result is stable.
    """
    if shards < 1:
        raise ValueError("shards must be >= 1")

    files = test_files(tests_dir)
    weighted = sorted(
        ((weight(f), f) for f in files),
        key=lambda pair: (-pair[0], str(pair[1])),
    )

    buckets: list[list[Path]] = [[] for _ in range(shards)]
    totals = [0] * shards
    for w, f in weighted:
        target = min(range(shards), key=lambda i: (totals[i], i))
        buckets[target].append(f)
        totals[target] += w

    return [sorted(b) for b in buckets]


def check(shards: int, tests_dir: Path = TESTS_DIR) -> int:
    """Assert the split is a real partition of the test files. 0 if it is."""
    files = test_files(tests_dir)
    buckets = split(shards, tests_dir)

    assigned = [f for b in buckets for f in b]
    problems = []

    if len(assigned) != len(set(assigned)):
        seen: set[Path] = set()
        dupes = sorted({f for f in assigned if f in seen or seen.add(f)})  # type: ignore[func-returns-value]
        problems.append(f"file(s) in more than one shard: {dupes}")
    missing = sorted(set(files) - set(assigned))
    if missing:
        problems.append(f"file(s) in no shard: {missing}")
    extra = sorted(set(assigned) - set(files))
    if extra:
        problems.append(f"file(s) not in the suite: {extra}")
    empty = [i for i, b in enumerate(buckets) if not b]
    if empty:
        problems.append(f"empty shard(s): {empty}")

    for line in problems:
        print(f"ci_shard: {line}", file=sys.stderr)
    if problems:
        return 1

    totals = [sum(weight(f) for f in b) for b in buckets]
    print(
        f"ci_shard: {len(files)} files, {sum(totals)} tests, {shards} shards; "
        f"per-shard test counts {totals}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--index", type=int, help="0-based shard to print")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the split partitions the suite, then exit",
    )
    args = parser.parse_args(argv)

    if args.check:
        return check(args.shards)

    if args.index is None:
        parser.error("--index is required unless --check is given")
    if not 0 <= args.index < args.shards:
        parser.error(f"--index must be in [0, {args.shards})")

    for path in split(args.shards)[args.index]:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
