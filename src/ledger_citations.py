"""Ledger citation guard (rule 7, board item 225). Resolves each citation."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any


#: Every repo path mentioned anywhere in an entry's prose, with how it points.
#: Three forms, in order of preference:
#:   `path::Qual.name`  - a SYMBOL (python files): survives a file's lines
#:                        moving, and the guard resolves it from the AST.
#:   path@`text`       - pinned TEXT (any file): the guard checks the file
#:                        still contains it, whitespace-normalised.
#:   `path:N[-M]`       - a bare LINE: REJECTED. It is unverifiable by
#:                        construction; the line it meant is gone after the
#:                        next merge and nothing could tell.
#: A bare `path` with none of these claims only that the file exists.
_CITATION_RE = re.compile(
    r"\b((?:docs|src|config|tests|scripts)/[\w./-]+\.(?:md|py|yaml|yml|json|toml))"
    r"(?:::(?P<sym>[A-Za-z_][\w.]*)|@`(?P<snip>[^`]+)`|:(?P<line>\d+)(?:-\d+)?)?"
)


def _normalise_text(text: str) -> str:
    """Collapse whitespace so reflowing a line is not a false rejection."""
    return " ".join(text.split())


def _python_symbols(source: str) -> set[str]:
    """Qualified names defined in a module: defs, classes, assigned names."""
    found: set[str] = set()

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.add(prefix + node.name)
                walk(node.body, prefix + node.name + ".")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found.add(prefix + target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found.add(prefix + node.target.id)

    walk(ast.parse(source).body, "")
    return found


def _string_fields(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _string_fields(v)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _string_fields(v)]
    return []


def broken_citations(
    ledger: dict[str, dict[str, Any]], root: Path
) -> list[tuple[str, str, str]]:
    """Repo citations in the ledger that are false or unverifiable:
    `(site_id, why, cite)`.

    Every string field of a row is read. A symbol citation must resolve in
    the file's AST; a text citation must still be in the file; a bare line
    number is rejected outright (board item 225: the old check only proved
    the line existed, so citations rotted while reading as verified).
    """
    out: list[tuple[str, str, str]] = []
    texts: dict[str, str | None] = {}
    symbols: dict[str, set[str]] = {}
    for site_id, entry in ledger.items():
        text = " ".join(_string_fields(entry))
        for match in _CITATION_RE.finditer(text):
            rel = match.group(1)
            if rel not in texts:
                target = root / rel
                texts[rel] = (
                    target.read_text(encoding="utf-8") if target.is_file() else None
                )
            body = texts[rel]
            if body is None:
                out.append((site_id, "no such file", match.group(0)))
            elif match.group("line"):
                out.append(
                    (
                        site_id,
                        "bare line-number citation is unverifiable; cite a "
                        "symbol (path::Qual.name) or pin text (path@`text`)",
                        match.group(0),
                    )
                )
            elif match.group("sym"):
                if not rel.endswith(".py"):
                    out.append((site_id, "symbol citation needs a .py file", match.group(0)))
                    continue
                if rel not in symbols:
                    symbols[rel] = _python_symbols(body)
                if match.group("sym").rstrip(".") not in symbols[rel]:
                    out.append((site_id, "symbol not defined in that file", match.group(0)))
            elif match.group("snip"):
                if _normalise_text(match.group("snip")) not in _normalise_text(body):
                    out.append((site_id, "cited text not found in that file", match.group(0)))
    return out
