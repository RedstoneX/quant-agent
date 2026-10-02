"""Read the trunk's version of the tree at check time, so guards store nothing.

A guard that keeps its "before" picture in a committed file collides with every
other open change, goes stale, and can be quietly edited until it passes. The
cure (docs/GUARDS_WITHOUT_STORED_STATE.md) is to compute "before" from
``origin/main`` whenever the guard runs, and to **refuse** — never pass — when
that reference cannot be read.

Nothing here caches to disk. One ``git ls-tree`` plus one ``git cat-file
--batch`` reads every trunk blob a measurement needs in two processes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TRUNK = "origin/main"


class ReferenceUnavailable(RuntimeError):
    """``origin/main`` could not be read, so no comparison is possible.

    Guards raise this instead of passing. A guard that silently succeeds when
    its reference is missing is decoration.
    """


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def require_trunk() -> str:
    """Return the resolved ``origin/main`` commit, or raise ``ReferenceUnavailable``."""
    out = _git("rev-parse", "--verify", "--quiet", f"{TRUNK}^{{commit}}")
    sha = out.stdout.strip()
    if out.returncode != 0 or not sha:
        raise ReferenceUnavailable(
            f"cannot read {TRUNK}: this guard compares the working tree against the "
            f"trunk and stores nothing, so without {TRUNK} it REFUSES rather than "
            "pass. Fix: `git fetch origin main` locally; in CI, check out with "
            "fetch-depth: 0. git said: " + (out.stderr.strip() or "ref not found")
        )
    return sha


def trunk_paths(suffix: str = "") -> list[str]:
    """Every path tracked on ``origin/main``, optionally filtered by suffix."""
    require_trunk()
    out = _git("ls-tree", "-r", "--name-only", TRUNK)
    if out.returncode != 0:
        raise ReferenceUnavailable(f"git ls-tree {TRUNK} failed: {out.stderr.strip()}")
    return sorted(p for p in out.stdout.splitlines() if p.endswith(suffix))


def working_paths(pattern: str) -> list[str]:
    """Every tracked path in the working tree matching a git pathspec."""
    out = _git("ls-files", pattern)
    if out.returncode != 0:
        raise ReferenceUnavailable(f"git ls-files failed: {out.stderr.strip()}")
    return sorted(p for p in out.stdout.splitlines() if (ROOT / p).is_file())


def trunk_blobs(paths: list[str]) -> dict[str, str]:
    """Contents of each path as it stands on ``origin/main``, in one git call.

    Paths absent from the trunk are simply omitted — that is how a new file is
    recognised, not an error.
    """
    require_trunk()
    if not paths:
        return {}
    request = "".join(f"{TRUNK}:{p}\n" for p in paths).encode()
    proc = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=ROOT, input=request, capture_output=True
    )
    if proc.returncode != 0:
        raise ReferenceUnavailable(f"git cat-file on {TRUNK} failed: {proc.stderr.decode()}")

    blobs: dict[str, str] = {}
    data, pos = proc.stdout, 0
    for path in paths:
        nl = data.find(b"\n", pos)
        if nl == -1:
            raise ReferenceUnavailable(f"git cat-file output ended early at {path}")
        header = data[pos:nl].decode(errors="replace").split()
        pos = nl + 1
        if len(header) < 3 or header[1] != "blob":  # "<name> missing"
            continue
        size = int(header[2])
        blobs[path] = data[pos:pos + size].decode("utf-8", errors="replace")
        pos += size + 1  # trailing newline git appends after the payload
    return blobs
