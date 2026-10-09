"""The repository-wide audit built on `tests.desk_output_guard`.

Lifted out of the detector module 2026-10-04 so the size ratchet holds: this
is the layer that walks every tracked file, applies the allow-list and checks
what each allow-listed file carries against a FIXED per-file list,
config/check_allowlists/struct_desk_output.txt. Each line is
`path | signal | short hash of the finding's excerpt`, so a real order or account
id is never copied into a second file. Nothing is compared with any trunk:
a finding not in the list fails, and so does a listed finding that no longer
occurs (stale entry). Detection is unchanged.
Manual sweep: `python -m tests.desk_output_audit` (`--all` to include the
allow-listed files).
"""

from __future__ import annotations

import hashlib
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from scripts import struct_allowlist
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
    "ops/model_policy/fixtures/sec_10q10k_pm_public_day_2026-09-14.json.gz": "SEC 10-Q/10-K HTML corpus fetched from data.sec.gov; public filings, not desk output",
    "ops/model_policy/fixtures/yf_daily_bars_pm_public_day_2026-09-14.json.gz": "Yahoo daily OHLCV bars for the screen universe; public market data, not desk output",
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
SPECIMEN_FILES = frozenset(
    {
        "tests/test_no_real_desk_output.py",
    }
)


def tracked_files(root: Path = PROJECT_ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "replace")
    return [p for p in out.split("\0") if p and p not in SPECIMEN_FILES]


@dataclass(frozen=True)
class Audit:
    """The whole-repository verdict, in the four shapes a reviewer needs."""

    #: Findings in files that are not on the allow-list at all.
    new_files: list[Finding]
    #: Allow-listed files carrying a finding the fixed list does not name:
    #: path -> (findings listed, findings now).
    grown: dict[str, tuple[int, int]]
    #: Only the NEW findings in those files, so the failure names the line.
    grown_findings: list[Finding]
    #: Allow-list entries that no longer earn their place: path or list line -> why.
    stale_entries: dict[str, str]
    #: Tracked files larger than the scan cap that nobody has accounted for.
    oversize_unlisted: list[str]

    def ok(self) -> bool:
        return not (self.new_files or self.grown or self.stale_entries or self.oversize_unlisted)


def finding_key(f: Finding) -> str:
    """Identity of one finding: its file, its signal and a hash of what it matched."""
    digest = hashlib.sha256(f.excerpt.encode("utf-8")).hexdigest()[:12]
    return f"{f.path} | {f.signal} | {digest}"


def _stale_entries(allowed, tracked: set[str], counts: dict[str, int], gone: Counter) -> dict[str, str]:
    """Allow-list entries, list lines and LARGE_BLOBS entries that no longer earn their place."""
    stale: dict[str, str] = {}
    for rel in allowed:
        if rel not in tracked:
            stale[rel] = "the file is no longer tracked — delete this allow-list entry"
        elif counts.get(rel, 0) == 0:
            stale[rel] = (
                "the file no longer trips the guard (redacted?) — delete this "
                "allow-list entry so the file is protected again"
            )
    for key in sorted(gone.elements()):
        stale[key] = "this list line no longer occurs in the file — delete it from struct_desk_output.txt"
    for rel in LARGE_BLOBS:
        if rel not in tracked:
            stale[rel] = "the file is no longer tracked — delete this LARGE_BLOBS entry"
    return stale


def audit_repo(root: Path = PROJECT_ROOT, directory: Path | None = None) -> Audit:
    allowed = allow_list()
    listed = Counter(struct_allowlist.load("desk_output", directory))
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
    now_keys: Counter = Counter()
    for rel, found in listed_hits.items():
        keys = Counter(finding_key(f) for f in found)
        now_keys.update(keys)
        new_ids = set((keys - listed).keys())
        if new_ids:
            grown[rel] = (sum(n for k, n in listed.items() if k.split(" | ")[0] == rel), len(found))
            grown_findings.extend(f for f in found if finding_key(f) in new_ids)

    stale = _stale_entries(allowed, tracked, counts, listed - now_keys)
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
