"""Statement-cramming ratchet: no tracked .py file may gain a line that holds
more than one statement, measured against ``origin/main`` at check time.

Why: the file-size ratchet (``scripts/file_size_guard.py``) counts lines, and
on 2026-10-04 a change bought headroom under it by joining statements onto
single lines (``from A import x; from B import y``, ``if cond: return x``)
instead of splitting the file. The file stayed exactly as unreadable; only the
counter moved. Compressing is the same offence as raising the limit, so this
guard refuses it mechanically.

How: the code is PARSED, never pattern-matched. Every statement in a file has a
line number; a line is "crammed" when more than one statement starts on it, or
when a block body starts on its own header's line (``if c: return x``,
``except E: pass``, ``else: go()``). Semicolons inside strings, docstrings and
comments are therefore invisible to it. There is no threshold: the count is
statements per line, and the only question is whether this tree holds MORE
crammed lines (by identity: path + enclosing scope + the line's text) than the
trunk does. Pre-existing ones never fail; new ones always do.

Scope is exactly what the size ratchet guards -- every tracked ``.py`` file
(``guard_reference.working_paths("*.py")``). No second list exists.

Stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md). If ``origin/main`` cannot
be read it REFUSES; it never passes by default.

Run it directly: ``python -m scripts.statement_cram_guard``.
"""
from __future__ import annotations

import ast
import re
import sys
from collections import Counter

from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    added_sites,
    enclosing_scopes,
    trunk_blobs,
    working_paths,
)

Identity = tuple[str, str, str]  # (path, enclosing scope, stripped line text)

# A block body on its header's line is only reachable through these keywords,
# which own no AST node of their own; the first statement of such a body
# starts on the keyword's line when it is crammed. This runs on ONE line that
# the parser has already told us begins a statement -- it is not a text scan.
_BARE_HEADER = re.compile(r"^\s*(else|finally)\s*:")

# Bodies that have no statement to split: a stub is not cramming.
_STUB = (ast.Pass,)


def _is_stub(body: list[ast.stmt]) -> bool:
    if len(body) != 1:
        return False
    node = body[0]
    if isinstance(node, _STUB):
        return True
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and node.value.value is Ellipsis


def crammed_lines(text: str, tree: ast.Module | None = None) -> list[tuple[int, ast.stmt]]:
    """``(line number, a statement on it)`` for each line holding more than one
    statement, or a body whose first statement sits on its header's line."""
    tree = ast.parse(text) if tree is None else tree
    lines = text.splitlines()
    first_on_line: dict[int, ast.stmt] = {}
    crammed: dict[int, ast.stmt] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            seen = first_on_line.setdefault(node.lineno, node)
            if seen is not node:
                crammed.setdefault(node.lineno, seen)
        # Bodies on their header's line. ``if c: return x`` is already two
        # statements on one line (If + Return); handlers and cases have a line
        # number but are not statements, and ``else``/``finally`` have no node.
        if isinstance(node, (ast.ExceptHandler, ast.match_case)) and node.body:
            first = node.body[0]
            if first.lineno == node.lineno and not _is_stub(node.body):
                crammed.setdefault(first.lineno, first)
        for field in ("orelse", "finalbody"):
            body = getattr(node, field, None)
            if not isinstance(body, list) or not body or not isinstance(body[0], ast.stmt):
                continue
            first = body[0]
            if isinstance(node, ast.If) and isinstance(first, ast.If) and len(body) == 1:
                continue  # ``elif`` -- its own header line, caught above if crammed
            if _BARE_HEADER.match(lines[first.lineno - 1]) and not _is_stub(body):
                crammed.setdefault(first.lineno, first)

    # A compound statement whose one-line body is a bare stub is not cramming.
    for lineno, node in list(crammed.items()):
        body = getattr(node, "body", None)
        if (
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and isinstance(body, list)
            and _is_stub(body)
            and sum(1 for n in ast.walk(node) if isinstance(n, ast.stmt) and n.lineno == lineno) == 2
        ):
            del crammed[lineno]
    return sorted(crammed.items())


def sites(path: str, text: str) -> Counter:
    """Multiset of crammed-line identities in one file's source."""
    tree = ast.parse(text)
    scopes = enclosing_scopes(tree)
    lines = text.splitlines()
    out: Counter = Counter()
    for lineno, node in crammed_lines(text, tree):
        out[(path, scopes.get(id(node), "<module>"), lines[lineno - 1].strip())] += 1
    return out


def working_sites() -> Counter:
    out: Counter = Counter()
    for p in working_paths("*.py"):
        out.update(sites(p, (ROOT / p).read_text(encoding="utf-8", errors="replace")))
    return out


def trunk_sites(paths: list[str]) -> Counter:
    out: Counter = Counter()
    for p, text in trunk_blobs(paths).items():
        try:
            out.update(sites(p, text))
        except SyntaxError:
            continue  # an unparseable trunk copy offers no pre-existing sites
    return out


def violations() -> list[str]:
    now = working_sites()
    before = trunk_sites(sorted({path for path, _, _ in now}))
    return [
        f"{path} [{scope}]: {n} crammed line(s) where {TRUNK} has {had}: `{line}` "
        f"-- one statement per line; split the file instead of compressing it."
        for (path, scope, line), n, had in added_sites(now, before)
    ]


def main(argv: list[str] | None = None) -> int:
    try:
        bad = violations()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        print("Statements crammed onto shared lines against %s:\n%s" % (TRUNK, "\n".join(bad)), file=sys.stderr)
        return 1
    print(f"statement-cram ratchet: no tracked .py file gained a multi-statement line against {TRUNK}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
