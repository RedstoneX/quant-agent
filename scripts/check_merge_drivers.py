#!/usr/bin/env python3
"""Fail when .gitattributes names a custom merge driver this clone has not registered.

WHY: `.gitattributes` maps the board documents to `merge=docsmerge`, but git
only honours that if `merge.docsmerge.driver` is also set in the clone's own
config. It never was, so the mapping was silently inert from the day it
shipped. The failure mode is silence; this makes it loud, and `--install`
(also run by tests/conftest.py at collection) makes it self-healing.

Driver names are DERIVED from .gitattributes at check time; no name and no
path is written down here. Built-in strategies need no registration.

Usage: check_merge_drivers.py [--install] [repo_dir]   exit 0 ok, 1 missing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Built into git itself (gitattributes(5)); never registered via config.
BUILTIN_STRATEGIES = frozenset({"text", "binary", "union"})
# Also not drivers: attribute states, not names.
ATTRIBUTE_STATES = frozenset({"set", "unset", "unspecified"})


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=20)


def custom_drivers(repo: Path) -> dict[str, list[str]]:
    """{driver name: [patterns]} for every merge=<name> that is not built in."""
    found: dict[str, list[str]] = {}
    attrs = repo / ".gitattributes"
    if not attrs.is_file():
        return found
    for raw in attrs.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *tokens = line.split()
        for tok in tokens:
            if tok.startswith("merge="):
                name = tok[len("merge="):]
                if name not in BUILTIN_STRATEGIES | ATTRIBUTE_STATES:
                    found.setdefault(name, []).append(pattern)
    return found


def unregistered(repo: Path) -> list[str]:
    """Problems: one line per driver with no usable registered command."""
    problems = []
    for name, patterns in sorted(custom_drivers(repo).items()):
        r = _git(repo, "config", "--get", f"merge.{name}.driver")
        cmd = r.stdout.strip() if r.returncode == 0 else ""
        if not cmd:
            problems.append(f"merge driver '{name}' (used by {', '.join(patterns)}) "
                            f"is NOT registered in this clone")
            continue
        script = cmd.split()[0]
        if "/" in script and not (repo / script).exists():
            problems.append(f"merge driver '{name}' points at missing script {script}")
    return problems


def install(repo: Path) -> None:
    subprocess.run(["bash", str(repo / "scripts/install_git_merge_drivers.sh")],
                   capture_output=True, text=True, timeout=30)


def main(argv: list[str]) -> int:
    do_install = "--install" in argv
    rest = [a for a in argv if not a.startswith("--")]
    repo = Path(rest[0]) if rest else Path(__file__).resolve().parent.parent
    problems = unregistered(repo)
    if problems and do_install:
        install(repo)
        problems = unregistered(repo)
    for p in problems:
        print(f"FAIL: {p}. Fix: bash scripts/install_git_merge_drivers.sh", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
