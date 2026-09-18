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

Balance comes from a greedy longest-processing-time assignment over an
*estimated runtime* in seconds, not over a test count. Test count is a bad
proxy here, because this suite's cost is concentrated rather than spread. Full
serial run, 6,618 tests in 507.7s, measured 2026-09-18 at commit 3bec45b6:

    160.0s  test_rehearsal_reproduces_cost_ceiling.py  (ONE test)
     60.0s  test_tech_analyst.py                       (ONE test)
     16.9s  test_ops_scripts_importable.py             (ONE test)
    ~ 11s   each, four separate one-definition guard tests
    < 7s    everything else, and the tail is flat

Two tests are 43% of the suite. Both are wall-clock assertions -- they exist to
prove a deadline or a retry budget holds -- so their cost is waiting, not
computing: of the 507.7s total only 219.5s was CPU. That is why ``SLOW_FILES``
below carries measured seconds for the handful of files that matter, and why
every other file is estimated as its test count times ``SECONDS_PER_TEST``,
the measured mean over the flat tail.

Getting this weighting right is not about tidiness. If the 160s file and the
60s file land in the same shard, that shard alone takes 220s and sets the
whole run's wall-clock. Weighting by count put them in different shards by
luck; weighting by seconds does it on purpose.

Keeping ``SLOW_FILES`` current is optional maintenance, not a correctness
requirement. A stale entry costs some balance and nothing else: a new file
simply gets the tail estimate, and ``--check`` still proves the split is a
complete partition of the suite. Each shard job prints ``--durations``, so the
numbers to refresh it with are in the job log.

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

# Measured serial runtime, in seconds, for the files whose cost is not
# proportional to how many tests they hold. From `pytest --durations` on a full
# serial run, 2026-09-18, commit 3bec45b6. Refresh from any shard job's log.
# Files absent from here are estimated from their test count; see the module
# docstring for why a stale entry is a balance problem and never a correctness
# one.
SLOW_FILES: dict[str, float] = {
    "test_rehearsal_reproduces_cost_ceiling.py": 160.0,
    "test_tech_analyst.py": 61.0,
    "test_ops_scripts_importable.py": 17.0,
    "test_one_definition_per_quantity.py": 35.0,
    "test_one_definition_guard.py": 24.0,
    "test_event_risk_calendar.py": 12.0,
    "test_news.py": 14.0,
    "test_market_data.py": 8.0,
}

# Mean measured seconds per test over the flat tail: (507.7s total minus the
# 331s accounted for above) divided by the tests outside those files.
SECONDS_PER_TEST = 0.0275


def test_files(tests_dir: Path = TESTS_DIR) -> list[Path]:
    """Every test module, sorted, relative to the repo root."""
    found = [
        p.relative_to(REPO_ROOT)
        for p in tests_dir.rglob("test_*.py")
        if "__pycache__" not in p.parts
    ]
    return sorted(found)


def test_count(path: Path) -> int:
    """Approximate test count for one file. At least 1 so no file counts zero."""
    try:
        text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 1
    return max(1, len(TEST_DEF.findall(text)))


def weight(path: Path) -> float:
    """Estimated serial runtime in seconds for one file."""
    measured = SLOW_FILES.get(path.name)
    if measured is not None:
        return measured
    return test_count(path) * SECONDS_PER_TEST


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
    totals = [0.0] * shards
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

    counts = [sum(test_count(f) for f in b) for b in buckets]
    seconds = [sum(weight(f) for f in b) for b in buckets]
    print(
        f"ci_shard: {len(files)} files, {sum(counts)} tests, {shards} shards"
    )
    print(f"ci_shard: tests per shard      {counts}")
    print(
        "ci_shard: estimated seconds   "
        f"{[round(s) for s in seconds]} (longest shard sets the wall-clock)"
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
