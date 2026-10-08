#!/usr/bin/env python3
"""Refuse a change that edits the board file together with anything outside docs/.

WHY. `docs/WORK.md` is one shared file. Between 2026-09-29 and 2026-10-06,
198 of 672 merges edited it, so almost any two changes in flight collided on
it, and a collision stalls the change until someone rebuilds it. Code changes
now leave the board alone; the orchestrator records board updates in their
own small docs-only change after the code has merged.

The changed set is this branch's own work: everything since it last shared
history with main. On a pull_request run HEAD is GitHub's test merge, whose
merge-base with main is main itself.
"""
from __future__ import annotations

import subprocess
import sys

BOARD = "docs/WORK.md"


def violation(paths: list[str]) -> list[str]:
    """The non-docs paths that make this change illegal, or [] if it is fine."""
    if BOARD not in paths:
        return []
    return sorted(p for p in paths if not p.startswith("docs/"))


def _changed(base: str) -> list[str]:
    mb = subprocess.run(["git", "merge-base", base, "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    out = subprocess.run(["git", "diff", "--name-only", mb, "HEAD"], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    base = (argv or sys.argv[1:] or ["origin/main"])[0]
    bad = violation(_changed(base))
    if bad:
        print(f"::error::this change edits {BOARD} together with code: {', '.join(bad[:10])}. "
              "Ship the code alone; record the board update in its own docs-only change after it merges.")
        return 1
    print("board edits travel alone: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
