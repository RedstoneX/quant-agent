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
proxy here, because this suite's cost is concentrated rather than spread. The
first attempt split by count, gave four shards 1,409 tests each, and they took
22s, 24s, 92s and 160s.

Measured on a real runner (``--durations=0`` across every shard, 2026-09-18,
run 35301604006) the whole suite was 758.7s of test time, and where it went
was extremely lopsided:

    230.9s  test_one_definition_per_quantity.py   30% of the suite, 5 tests
     85.0s  test_ops_scripts_importable.py        one test is 70.9s of it
     60.2s  test_tech_analyst.py                  one test is 60.0s of it
     32.0s  test_pipeline.py
     30.2s  test_cost_circuit.py
    151.3s  the other 195 files, 4,454 tests, essentially flat

Three of those five costs were not inherent and have since been fixed at the
source rather than papered over with more shards:

- ``test_one_definition_per_quantity.py`` held three whole-repo AST scans
  that each re-read and re-parsed every file under ``src/`` independently
  (~62s apiece). They now share one cached parse per test session
  (``_binops_cached`` in that file), so the walk happens once and the other
  two tests reuse it -- three tests, one parse.
- ``test_ops_scripts_importable.py::test_no_python_312_only_fstrings`` (the
  71s test) parses ``ops/``, ``scripts/``, ``src/`` and ``tests/``. It now
  goes through ``tests/_shared_ast_cache.py``, a path-keyed cache shared with
  the guard above -- any file already parsed by one whole-repo scan is not
  re-read by the other.
- ``test_tech_analyst.py``'s 60s test was not inherent to what it proves at
  all: `src.agents.base._TOKEN_GOVERNORS` is a process-wide singleton token-
  rate limiter, and an earlier test in the same file (chunked cost-merging)
  charged it ~184k tokens against a 150k/min ceiling with real usage numbers.
  The next test to call the (real, unmocked) governor then genuinely called
  `time.sleep` for up to the full 60s window to drain. `tests/conftest.py`
  now clears every governor's window before each test, so a test's rate
  budget can no longer leak into whichever test happens to run after it.

With those fixed, the remaining floor is whatever a single whole-repo scan
costs standing alone plus the flat ~151s tail, comfortably re-shardable into
far fewer jobs than the five the un-fixed costs used to require. See the
current shard count and its own measured comment in
``.github/workflows/test.yml``.

That shape is why ``SLOW_FILES`` below carries a measured second count for
the files that still matter and everything else is estimated as its test
count times ``SECONDS_PER_TEST``, the measured mean over the flat tail. A
stale entry only costs balance, never correctness -- ``--check`` still proves
the split is a complete partition of the suite regardless.

Measure on the runner, not on a developer box. A local serial run disagreed
badly with the original numbers above -- it made one file look like 160s that
is under 5s in CI, and understated test_ops_scripts_importable fourfold --
because host load and Python version move these particular tests a lot.

Keeping ``SLOW_FILES`` current is optional maintenance, not a correctness
requirement. A stale entry costs some balance and nothing else: a new file
simply gets the tail estimate, and ``--check`` still proves the split is a
complete partition of the suite. Each shard job prints ``--durations``, so the
numbers to refresh it with are already in the job log.

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

# Measured test time, in seconds, for the files whose cost is not proportional
# to how many tests they hold. Most entries below are from `pytest
# --durations=0` on GitHub's own runners, 2026-09-18, run 35301604006.
# Files absent from here are estimated from their test count, and a stale
# entry is a balance problem, never a correctness one -- see the module
# docstring's "Measure on the runner" note before trusting a local number.
#
# The next three were the run's five expensive tests and are now fixed at
# the source (see the module docstring); these are LOCAL post-fix numbers
# (this box, 2026-09-18), not yet re-measured on a runner, kept only so the
# balancer does not treat these three files as free. Refresh from a real
# shard job's `--durations` log once one has run with the fix.
SLOW_FILES: dict[str, float] = {
    "test_one_definition_per_quantity.py": 12.0,
    "test_tech_analyst.py": 1.0,
    "test_ops_scripts_importable.py": 16.0,
    "test_pipeline.py": 32.0,
    "test_cost_circuit.py": 30.2,
    "test_one_definition_guard.py": 29.1,
    "test_event_risk_calendar.py": 25.5,
    "test_news.py": 22.5,
    "test_status_board.py": 18.1,
    "test_credential_outage_fail_closed.py": 16.7,
    "test_db.py": 14.6,
    "test_order_fill_stream.py": 13.5,
    "test_market_data.py": 10.0,
    "test_agent_log_attribution.py": 8.6,
    "test_desk_reset.py": 8.5,
    "test_rehearsal_report_verdict.py": 8.0,
    "test_atr_is_wilder_everywhere.py": 7.7,
    "test_stop_writeback.py": 7.3,
    "test_alert_watchdog.py": 7.3,
    "test_api_contract.py": 6.8,
    "test_blocked_proposals.py": 6.8,
    "test_risk_based_sizing.py": 6.7,
    "test_fill_reconciliation.py": 6.6,
    "test_broker.py": 5.7,
    "test_provider_attempt_budget.py": 5.6,
    "test_bugfixes.py": 5.5,
}

