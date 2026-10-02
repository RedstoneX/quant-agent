"""The baseline regenerator must not quietly stop tracking a file.

On 2026-10-02 three separate changes each dropped a baseline entry without
anyone intending it. The cause was the same every time: the regenerator
measures the files in the CURRENT worktree, and a branch cut before a new
file was added simply does not contain that file -- so its entry looked
deletable and the whole baseline was rewritten without it. Once such a branch
lands, that file is no longer tracked by the ratchet for anybody, which is a
loosening of a shrink-only guard performed by accident.

The rule these tests pin: an entry may only be dropped when the file is gone
from the trunk as well, and passing an argument the script does not recognise
must never rewrite the baseline at all.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = "scripts.regen_file_size_baseline"


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", SCRIPT, *args],
        cwd=cwd, capture_output=True, text=True,
    )


def test_an_unrecognised_argument_writes_nothing() -> None:
    """`--help` fell through and regenerated the baseline for real."""
    baseline = ROOT / "tests" / "file_size_baseline.json"
    before = baseline.read_text()

    result = _run("--help", cwd=ROOT)

    assert result.returncode == 2, result.stdout
    assert "nothing written" in result.stdout
    assert baseline.read_text() == before, (
        "an unrecognised argument rewrote tests/file_size_baseline.json"
    )


def test_an_entry_for_a_file_this_branch_lacks_is_kept() -> None:
    """The exact shape that cost three entries: the branch predates the file.

    `files_on_trunk` is what tells the regenerator that a file it cannot see
    still exists for everyone else. With the file absent from the worktree
    but present on the trunk, its entry must survive.
    """
    from scripts.regen_file_size_baseline import files_on_trunk

    on_trunk = files_on_trunk()
    assert on_trunk, "could not read origin/main; the protection cannot work"

    baseline = json.loads((ROOT / "tests" / "file_size_baseline.json").read_text())
    absent_here = [p for p in baseline if not (ROOT / p).exists()]
    assert not absent_here, (
        "baseline entries name files that do not exist here: "
        f"{absent_here} -- if they are gone from the trunk too, remove them"
    )
    tracked_elsewhere = [p for p in baseline if p in on_trunk]
    assert tracked_elsewhere, "no baseline entry resolves on the trunk"
