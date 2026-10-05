"""A "dead default" excuse must name a real caller that really passes the argument.

A ledger row whose note says "Dead default (<path>::<symbol>): the caller passes this
explicitly" is removed from the made-up-number count on that claim alone. The citation
guards prove the named symbol EXISTS; they cannot prove the claim is TRUE, and one such
claim was false. This guard checks the claim itself, by AST, over the working tree:

  no citation      the note says "Dead default" but cites no `path.py::Symbol`
  unresolved       the cited file or symbol is not there
  self-citation    the cited symbol is the definition the default belongs to
  no call          the cited symbol never calls the method named in the row's id
  not passed       it calls it but never passes the argument named in the row's id
                   (by keyword, or positionally at a position that reaches it)

ABSOLUTE: there is no trunk baseline, so a bad excuse fails the moment it is written.
A refusal to read the tree is reported as REFUSED (exit 2), never as a violation.

Run: ``python -m scripts.ledger_dead_default_guard``.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from scripts.guard_reference import ROOT
from scripts.ledger_locator import working_ledger
from scripts.ledger_substantiation_guard import _find_symbol

Reader = Callable[[str], "str | None"]

_MARK = re.compile(r"Dead default")
_CITE = re.compile(
    r"Dead default[^()]*?\(\s*(?:only caller:\s*)?(?P<path>[\w/.\-]+\.py)::(?P<sym>[\w.]+)\s*\)"
)
_ID = re.compile(r"(?P<method>\w+)\((?P<param>\w+)\)$")


def _calls(node: ast.AST, method: str) -> list[ast.Call]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr == method) or (isinstance(f, ast.Name) and f.id == method):
                out.append(n)
    return out


def _param_index(tree: ast.Module, method: str, param: str) -> int | None:
    """Position of `param` among the callee's arguments, `self`/`cls` excluded."""
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method:
            names = [a.arg for a in n.args.posonlyargs + n.args.args if a.arg not in ("self", "cls")]
            if param in names:
                return names.index(param)
    return None


def _passes(call: ast.Call, param: str, index: int | None) -> bool:
    if any(k.arg == param for k in call.keywords):
        return True
    return index is not None and len(call.args) > index and not any(isinstance(a, ast.Starred) for a in call.args)


def check_row(entry: dict[str, Any], read: Reader) -> str | None:
    """Why this row's dead-default excuse is not true, or None when it holds (or makes none)."""
    note = " ".join(str(entry.get("note") or "").split())
    if not _MARK.search(note):
        return None
    site_id = str(entry.get("id"))
    idm = _ID.search(site_id)
    cite = _CITE.search(note)
    if cite is None:
        return "claims a dead default but cites no caller as path.py::Symbol"
    if idm is None:
        return "claims a dead default but its id names no method(param) to look for"
    method, param = idm.group("method"), idm.group("param")
    rel, sym = cite.group("path"), cite.group("sym")
    body = read(rel)
    if body is None:
        return f"cited caller file {rel} does not exist"
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return f"cited caller file {rel} does not parse"
    node = _find_symbol(tree, sym)
    if node is None:
        return f"cited caller {rel}::{sym} is not defined"
    if getattr(node, "name", None) == method:
        return f"cited caller {rel}::{sym} is the definition itself, not a caller"
    calls = _calls(node, method)
    if not calls:
        return f"cited caller {rel}::{sym} never calls {method}"
    site = entry.get("site")
    site_body = read(site) if isinstance(site, str) else None
    index = None
    if site_body is not None:
        try:
            index = _param_index(ast.parse(site_body), method, param)
        except SyntaxError:
            index = None
    if not any(_passes(c, param, index) for c in calls):
        return f"cited caller {rel}::{sym} calls {method} but never passes {param}"
    return None


def check(ledger_text: str, read: Reader) -> list[str]:
    rows = (yaml.safe_load(ledger_text) or {}).get("numbers") or []
    out = []
    for e in rows:
        why = check_row(e, read)
        if why:
            out.append(f"{e.get('id')}: dead-default excuse is not true: {why}")
    return out


def violations(root: Path = ROOT) -> list[str]:
    text = (root / working_ledger(root)).read_text(encoding="utf-8")

    def read(rel: str) -> str | None:
        p = root / rel
        return p.read_text(encoding="utf-8") if p.is_file() else None

    return check(text, read)


def main() -> int:
    try:
        bad = violations()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(f"dead-default excuses refused: {len(bad)}")
    for line in bad:
        print("  " + line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
