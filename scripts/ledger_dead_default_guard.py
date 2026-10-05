"""A "dead default" excuse must name a real caller that really passes the argument.

A ledger row whose note says "Dead default (<path>::<symbol>): the caller passes this
explicitly" is removed from the made-up-number count on that claim alone. The citation
guards prove the named symbol EXISTS; they cannot prove the claim is TRUE, and one such
claim was false. This guard checks the claim itself, by AST, over the working tree:

  no citation      the note says "Dead default" but cites no `path.py::Symbol`
  unresolved       the cited file or symbol is not there
  self-citation    the cited symbol IS the definition the default belongs to -- the
                   same node in the row's own `site` file, by position, never by name:
                   a caller that happens to share the method's name is a caller
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


def _definition(tree: ast.Module, site_id: str, site: str) -> ast.AST | None:
    """The def the row's id names, located in its own `site` file by qualified name.

    `src/m.py` + `src.m.S.build(top_n)` resolves `S.build` inside `src/m.py`. Only
    a FunctionDef is a definition a default can belong to.
    """
    module = site[: -len(".py")].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    qual = _ID.sub(lambda m: m.group("method"), site_id)
    if not qual.startswith(module + "."):
        return None
    node = _find_symbol(tree, qual[len(module) + 1:])
    return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _same_node(a: ast.AST, b: ast.AST) -> bool:
    """Identity across two parses of one file: same kind at the same position."""
    return type(a) is type(b) and (a.lineno, a.col_offset) == (b.lineno, b.col_offset)


def _param_index(definition: ast.AST, param: str) -> int | None:
    """Position of `param` among the definition's arguments, `self`/`cls` excluded."""
    names = [a.arg for a in definition.args.posonlyargs + definition.args.args if a.arg not in ("self", "cls")]
    return names.index(param) if param in names else None


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
    site = entry.get("site")
    site_body = read(site) if isinstance(site, str) else None
    if site_body is None:
        return f"its site {site!r} cannot be read, so the cited caller cannot be told from the definition"
    try:
        definition = _definition(ast.parse(site_body), site_id, str(site))
    except SyntaxError:
        definition = None
    if definition is None:
        return f"the definition {site_id} is not found in {site}, so the cited caller cannot be told from it"
    if rel == site and _same_node(node, definition):
        return f"cited caller {rel}::{sym} is the definition itself, not a caller"
    calls = _calls(node, method)
    if not calls:
        return f"cited caller {rel}::{sym} never calls {method}"
    index = _param_index(definition, param)
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
