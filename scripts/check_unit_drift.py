#!/usr/bin/env python3
"""Detect systemd units on the box that no longer match the repository.

Deterministic and read-only. No LLM call, no daemon, no new alert path.

THE PROBLEM THIS CLOSES. QAMC's schedule *is* its systemd user units: six
ET-windowed trading sessions, the Mission Control API, and five maintenance
timers. Until 2026-08-31 the repository tracked six of the eleven services
and none of the six session timers. The seven units that actually run the
trading day existed nowhere but on one machine, and the one tracked copy
anybody might have installed — `quant-agent-daily.service` — still carried
`/home/yebo/quant-agent`, a path inherited from the upstream fork, which
would have broken the daily export on contact.

`check_deploy_drift.py` already answers "is the deployed checkout behind
what was merged?". It compares commits, and it is blind to this: a box can
sit exactly on `origin/main` while its installed units are something else
entirely, because installing a unit is a `cp` that git never sees. Nothing
compared INSTALLED against TRACKED. This script is that comparison.

DELIBERATELY COMPARED AGAINST THE DEPLOYED CHECKOUT, NOT `origin/main`.
That keeps the two checks disjoint and stops them alarming twice for one
condition. Right after a merge the box is behind: `check_deploy_drift.py`
says so, and this script correctly says the installed units still match the
checkout they were deployed from. The gap only this script can see is a
deploy that moved the checkout forward and forgot to copy the units — and a
unit added or edited by hand on the box, which no commit comparison can
ever notice.

What it reports, in four buckets:
  untracked   — installed on the box, absent from the repository. The
                dangerous one: a unit hand-added at 2am in six months' time
                is invisible to every other check this desk runs, and if
                the box were lost it would be unrebuildable.
  modified    — installed and tracked, but the bytes differ. Someone edited
                the box copy in place, or a deploy copied only some files.
  undeployed  — tracked but not installed. A new unit that never got its
                `cp`, or a unit deleted from the box by hand.
  not_enabled — installed, declares `[Install] WantedBy=`, but has no
                symlink in the corresponding `.wants` directory. The unit
                exists, is correct, and never fires. Silent by construction.

Byte-for-byte, comments included. That is not pedantry: deploys here are a
literal `cp scripts/systemd/* ~/.config/systemd/user/`, and the four units
tracked before this change are byte-identical to their installed copies,
rationale comments and all. A semantic comparison would quietly bless a box
copy whose comments no longer describe what it does.

Usage:
    scripts/check_unit_drift.py
    scripts/check_unit_drift.py --repo-path /home/qamc/quant-agent
    scripts/check_unit_drift.py --units-path ~/.config/systemd/user
    scripts/check_unit_drift.py --no-telegram   # print only, no push

Exit codes:
    0  in sync, or the check could not run
    1  the box's units diverge from the repository — a finding, not a crash
    3  the repo unit directory or the systemd user directory is missing —
       an operator problem, not a drift finding
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_REPO_PATH = "/home/qamc/quant-agent"
DEFAULT_UNITS_PATH = "/home/qamc/.config/systemd/user"
# Where unit files live inside the checkout.
REPO_UNIT_SUBDIR = "scripts/systemd"
UNIT_SUFFIXES = (".service", ".timer", ".path")
# The declared, reviewable list of deliberately paused units, tracked
# alongside the units it describes.
PAUSED_UNITS_FILENAME = "paused_units.yaml"


@dataclass
class UnitDriftReport:
    repo_dir: str
    units_dir: str
    repo_units: list[str] = field(default_factory=list)
    installed_units: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    undeployed: list[str] = field(default_factory=list)
    not_enabled: list[tuple[str, str]] = field(default_factory=list)
    # Declared-paused bookkeeping. `paused` is not an alarm: a unit on the
    # paused list that is installed-but-not-enabled is exactly what a
    # deliberate pause looks like. `paused_but_enabled` means the box and
    # the list disagree — someone re-enabled the unit without updating the
    # list — and IS drift. `paused_unknown` is an error in the list itself:
    # it names a unit this repo does not track.
    paused: list[tuple[str, str]] = field(default_factory=list)
    paused_but_enabled: list[tuple[str, str]] = field(default_factory=list)
    paused_unknown: list[str] = field(default_factory=list)
    paused_list_error: str | None = None
    error: str | None = None

    @property
    def checked(self) -> bool:
        return self.error is None

    @property
    def has_drift(self) -> bool:
        return self.checked and bool(
            self.untracked or self.modified or self.undeployed or self.not_enabled
            or self.paused_but_enabled or self.paused_unknown
        )


def list_units(directory: Path) -> list[str]:
    """Unit filenames in `directory`, sorted. Files only — `.wants`
    directories and stray symlinks are not units."""
    names = []
    for entry in directory.iterdir():
        if entry.name.endswith(UNIT_SUFFIXES) and entry.is_file():
            names.append(entry.name)
    return sorted(names)


def parse_wanted_by(path: Path) -> list[str]:
    """`WantedBy=` targets declared in a unit's `[Install]` section.

    Deliberately narrow: only `[Install]`. A `WantedBy` anywhere else is not
    what `systemctl enable` acts on.
    """
    targets: list[str] = []
    section = ""
    try:
        text = path.read_text()
    except OSError:
        return targets
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Install" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "WantedBy":
            targets.extend(t for t in value.split() if t)
    return targets


def is_enabled(units_dir: Path, unit_name: str, target: str) -> bool:
    """True when `<units_dir>/<target>.wants/<unit_name>` exists.

    `exists()` follows symlinks, so a dangling `.wants` link — an enable
    whose unit was later deleted — reads as not enabled, which is exactly
    what it behaves like.

    If the link is present but its target cannot be stat'd (a permission
    error: the check being run from an account that is not the unit
    owner's), the link's own presence is the answer. systemd runs as the
    owner and can read it. Reporting "not enabled" there would be an
    artifact of who ran the check, not a fact about the box.
    """
    link = units_dir / f"{target}.wants" / unit_name
    try:
        return link.exists()
    except OSError:
        return link.is_symlink()


def load_paused_units(repo_dir: Path) -> tuple[list[str], str | None]:
    """Unit names declared in `<repo_dir>/paused_units.yaml`.

    Missing file, or a file whose `paused_units` list is empty, means
    nothing is deliberately paused — the desk is fully running. That is
    not an error. A file that exists but cannot be parsed as expected IS
    an error: silently treating it as "nothing paused" would turn a typo
    in the list into six new false alarms with no clue why.
    """
    path = repo_dir / PAUSED_UNITS_FILENAME
    if not path.is_file():
        return [], None
    try:
        import yaml

        data = yaml.safe_load(path.read_text()) or {}
        entries = data.get("paused_units") or []
        names = [str(e["unit"]) for e in entries]
    except Exception as exc:  # noqa: BLE001 — a malformed list must not crash the check
        return [], f"could not read {path}: {exc!r}"
    return names, None


def build_report(
    repo_path: str = DEFAULT_REPO_PATH, units_path: str = DEFAULT_UNITS_PATH,
) -> UnitDriftReport:
    repo_dir = Path(repo_path).expanduser() / REPO_UNIT_SUBDIR
    units_dir = Path(units_path).expanduser()
    report = UnitDriftReport(repo_dir=str(repo_dir), units_dir=str(units_dir))

    if not repo_dir.is_dir():
        report.error = f"repo unit directory not found: {repo_dir}"
        return report
    if not units_dir.is_dir():
        report.error = f"systemd user directory not found: {units_dir}"
        return report

    report.repo_units = list_units(repo_dir)
    report.installed_units = list_units(units_dir)

    repo_set = set(report.repo_units)
    installed_set = set(report.installed_units)

    report.untracked = sorted(installed_set - repo_set)
    report.undeployed = sorted(repo_set - installed_set)

    for name in sorted(repo_set & installed_set):
        # Bytes, not text: an encoding or line-ending change is a real
        # difference in a file systemd parses.
        if (repo_dir / name).read_bytes() != (units_dir / name).read_bytes():
            report.modified.append(name)

    paused_names, report.paused_list_error = load_paused_units(repo_dir)
    paused_set = set(paused_names)
    report.paused_unknown = sorted(paused_set - repo_set)

    # Enablement is only meaningful for units that are actually installed.
    # An undeployed unit is already reported above; saying it is also not
    # enabled is the same finding twice. A unit on the paused list is
    # expected to be not-enabled — that goes to `paused`, not `not_enabled`,
    # so a deliberate pause never reads as an alarm.
    for name in sorted(installed_set):
        for target in parse_wanted_by(units_dir / name):
            if is_enabled(units_dir, name, target):
                if name in paused_set:
                    # The list says paused, the box says enabled: they
                    # disagree. That disagreement is the drift.
                    report.paused_but_enabled.append((name, target))
            elif name in paused_set:
                report.paused.append((name, target))
            else:
                report.not_enabled.append((name, target))

    return report


def format_alert(report: UnitDriftReport) -> str:
    lines = ["⚠️ QAMC systemd unit drift", f"box: {report.units_dir}",
             f"repo: {report.repo_dir}", ""]
    if report.untracked:
        lines.append("Installed on the box but NOT in the repository:")
        lines.extend(f"  {n}" for n in report.untracked)
        lines.append("")
    if report.modified:
        lines.append("Installed copy differs from the repository:")
        lines.extend(f"  {n}" for n in report.modified)
        lines.append("")
    if report.undeployed:
        lines.append("In the repository but NOT installed:")
        lines.extend(f"  {n}" for n in report.undeployed)
        lines.append("")
    if report.not_enabled:
        lines.append("Installed but not enabled (declares WantedBy, no .wants link):")
        lines.extend(f"  {n} -> {t}" for n, t in report.not_enabled)
        lines.append("")
    if report.paused_but_enabled:
        lines.append(
            "On the paused list but enabled on the box — the list and the "
            "box disagree:"
        )
        lines.extend(f"  {n} -> {t}" for n, t in report.paused_but_enabled)
        lines.append("")
    if report.paused_unknown:
        lines.append(
            f"Named in {PAUSED_UNITS_FILENAME} but not tracked in "
            f"{REPO_UNIT_SUBDIR}/ — error in the paused list:"
        )
        lines.extend(f"  {n}" for n in report.paused_unknown)
        lines.append("")
    lines.extend(_paused_section_lines(report))
    lines.append(
        "Deploy is: cp scripts/systemd/* ~/.config/systemd/user/ && "
        "systemctl --user daemon-reload"
    )
    return "\n".join(lines).rstrip()


def _paused_section_lines(report: UnitDriftReport) -> list[str]:
    """The 'not an alarm' section, rendered the same way whether the run is
    otherwise clean or already alerting on something else."""
    if not report.paused:
        return []
    lines = ["Deliberately paused (not an alarm):"]
    lines.extend(f"  {n} -> {t}" for n, t in report.paused)
    lines.append("")
    return lines


def format_ok(report: UnitDriftReport) -> str:
    base = (
        f"check_unit_drift: in sync — {len(report.repo_units)} unit"
        f"{'s' if len(report.repo_units) != 1 else ''} tracked and installed "
        f"identically"
    )
    paused_lines = _paused_section_lines(report)
    if not paused_lines:
        return base
    return "\n".join([base, ""] + paused_lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-path", default=DEFAULT_REPO_PATH)
    parser.add_argument("--units-path", default=DEFAULT_UNITS_PATH)
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="Print findings but don't push a Telegram alert.",
    )
    args = parser.parse_args(argv)

    # An unhandled exception would exit 1, and the unit lists 1 as a SUCCESS
    # exit status because 1 means "drift found". A crash would therefore be
    # swallowed as a clean finding that alerts nobody — the same shape of
    # silent-alarm defect PR #175 fixed. Exit 3 instead: an operator problem,
    # and the unit is left to mark itself failed.
    try:
        report = build_report(args.repo_path, args.units_path)
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all, see above
        print(f"check_unit_drift: check could not run: {exc!r}", file=sys.stderr)
        return 3

    if not report.checked:
        print(f"check_unit_drift: {report.error}", file=sys.stderr)
        return 3

    if report.paused_list_error:
        print(f"check_unit_drift: {report.paused_list_error}", file=sys.stderr)

    if not report.has_drift:
        print(format_ok(report))
        return 0

    message = format_alert(report)
    print(message)

    if not args.no_telegram:
        from src.notifier import TelegramNotifier

        notifier = TelegramNotifier()
        if notifier.enabled:
            notifier.send(message)
        else:
            print(
                "check_unit_drift: Telegram not configured; alert printed "
                "above only",
                file=sys.stderr,
            )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
