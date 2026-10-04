"""Ledger citation guard (rule 7, board items 225 and 232), in two layers.

Layer 1, `unresolved_citations`: does the citation point at something real?
Layer 2, `unsubstantiated_citations`: does what it points at justify a number?
`broken_citations` is both, for the callers that want the whole verdict.
"""

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


_IMPORT_RE = re.compile(r"^\s*(import\s+\S|from\s+\S+\s+import\b)")
_OPENERS = {"(": ")", "[": "]", "{": "}"}
_PREFIX_OK = re.compile(r"^[\s#>*|\-\"'`/]*$")
_ASSIGN_PREFIX = re.compile(r"^\s*[\w.\[\]]+\s*(:[^=]+)?=\s*$")


def _unbalanced(text: str) -> bool:
    stack: list[str] = []
    for ch in text:
        if ch in _OPENERS:
            stack.append(_OPENERS[ch])
        elif ch in _OPENERS.values():
            if not stack or stack.pop() != ch:
                return True
    return bool(stack)


def _cannot_substantiate_text(snip: str, body: str) -> str | None:
    """Why a text pin proves nothing, or None. An import line, an unbalanced
    fragment, or text that starts mid-sentence says nothing about a number."""
    if _IMPORT_RE.match(snip):
        return "text pin is an import statement"
    if _unbalanced(snip):
        return "text pin has unbalanced brackets (a mid-sentence fragment)"
    tokens = snip.split()
    if not tokens:
        return "text pin is empty"
    hit = re.search(r"\s+".join(re.escape(t) for t in tokens), body)
    if hit is None:
        return None
    line_start = body.rfind("\n", 0, hit.start()) + 1
    before = body[line_start : hit.start()]
    if _PREFIX_OK.match(before) or re.search(r"[.:;!?]\s+$", before):
        return None
    if _ASSIGN_PREFIX.match(before):
        return None
    return "text pin begins mid-sentence, not at a statement or sentence boundary"


def _string_fields(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _string_fields(v)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _string_fields(v)]
    return []


def unresolved_citations(
    ledger: dict[str, dict[str, Any]], root: Path
) -> list[tuple[str, str, str]]:
    """LAYER 1 (resolves). Repo citations in the ledger that are false or
    unverifiable:
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


def unsubstantiated_citations(
    ledger: dict[str, dict[str, Any]], root: Path
) -> list[tuple[str, str, str]]:
    """LAYER 2 (substantiates), item 232: `(site_id, why, cite)` for citations
    that may resolve yet cannot justify a number - an import line, a module
    dunder such as `__all__`, or a text pin that starts mid-sentence. Missing
    files and symbols are layer 1's business and are skipped here."""
    out: list[tuple[str, str, str]] = []
    bodies: dict[str, str | None] = {}
    for site_id, entry in ledger.items():
        text = " ".join(_string_fields(entry))
        for match in _CITATION_RE.finditer(text):
            rel = match.group(1)
            if rel not in bodies:
                target = root / rel
                bodies[rel] = (
                    target.read_text(encoding="utf-8") if target.is_file() else None
                )
            body = bodies[rel]
            if body is None:
                continue
            sym = match.group("sym")
            if sym:
                sym = sym.rstrip(".")
                if "." not in sym and sym.startswith("__") and sym.endswith("__"):
                    out.append(
                        (
                            site_id,
                            "module-level dunder (e.g. __all__) substantiates nothing",
                            match.group(0),
                        )
                    )
            elif match.group("snip"):
                if _normalise_text(match.group("snip")) not in _normalise_text(body):
                    continue
                why = _cannot_substantiate_text(match.group("snip"), body)
                if why:
                    out.append((site_id, why, match.group(0)))
    return out


def broken_citations(
    ledger: dict[str, dict[str, Any]], root: Path
) -> list[tuple[str, str, str]]:
    """Both layers: everything wrong with a citation, resolving or substantiating."""
    return unresolved_citations(ledger, root) + unsubstantiated_citations(ledger, root)