# Mean measured seconds per test over the flat tail: 151.3s spread across the
# 4,454 tests in the 195 files not named above.
SECONDS_PER_TEST = 0.034

# Splitting used to also divide test_one_definition_per_quantity.py's three
# whole-repo AST scans across shards node-by-node, because running all three
# in one process meant paying the repo walk three times (~186s). That
# redundant walk is gone now that the three share one cached parse (see the
# module docstring) -- the file costs about what one of its own scans costs,
# so it is an ordinary whole-file unit like everything else below.


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


def items(tests_dir: Path = TESTS_DIR) -> list[tuple[str, float]]:
    """Every unit pytest will be asked to run, with its estimated seconds.

    A unit is always a whole file path -- see the module docstring for why
    this no longer needs to split any file's tests across shards. Returned
    sorted, so the split is identical in every shard job without any shared
    state.
    """
    return sorted((str(path), weight(path)) for path in test_files(tests_dir))


def split(shards: int, tests_dir: Path = TESTS_DIR) -> list[list[str]]:
    """Partition the test files into ``shards`` buckets of similar weight.

    Greedy longest-processing-time: heaviest file first, into whichever bucket
    is currently lightest. Ties break on bucket index so the result is stable.
    """
    if shards < 1:
        raise ValueError("shards must be >= 1")

    weighted = sorted(
        ((w, item) for item, w in items(tests_dir)),
        key=lambda pair: (-pair[0], pair[1]),
    )

    buckets: list[list[str]] = [[] for _ in range(shards)]
    totals = [0.0] * shards
    for w, f in weighted:
        target = min(range(shards), key=lambda i: (totals[i], i))
        buckets[target].append(f)
        totals[target] += w

    return [sorted(b) for b in buckets]


def check(shards: int, tests_dir: Path = TESTS_DIR) -> int:
    """Assert the split is a real partition of the suite. 0 if it is.

    This is the safety net, so it is deliberately paranoid: a file quietly
    dropped from the split is tests quietly not running, which would leave a
    green `pytest` check standing over code nobody tested.
    """
    expected = dict(items(tests_dir))
    buckets = split(shards, tests_dir)
    assigned = [i for b in buckets for i in b]
    problems = []

    if len(assigned) != len(set(assigned)):
        seen: set[str] = set()
        dupes = sorted({i for i in assigned if i in seen or seen.add(i)})  # type: ignore[func-returns-value]
        problems.append(f"unit(s) in more than one shard: {dupes}")
    missing = sorted(set(expected) - set(assigned))
    if missing:
        problems.append(f"unit(s) in no shard: {missing}")
    extra = sorted(set(assigned) - set(expected))
    if extra:
        problems.append(f"unit(s) not in the suite: {extra}")
    empty = [i for i, b in enumerate(buckets) if not b]
    if empty:
        problems.append(f"empty shard(s): {empty}")

    # Every test file must be represented.
    covered = set(assigned)
    absent = sorted(str(f) for f in test_files(tests_dir) if str(f) not in covered)
    if absent:
        problems.append(f"test file(s) no shard would run: {absent}")

    for line in problems:
        print(f"ci_shard: {line}", file=sys.stderr)
    if problems:
        return 1

    seconds = [sum(expected[i] for i in b) for b in buckets]
    print(
        f"ci_shard: {len(test_files(tests_dir))} files, {len(expected)} units, "
        f"{shards} shards"
    )
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
