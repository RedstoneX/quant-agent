"""Read the trunk's version of the tree at check time, so guards store nothing.

A guard that keeps its "before" picture in a committed file collides with every
other open change, goes stale, and can be quietly edited until it passes. The
cure (docs/GUARDS_WITHOUT_STORED_STATE.md) is to compute "before" from
``origin/main`` whenever the guard runs, and to **refuse** — never pass — when
that reference cannot be read.

Nothing here caches to disk. One ``git ls-tree`` plus one ``git cat-file
--batch`` reads every trunk blob a measurement needs in two processes.

COMPARE IDENTITIES, NEVER TOTALS
--------------------------------
A delta expressed as a count has a hole: a change that removes one offender and
adds a different one nets to zero and passes, so the new defect lands unnoticed
(found 2026-10-02 when proving a guard red needed TWO added offenders because
the branch had removed one). So every guard names each offending site by an
identity that survives line shifts -- the path, the kind, the enclosing scope
and the site's own source text -- and ``added_sites`` fails any identity the
working tree holds MORE copies of than the trunk. Removals are never a failure.

ONE MOMENT ON BOTH SIDES
------------------------
A ratchet is only meaningful when the "before" and the "after" are the same
moment's trunk. On ``pull_request`` CI, ``actions/checkout`` with no ``ref:``
checks out GitHub's *merge ref* -- the branch merged into whatever main was
when GitHub last computed it -- while a fresh fetch of ``origin/main`` resolves
to main's CURRENT tip. Measured 2026-10-04 on PRs 1158 and 1172: the tested
tree's main-side parent was ten commits behind main, so the tested tree held
``src/pipeline.py`` at 2529 lines while main held 1894, and the ratchet
reported a 600-line growth on a file neither branch touched. The same phantom
offender appeared on 17 of 18 red changes. That is not a strict rule biting; it
is a stale tree measured against a fresh trunk.

So ``trunk_rev()`` reads the main-side parent of the merge commit actually
under test -- the branch is judged against the real main it was merged with --
and falls back to ``origin/main`` everywhere else (direct push, local run). The
fallback is the strict direction: it can only make the comparison harsher,
never easier. The parent is trusted only when every one of these holds, so a
branch can never nominate its own reference: the run is a ``pull_request``
event, HEAD is a two-parent merge, HEAD's second parent is exactly the PR head
commit GitHub reports, and HEAD's first parent is an ancestor of the current
``origin/main``. This is deliberately NOT "compare against the merge base": a
merge base is a commit the branch picks by not merging, which would let a
branch delete something today's main still needs and pass.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Hashable, Iterable, Mapping, TypeVar

from scripts.trunk_refresh import refresh_trunk

Identity = TypeVar("Identity", bound=Hashable)

ROOT = Path(__file__).resolve().parent.parent

TRUNK = "origin/main"


class ReferenceUnavailable(RuntimeError):
    """``origin/main`` could not be read, so no comparison is possible.

    Guards raise this instead of passing. A guard that silently succeeds when
    its reference is missing is decoration.
    """


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _rev_parse(rev: str) -> str:
    out = _git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return out.stdout.strip() if out.returncode == 0 else ""


def _merge_ref_main_parent() -> str:
    """Main-side parent of the merge commit CI is testing, or "" if not one.

    Every condition below must hold; any doubt returns "" and the caller falls
    back to the current ``origin/main``, which is the stricter reference.
    """
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return ""
    event_path = os.environ.get("GITHUB_EVENT_PATH") or ""
    try:
        with open(event_path, encoding="utf-8") as fh:
            head_sha = json.load(fh)["pull_request"]["head"]["sha"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    out = _git("rev-list", "--parents", "-n", "1", "HEAD")
    parts = out.stdout.split()
    if out.returncode != 0 or len(parts) != 3:  # not "<merge> <p1> <p2>"
        return ""
    _, main_side, branch_side = parts
    if branch_side != str(head_sha):  # HEAD is not GitHub's merge of THIS head
        return ""
    if not _rev_parse(main_side):
        return ""
    # The reference must be a commit main actually contains, never one the
    # branch could have fabricated.
    if _git("merge-base", "--is-ancestor", main_side, TRUNK).returncode != 0:
        return ""
    return main_side


def trunk_rev() -> str:
    """The commit this tree is judged against: one moment on both sides.

    Resolved afresh on every call -- nothing is cached, because a cached
    reference outlives the repository state it was read from. Raises
    ``ReferenceUnavailable`` when ``origin/main`` cannot be read at all: a
    guard that passes without a reference is decoration. The merge-ref
    refinement below is only ever applied to a trunk that WAS read; it can
    never stand in for one that could not be.
    """
    stale_note = refresh_trunk(ROOT, TRUNK)
    tip = _rev_parse(TRUNK)
    if not tip:
        raise ReferenceUnavailable(
            f"cannot read {TRUNK}: this guard compares the working tree against the "
            f"trunk and stores nothing, so without {TRUNK} it REFUSES rather than "
            "pass. Fix: `git fetch origin main` locally; in CI, check out with "
            f"fetch-depth: 0. git said: ref not found. {stale_note}"
        )
    return _merge_ref_main_parent() or tip


def require_trunk() -> str:
    """Return the resolved trunk commit, or raise ``ReferenceUnavailable``."""
    return trunk_rev()


def trunk_paths(suffix: str = "") -> list[str]:
    """Every path tracked on ``origin/main``, optionally filtered by suffix."""
    require_trunk()
    out = _git("ls-tree", "-r", "--name-only", trunk_rev())
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
    rev = trunk_rev()
    request = "".join(f"{rev}:{p}\n" for p in paths).encode()
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


def added_sites(
    now: Iterable[Identity] | Mapping[Identity, int],
    before: Iterable[Identity] | Mapping[Identity, int],
) -> list[tuple[Identity, int, int]]:
    """Every identity the working tree holds more copies of than ``origin/main``.

    Each side is either the identities themselves (one per occurrence) or a
    mapping ``{identity: occurrences}``; both become a multiset. Returns
    ``(identity, copies_now, copies_on_trunk)`` sorted by identity. An
    identity that disappeared is never reported; an identity that is new, or
    that now occurs more often, always is -- even when some unrelated site was
    removed in the same change, which is exactly the case a total would hide.
    """
    have, had = Counter(now), Counter(before)
    return sorted(
        (key, n, had.get(key, 0)) for key, n in have.items() if n > had.get(key, 0)
    )


def enclosing_scopes(tree: ast.AST) -> dict[int, str]:
    """Map ``id(node)`` to the dotted name of the def/class that encloses it.

    Module-level nodes map to ``"<module>"``. Guards use this so a site's
    identity is "which function, which source text", which survives the line
    shifts that a line number would not.
    """
    scopes: dict[int, str] = {}

    def walk(node: ast.AST, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            scopes[id(child)] = qual
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, f"{qual}.{child.name}" if qual != "<module>" else child.name)
            else:
                walk(child, qual)

    walk(tree, "<module>")
    return scopes


def site_identity(node: ast.AST, scopes: dict[int, str]) -> tuple[str, str]:
    """``(enclosing scope, source text)`` for one offending AST node."""
    return scopes.get(id(node), "<module>"), ast.unparse(node)
