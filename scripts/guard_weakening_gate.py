"""Refuse a change that edits a guard unless a commit justifies it.

Why: a change was nearly merged that loosened the file-size ratchet so two
blocked changes would go green; the loosened rule was gameable (delete a
300-line test file, grow a monolith by 299 lines). Nothing in CI objected.

WHICH FILES ARE GUARDS (derived, never listed -- a list rots)
    scripts/*guard*.py          any script whose name says guard
    tests/test_*guard*.py       any test whose name says guard
    tests/test_*ratchet*.py     any test whose name says ratchet
A new guard that follows the naming rule is covered the day it is added.

WHAT IS CAUGHT
    Any edit or deletion of an existing guard file whose behaviour could have
    changed. Tightening cannot be told from loosening reliably (a threshold's
    direction depends on the guard), so the rule is deliberately over-asking:
    EVERY behavioural edit needs the justification. The only exemptions are
    provable no-ops: a new guard file (it only adds refusals), and a .py edit
    whose AST is identical once docstrings are stripped (comments, blank lines
    and formatting never reach the AST).

THE JUSTIFICATION
    A commit message line, ONE PHYSICAL LINE, starting `Guard-rule-change:`
    of at least 25 words that (a) names the mechanism making the existing rule
    incorrect and (b) states why the new rule cannot be gamed. Word count is
    mechanical; (a)/(b) are for the reviewer and the adversary.

STORES NOTHING. Computed against origin/main at check time; when the base
cannot be read the gate FAILS -- inability to compare is never a pass.
"""
from __future__ import annotations

import ast
import fnmatch
import re
import sys
from pathlib import Path

try:
    from scripts import definition_of_done as dod
except ImportError:  # run as a bare script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import definition_of_done as dod

GUARD_PATTERNS = ("scripts/*guard*.py", "tests/test_*guard*.py",
                  "tests/test_*ratchet*.py")
MIN_WORDS = 25
LINE = re.compile(r"^[ \t]*Guard-rule-change[ \t]*:[ \t]*(.+?)[ \t]*$", re.M)

REQUIREMENT = (
    "a change that edits a guard must carry, on ONE PHYSICAL LINE of a commit "
    f"message, `Guard-rule-change:` followed by at least {MIN_WORDS} words that "
    "(a) name the mechanism making the existing rule incorrect and (b) state "
    "why the new rule cannot be gamed")


def is_guard(path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, p) and path.count("/") == p.count("/")
               for p in GUARD_PATTERNS)


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return tree


def behaviour_unchanged(before: str, after: str) -> bool:
    """True only when provably the same code: identical AST sans docstrings."""
    try:
        a = ast.dump(_strip_docstrings(ast.parse(before)))
        b = ast.dump(_strip_docstrings(ast.parse(after)))
    except SyntaxError:
        return False
    return a == b


def problems(base: str | None, repo: Path | None = None,
             messages: str | None = None) -> list[str]:
    if not base:
        return ["cannot read origin/main to compare against; refusing rather "
                "than passing (inability to compare is never a pass)"]
    repo = repo or dod.REPO_ROOT
    r = dod._git("diff", "--name-only", "--no-renames", f"{base}...HEAD",
                 repo=repo)
    if r.returncode != 0:
        return ["git diff against the base failed; refusing: " + r.stderr.strip()]
    touched = []
    for path in sorted(p for p in r.stdout.split("\n") if p and is_guard(p)):
        before = dod.file_at(base, path, repo)
        if before is None:
            continue  # brand-new guard: only adds refusals
        f = repo / path
        after = f.read_text() if f.exists() else None
        if after is not None and behaviour_unchanged(before, after):
            continue
        touched.append(path + (" (deleted)" if after is None else ""))
    if not touched:
        return []
    if messages is None:
        log = dod._git("log", "--format=%B%n", f"{base}..HEAD", repo=repo)
        if log.returncode != 0:
            return ["git log failed; refusing: " + log.stderr.strip()]
        messages = log.stdout
    if any(len(m.split()) >= MIN_WORDS for m in LINE.findall(messages)):
        return []
    return [f"guard file(s) changed without justification: "
            f"{', '.join(touched)}. Requirement: {REQUIREMENT}."]


def main() -> int:
    out = problems(dod.base_ref())
    for p in out:
        print(f"- {p}")
    return 1 if out else 0


if __name__ == "__main__":
    raise SystemExit(main())
