"""The repository-wide audit built on `tests.desk_output_guard`.

Lifted out of the detector module 2026-10-04 so the size ratchet holds: this
is the layer that walks every tracked file, applies the allow-list and asks
`origin/main` what each allow-listed file ALREADY carried. Nothing here is
stored: what a file was allowed to hold is measured on the trunk at check time.
Manual sweep: `python -m tests.desk_output_audit` (`--all` to include the
allow-listed files).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.guard_reference import added_sites, trunk_blobs
from tests.desk_output_guard import PROJECT_ROOT, Finding, allow_list, read_text, scan_text


# Scanning is proportionate to file size, and this repo commits third-party
# bulk snapshots that dwarf everything else — the 10-Q corpus decompresses to
# 148 MB of SEC HTML and alone costs more than the rest of the repo together.
# Past this cap only the first SCAN_BYTE_CAP characters are scanned. The cap is
# not a quiet hiding place: `test_no_real_desk_output.py` fails on any tracked
# file over the cap that is not named in LARGE_BLOBS with a reason, so putting
# desk output past byte 4,000,000 of a new giant file takes a reviewed entry.
SCAN_BYTE_CAP = 4_000_000

LARGE_BLOBS: dict[str, str] = {
    "ops/model_policy/fixtures/sec_10q10k_pm_public_day_2026-09-14.json.gz":
        "SEC 10-Q/10-K HTML corpus fetched from data.sec.gov; public filings, not desk output",
    "ops/model_policy/fixtures/yf_daily_bars_pm_public_day_2026-09-14.json.gz":
        "Yahoo daily OHLCV bars for the screen universe; public market data, not desk output",
}


# `test_no_real_desk_output.py` has to hold strings shaped exactly like real
# desk output — a real-looking ticker, cent-precision prices, a broker order
# id, a broker account number, a production log line — or it cannot prove the
# five signals actually fire. Those strings are invented for that one purpose;
# none of them ever came off the desk. Scanning that file for desk output
# means scanning the detector's own test specimens, which is not what this
# module is for.
#
# This is an exact single-path exclusion, not a directory or a glob:
# `test_the_specimen_exclusion_is_exactly_this_one_file` pins the set below to
# exactly this path, so it cannot quietly grow into a hiding place. A new file
# dropped anywhere else, including beside this one, is scanned like any other.
SPECIMEN_FILES = frozenset({
    "tests/test_no_real_desk_output.py",
})


def tracked_files(root: Path = PROJECT_ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True,
    ).stdout.decode("utf-8", "replace")
    return [p for p in out.split("\0") if p and p not in SPECIMEN_FILES]


@dataclass(frozen=True)
class Audit:
    """The whole-repository verdict, in the four shapes a reviewer needs."""

    #: Findings in files that are not on the allow-list at all.
    new_files: list[Finding]
    #: Allow-listed files carrying a finding their `origin/main` copy does
    #: not: path -> (findings on the trunk, findings now).
    grown: dict[str, tuple[int, int]]
    #: Only the NEW findings in those files, so the failure names the line.
    grown_findings: list[Finding]
    #: Allow-list entries that no longer earn their place: path -> why.
    stale_entries: dict[str, str]
    #: Tracked files larger than the scan cap that nobody has accounted for.
    oversize_unlisted: list[str]

    def ok(self) -> bool:
        return not (
            self.new_files or self.grown
            or self.stale_entries or self.oversize_unlisted
        )


def _identity(f: Finding) -> tuple[str, str]:
    return (f.signal, f.excerpt)


def _trunk_findings(paths: list[str]) -> dict[str, list[Finding]]:
    """Each allow-listed file scanned as it stands on `origin/main`.

    Raises `ReferenceUnavailable` when the trunk cannot be read: the audit then
    REFUSES rather than treating an unreadable trunk as "nothing was there".
    A path absent from the trunk is a new file; everything in it is new.
    """
    blobs = trunk_blobs(sorted(paths))
    return {p: scan_text(t[:SCAN_BYTE_CAP], p) for p, t in blobs.items()}


def audit_repo(root: Path = PROJECT_ROOT) -> Audit:
    allowed = allow_list()
    tracked = set(tracked_files(root))
    new_files: list[Finding] = []
    listed_hits: dict[str, list[Finding]] = {}
    counts: dict[str, int] = {}
    oversize_unlisted: list[str] = []

    for rel in sorted(tracked):
        text = read_text(root / rel)
        if text is None:
            continue
        if len(text) > SCAN_BYTE_CAP and rel not in LARGE_BLOBS:
            oversize_unlisted.append(rel)
        found = scan_text(text[:SCAN_BYTE_CAP], rel)
        counts[rel] = len(found)
        if not found:
            continue
        if rel not in allowed:
            new_files.extend(found)
            continue
        listed_hits[rel] = found

    grown: dict[str, tuple[int, int]] = {}
    grown_findings: list[Finding] = []
    before = _trunk_findings(list(listed_hits))
    for rel, found in listed_hits.items():
        was = before.get(rel, [])
        added = added_sites(map(_identity, found), map(_identity, was))
        if added:
            new_ids = {key for key, _now, _then in added}
            grown[rel] = (len(was), len(found))
            grown_findings.extend(f for f in found if _identity(f) in new_ids)

    stale: dict[str, str] = {}
    for rel in allowed:
        if rel not in tracked:
            stale[rel] = "the file is no longer tracked — delete this allow-list entry"
        elif counts.get(rel, 0) == 0:
            stale[rel] = (
                "the file no longer trips the guard (redacted?) — delete this "
                "allow-list entry so the file is protected again"
            )
    for rel in LARGE_BLOBS:
        if rel not in tracked:
            stale[rel] = "the file is no longer tracked — delete this LARGE_BLOBS entry"

    return Audit(new_files, grown, grown_findings, stale, oversize_unlisted)


def scan_repo(root: Path = PROJECT_ROOT, *, skip_allowed: bool = True) -> list[Finding]:
    """Every finding, optionally excluding allow-listed files. For the CLI."""
    allowed = allow_list()
    findings: list[Finding] = []
    for rel in tracked_files(root):
        if skip_allowed and rel in allowed:
            continue
        text = read_text(root / rel)
        if text is None:
            continue
        findings.extend(scan_text(text[:SCAN_BYTE_CAP], rel))
    return findings


if __name__ == "__main__":  # manual sweep: python -m tests.desk_output_audit
    import sys

    hits = scan_repo(skip_allowed="--all" not in sys.argv)
    for f in hits:
        print(f.render())
    print(f"\n{len(hits)} finding(s) in {len({f.path for f in hits})} file(s)")
    sys.exit(1 if hits else 0)
