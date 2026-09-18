#!/usr/bin/env python3
"""Daily board-hygiene check: is `docs/WORK.md` filling up unnoticed?

THE PROBLEM THIS CLOSES. `docs/WORK.md` is documented, at its own top, to
hold only OPEN work: finished items get written up in
`docs/INCIDENT_HISTORY.md` and deleted from the board. `tests/
test_status_board.py` enforces a 100,000-byte cap on the file and a CI
check (added separately, see `scripts.status_board.
find_closed_items_not_marked_done`) fails a merge that leaves a
finished-looking item still parked on the board. Both of those only fire
when somebody actually touches the repository. If nobody edits the board
for a stretch, neither check runs, and the file can fill silently until the
next edit hits the cap outright with no warning beforehand.

This script is the mechanical check for the quiet-week gap: a daily,
read-only look at the board's size and at whether anything currently on it
already looks finished. It duplicates no detection logic — it reads the
cap out of the test that owns it and calls the CI check's own function for
"does this look finished" — so the three places (this script, the CI gate,
and the test) can never quietly disagree about what "near the cap" or
"looks finished" means.

No LLM call, no trading, no write to the board. Modeled on
`scripts/check_unit_drift.py`: same read-only shape, same wrapper-script-
sources-.env reason, same "silent unless there is a finding" contract.

Usage:
    scripts/check_board_hygiene.py
    scripts/check_board_hygiene.py --repo-path /home/qamc/quant-agent
    scripts/check_board_hygiene.py --no-telegram   # print only, no push

Exit codes:
    0  healthy (nothing to report), or a finding was printed/pushed
    3  the check itself could not run (missing file, cap not found) — an
       operator problem, not a nightly finding
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_REPO_PATH = "/home/qamc/quant-agent"
WORK_MD_RELPATH = "docs/WORK.md"
CAP_TEST_RELPATH = "tests/test_status_board.py"
#: The exact test function that owns the cap. Scoping the regex search to
#: this function's own body (rather than the whole test file) stops an
#: unrelated `assert size <= ...` elsewhere in the file from being read as
#: the board's cap.
CAP_TEST_FUNCTION = "test_work_md_stays_under_a_hundred_thousand_bytes"
_CAP_ASSERT_RE = re.compile(r"assert\s+size\s*<=\s*([\d_]+)")

#: PROVISIONAL — not sourced from any owner ruling or repo doctrine, because
#: none states a "near the cap" share. Picked as the most defensible default:
#: it leaves 10% of the cap (10,000 bytes at the current 100,000-byte cap)
#: as runway, which is roughly double the ~5,200-byte (~5%) headroom the cap
#: itself was originally sized to leave above a freshly-trimmed board (see
#: CAP_TEST_FUNCTION's own docstring) — early enough to act before the file
#: is back to the state that cap was designed to just barely tolerate.
#: Revisit if the owner states an actual preference.
NEAR_CAP_SHARE = 0.90


@dataclass
class BoardHygieneReport:
    work_md_path: str
    cap_bytes: int | None = None
    size_bytes: int | None = None
    near_cap_share: float = NEAR_CAP_SHARE
    parked_items: list[str] = field(default_factory=list)
    cap_error: str | None = None
    parked_check_error: str | None = None

    @property
    def cap_known(self) -> bool:
        return self.cap_bytes is not None and self.size_bytes is not None

    @property
    def near_cap(self) -> bool:
        return self.cap_known and self.size_bytes >= self.near_cap_share * self.cap_bytes

    @property
    def has_finding(self) -> bool:
        return self.near_cap or bool(self.parked_items)


def read_cap_bytes(repo_path: Path) -> tuple[int | None, str | None]:
    """The board's byte cap, read out of the test that owns it — never
    re-typed here. Returns (cap, error); error is set (and cap is None)
    when the cap cannot be sourced, so the caller can degrade honestly
    instead of inventing a number."""
    test_file = repo_path / CAP_TEST_RELPATH
    if not test_file.is_file():
        return None, f"{CAP_TEST_RELPATH} not found"
    text = test_file.read_text()
    idx = text.find(f"def {CAP_TEST_FUNCTION}")
    if idx == -1:
        return None, f"{CAP_TEST_FUNCTION} not found in {CAP_TEST_RELPATH}"
    # The assert sits inside the function body; a generous slice comfortably
    # covers it without risking a match from some unrelated later test.
    window = text[idx:idx + 4000]
    m = _CAP_ASSERT_RE.search(window)
    if not m:
        return None, f"could not find the cap assertion inside {CAP_TEST_FUNCTION}"
    return int(m.group(1).replace("_", "")), None


def _load_finished_item_check():
    """The CI gate's own function
    (`scripts.status_board.find_closed_items_not_marked_done`), loaded once
    and never re-implemented here. Returns None when it cannot be reached,
    so the caller degrades to an explicit "check unavailable" instead of
    silently reporting zero parked items. A module-level indirection
    (rather than importing straight into `find_parked_finished_items`) so
    tests can substitute a stand-in without needing a real second
    `scripts/status_board.py` on disk.
    """
    try:
        from scripts.status_board import find_closed_items_not_marked_done
        return find_closed_items_not_marked_done
    except ImportError:
        return None


#: Overridable for tests; production always resolves this at call time via
#: `_load_finished_item_check()` when this is left at its default of None.
_finished_item_check_override = None


def find_parked_finished_items(repo_path: Path) -> tuple[list[str], str | None]:
    """Item descriptions that look finished but are still parked on the
    board, via the SAME function the CI gate uses — never a
    re-implementation."""
    check = _finished_item_check_override or _load_finished_item_check()
    if check is None:
        return [], "could not import the finished-item check"
    work_md = repo_path / WORK_MD_RELPATH
    try:
        return check(work_md), None
    except Exception as exc:  # noqa: BLE001 — must not crash a nightly read-only check
        return [], f"the finished-item check raised: {exc!r}"


_ITEM_NUMBER_RE = re.compile(r"^item (\d+)", re.I)


def item_numbers(parked_items: list[str]) -> list[str]:
    """Just the numbers named in each flagged description, in the order
    given — what the owner needs to find them, nothing else."""
    numbers = []
    for entry in parked_items:
        m = _ITEM_NUMBER_RE.match(entry.strip())
        if m:
            numbers.append(m.group(1))
    return numbers


def build_report(repo_path: str = DEFAULT_REPO_PATH) -> BoardHygieneReport:
    repo_dir = Path(repo_path).expanduser()
    report = BoardHygieneReport(work_md_path=str(repo_dir / WORK_MD_RELPATH))

    cap, cap_error = read_cap_bytes(repo_dir)
    report.cap_error = cap_error
    if cap is not None:
        report.cap_bytes = cap
        work_md = repo_dir / WORK_MD_RELPATH
        if work_md.is_file():
            report.size_bytes = work_md.stat().st_size
        else:
            report.cap_error = f"{WORK_MD_RELPATH} not found"

    parked, parked_error = find_parked_finished_items(repo_dir)
    report.parked_items = parked
    report.parked_check_error = parked_error

    return report


def format_message(report: BoardHygieneReport) -> str:
    """One short, plain-English message — no file paths, no jargon, no run
    IDs. The owner reads this on his phone and is not a developer."""
    parts = []
    numbers = item_numbers(report.parked_items)
    if numbers:
        if len(numbers) == 1:
            parts.append(
                f"Item {numbers[0]} looks finished but is still listed as "
                f"open on the work board. Write it up and take it off."
            )
        else:
            named = ", ".join(numbers)
            parts.append(
                f"{len(numbers)} items look finished but are still listed as "
                f"open on the work board (items {named}). Write them up and "
                f"take them off."
            )
    if report.near_cap:
        pct = round(100 * report.size_bytes / report.cap_bytes)
        parts.append(
            f"The work board is at {pct}% of its size limit and needs "
            f"finished items cleared out before it fills up."
        )
    return " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-path", default=DEFAULT_REPO_PATH)
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="Print findings but don't push a Telegram alert.",
    )
    args = parser.parse_args(argv)

    try:
        report = build_report(args.repo_path)
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all
        print(f"check_board_hygiene: check could not run: {exc!r}", file=sys.stderr)
        return 3

    if report.cap_error:
        print(f"check_board_hygiene: {report.cap_error}", file=sys.stderr)
        return 3
    if report.parked_check_error:
        print(f"check_board_hygiene: {report.parked_check_error}", file=sys.stderr)
        # Not fatal on its own: the size check may still be meaningful.

    if not report.has_finding:
        print(
            f"check_board_hygiene: healthy — {report.size_bytes} of "
            f"{report.cap_bytes} bytes, no parked finished items"
        )
        return 0

    message = format_message(report)
    print(message)

    if not args.no_telegram:
        from src.notifier import TelegramNotifier

        notifier = TelegramNotifier()
        if notifier.enabled:
            notifier.send(message)
        else:
            print(
                "check_board_hygiene: Telegram not configured; message "
                "printed above only",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
