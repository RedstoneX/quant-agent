"""Ledger citations: RESOLVES is not SUBSTANTIATES (board item 232).

`src/ledger_citations.py` proves a pin points at something real. That turns
"unverified" into "verified" for a pin that lands on an import, an `__all__`
entry, a comment, or on code that never mentions the number it is cited for.
This guard classifies every pin the ledger makes, by AST, into:

  unresolved  the file/symbol/text is not there (layer 1's business; counted)
  dead        lands on an import, `__all__`, a module dunder, a comment, a
              blank, or text that starts mid-sentence: proves nothing, ever
  no_mention  lands on real code/prose that mentions NEITHER the row's own
              name (last component of its id) NOR its value
  mentions    lands on code/prose that mentions the row's name or value

Only `mentions` is a candidate for substantiating, and it is NOT a verdict:
mentioning a value is not justifying it. Telling those apart needs a reader.

A pin made by a row's `source` field is held to an ABSOLUTE rule with no trunk
baseline: `dead` or `no_mention` there fails outright, because `source` is the
row's own claim of where its number is settled and a pin that never carries the
number cannot be that. `note` pins are ratcheted:

The limit is FIXED in the repo, never re-derived from the trunk: every bad `note`
pin that exists today is pinned by identity (row, citation, verdict) in
`config/check_allowlists/ledger_substantiation.txt`, and every uncited row in
`ledger_substantiation_uncited.txt`. A bad pin or uncited row not on its list
fails; a listed entry that no longer occurs is stale and fails (shrink-only; a
new entry needs a Guard-rule-change line).

Run: ``python -m scripts.ledger_substantiation_guard``.
"""
from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, NamedTuple

import yaml

from scripts.ledger_locator import working_ledger
from scripts.guard_reference import ROOT, ReferenceUnavailable
from src.ledger_citations import (
    _CITATION_RE,
    _cannot_substantiate_text,
    _normalise_text,
    _string_fields,
)

Reader = Callable[[str], "str | None"]


class Pin(NamedTuple):
    site_id: str
    cite: str
    verdict: str  # unresolved | dead | no_mention | mentions
    why: str


def _leaf(site_id: str) -> str:
    """The row's own name: `m.C.f(param)` -> param, `m.C.f:factor[2]` -> f."""
    paren = re.search(r"\(([\w]+)\)$", site_id)
    if paren:
        return paren.group(1)
    return re.sub(r"\[.*$", "", site_id.split(":")[0]).rsplit(".", 1)[-1]


#: A leading `-` is a SIGN only where it cannot be a subtraction operator:
#: not directly after an identifier, a digit, or a closing bracket. So the
#: `-0.3` in `(-0.3, -0.1)` is read as negative, while the `-1` in `x-1` and
#: in `f(a)-1` is not. The proxy errs CLOSED on a space-padded subtraction
#: (`x - 1` still yields 1, never -1), which can only ever cost a mention,
#: never invent one.
def _numbers(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"(?<![\w.])(?:\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?|\.\d+)", text):
        try:
            value = float(m.group(0).replace("_", ""))
        except ValueError:
            continue
        out.append(value)
        before = text[: m.start()]
        if before.rstrip() is before and before.endswith("-"):
            stem = before[:-1]
            if not stem or not (stem[-1].isalnum() or stem[-1] in "_.)]"):
                out.append(-value)
    return out


def mentions(window: str, site_id: str, value: Any) -> bool:
    if re.search(rf"\b{re.escape(_leaf(site_id))}\b", window):
        return True
    key = re.search(r"\[['\"]([^'\"]+)['\"]\]$", site_id)
    if key and re.search(rf"['\"]{re.escape(key.group(1))}['\"]", window):
        return True
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return any(abs(n - float(value)) < 1e-12 for n in _numbers(window))
    if isinstance(value, str) and len(value) >= 3:
        return value in window
    return False


def _find_symbol(tree: ast.Module, qual: str) -> ast.AST | None:
    node: Any = tree
    for part in qual.split("."):
        nxt = None
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == part:
                nxt = child
            elif isinstance(child, ast.Assign) and any(isinstance(t, ast.Name) and t.id == part for t in child.targets):
                nxt = child
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and child.target.id == part:
                nxt = child
        if nxt is None:
            return None
        node = nxt
    return node


def _statement_window(tree: ast.Module, lines: list[str], lineno: int) -> tuple[str, str | None]:
    """(text of the innermost statement holding `lineno`, dead-landing reason)."""
    best: ast.stmt | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and node.lineno <= lineno <= (node.end_lineno or node.lineno):
            if best is None or (node.end_lineno - node.lineno) <= (best.end_lineno - best.lineno):
                best = node
    if best is None:
        return lines[lineno - 1], None
    if isinstance(best, (ast.Import, ast.ImportFrom)):
        return "", "pin lands on an import statement"
    if isinstance(best, (ast.Assign, ast.AnnAssign)):
        names = [t.id for t in (best.targets if isinstance(best, ast.Assign) else [best.target]) if isinstance(t, ast.Name)]
        if "__all__" in names:
            return "", "pin lands on an `__all__` entry"
    if isinstance(best, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        end = best.body[0].lineno - 1 if best.body else best.lineno
        return "\n".join(lines[best.lineno - 1 : max(end, best.lineno)]), None
    return "\n".join(lines[best.lineno - 1 : best.end_lineno]), None


def classify(ledger: dict[str, dict[str, Any]], read: Reader) -> list[Pin]:
    """One `Pin` per symbol or text citation. Bare paths and URLs are not pins."""
    out: list[Pin] = []
    parsed: dict[str, ast.Module | None] = {}
    for site_id, entry in ledger.items():
        value = entry.get("value")
        for m in _CITATION_RE.finditer(" ".join(_string_fields(entry))):
            sym, snip, rel = m.group("sym"), m.group("snip"), m.group(1)
            if not (sym or snip):
                continue
            cite = m.group(0)
            body = read(rel)
            if body is None:
                out.append(Pin(site_id, cite, "unresolved", "no such file"))
                continue
            tree = None
            if rel.endswith(".py"):
                if rel not in parsed:
                    try:
                        parsed[rel] = ast.parse(body)
                    except SyntaxError:
                        parsed[rel] = None
                tree = parsed[rel]
            if sym:
                sym = sym.rstrip(".")
                node = _find_symbol(tree, sym) if tree is not None else None
                if node is None:
                    out.append(Pin(site_id, cite, "unresolved", "symbol not defined"))
                elif "." not in sym and sym.startswith("__") and sym.endswith("__"):
                    out.append(Pin(site_id, cite, "dead", "module dunder"))
                else:
                    seg = ast.get_source_segment(body, node) or ""
                    ok = mentions(seg, site_id, value)
                    out.append(Pin(site_id, cite, "mentions" if ok else "no_mention",
                                   "definition names the row or its value" if ok else "definition names neither"))
                continue
            toks = snip.split()
            if not toks:
                out.append(Pin(site_id, cite, "dead", "blank pin"))
                continue
            hit = re.search(r"\s+".join(re.escape(t) for t in toks), body)
            if hit is None:
                out.append(Pin(site_id, cite, "unresolved", "text not found"))
                continue
            why = _cannot_substantiate_text(snip, body)
            if why:
                out.append(Pin(site_id, cite, "dead", why))
                continue
            lineno = body.count("\n", 0, hit.start()) + 1
            lines = body.split("\n")
            span = lines[lineno - 1 : lineno + snip.count("\n")]
            if all(ln.strip().startswith("#") for ln in span) and rel.endswith((".py", ".yaml", ".yml", ".toml")):
                out.append(Pin(site_id, cite, "dead", "pin lands on a comment"))
                continue
            window, dead = (_statement_window(tree, lines, lineno) if tree is not None else (lines[lineno - 1], None))
            if dead:
                out.append(Pin(site_id, cite, "dead", dead))
                continue
            ok = mentions(window + " " + snip, site_id, value)
            out.append(Pin(site_id, cite, "mentions" if ok else "no_mention",
                           "text names the row or its value" if ok else "text names neither"))
    return out


def _entries(text: str) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(text) or {}
    return {e["id"]: e for e in (raw.get("numbers") or []) if e.get("id")}


def _cited_paths(ledger: dict[str, dict[str, Any]]) -> list[str]:
    return sorted({m.group(1) for e in ledger.values() for m in _CITATION_RE.finditer(" ".join(_string_fields(e)))})


def _source_only(ledger: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The rows' `source` fields alone: the ledger's own claim of where the number is settled."""
    return {k: {"id": k, "value": e.get("value"), "source": e["source"]} for k, e in ledger.items() if e.get("source")}


def _read_under(root: Path, rel: str) -> str | None:
    p = root / rel
    return p.read_text(encoding="utf-8") if p.is_file() else None


def source_pins(root: Path = ROOT) -> list[Pin]:
    """Pins made by `source` fields only, over the working tree."""
    ledger = _entries((root / working_ledger(root)).read_text(encoding="utf-8"))
    return classify(_source_only(ledger), lambda rel: _read_under(root, rel))


def working_pins(root: Path = ROOT) -> list[Pin]:
    ledger = _entries((root / working_ledger(root)).read_text(encoding="utf-8"))

    def read(rel: str) -> str | None:
        p = root / rel
        return p.read_text(encoding="utf-8") if p.is_file() else None

    return classify(ledger, read)


ALLOWLIST_DIR = ROOT / "config" / "check_allowlists"
PINS_ALLOWLIST = ALLOWLIST_DIR / "ledger_substantiation.txt"
UNCITED_ALLOWLIST = ALLOWLIST_DIR / "ledger_substantiation_uncited.txt"


def read_allowlist(path: Path) -> set[str]:
    """Entries of a committed allow-list; `#` lines and blanks are comments."""
    return {ln.strip("\n") for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")}


def pin_key(p: Pin) -> str:
    """Identity of a bad pin: row, citation, verdict (tab-joined; no line numbers)."""
    return "\t".join((p.site_id, p.cite, p.verdict))


BAD = ("dead", "no_mention")


def tally(pins: list[Pin]) -> Counter:
    return Counter(p.verdict for p in pins)


def source_violations(pins: list[Pin] | None = None) -> list[str]:
    """ABSOLUTE, no trunk baseline: a `source` pin that is dead or never mentions the row is false.

    A `source` field is the row's own statement of where its number is settled, so a
    pin there that lands on a comment, an import or a symbol naming neither the row nor
    its value cannot be what the field says it is; `note` pins stay ratcheted below.
    """
    pins = source_pins() if pins is None else pins
    return [f"{p.site_id}: source citation {p.cite} is {p.verdict} ({p.why}); a source must carry the number"
            for p in pins if p.verdict in BAD]


def violations(now: list[Pin] | None = None, allowed: set[str] | None = None,
               sources: list[Pin] | None = None) -> list[str]:
    """Bad pins not on the committed allow-list, stale list entries, plus every bad `source` pin."""
    now = working_pins() if now is None else now
    allowed = read_allowlist(PINS_ALLOWLIST) if allowed is None else allowed
    absolute = source_violations(sources)
    bad = {pin_key(p): p for p in now if p.verdict in BAD}
    new = [f"{p.site_id}: {p.verdict} citation {p.cite} ({p.why}); it resolves but cannot substantiate"
           for k, p in sorted(bad.items()) if k not in allowed]
    stale = [f"{k.replace(chr(9), ' | ')}: on ledger_substantiation.txt but no longer a bad pin; delete the entry"
             for k in sorted(allowed - set(bad))]
    return absolute + new + stale


def _has_citation(row: dict[str, Any]) -> bool:
    return bool(_CITATION_RE.search(" ".join(_string_fields(row))))


def uncited_ids(ledger: dict[str, dict[str, Any]]) -> set[str]:
    """Rows that make no citation in any field: invisible to every pin check above."""
    return {k for k, e in ledger.items() if not _has_citation(e)}


def uncited_violations(now: dict[str, dict[str, Any]] | None = None,
                       allowed: set[str] | None = None) -> list[str]:
    """Uncited rows must be on the committed list; a listed row that gained a citation is stale.

    Catches both a citation deleted from a cited row and a new row added bare; fixing
    a listed row only ever shrinks the list.
    """
    if now is None:
        now = _entries((ROOT / working_ledger(ROOT)).read_text(encoding="utf-8"))
    allowed = read_allowlist(UNCITED_ALLOWLIST) if allowed is None else allowed
    bare = uncited_ids(now)
    return ([f"{k}: carries no citation and is not on ledger_substantiation_uncited.txt; "
             f"deleting or omitting a citation is not substantiation" for k in sorted(bare - allowed)]
            + [f"{k}: on ledger_substantiation_uncited.txt but now cites something or is gone; delete the entry"
               for k in sorted(allowed - bare)])


def main() -> int:
    try:
        now = working_pins()
        bad = violations(now) + uncited_violations()
    except ReferenceUnavailable as exc:
        print(f"REFUSED: {exc}")
        return 2
    t = tally(now)
    ts = tally(source_pins())
    print(f"source-field pins: dead+no_mention={ts['dead'] + ts['no_mention']} of {sum(ts.values())} (absolute)")
    print(f"pins={len(now)} " + " ".join(f"{k}={t[k]}" for k in ("unresolved", "dead", "no_mention", "mentions"))
          + f" | allow-listed: {len(read_allowlist(PINS_ALLOWLIST))}")
    led = _entries((ROOT / working_ledger(ROOT)).read_text(encoding="utf-8"))
    print(f"uncited rows: {len(uncited_ids(led))} of {len(led)} (down-only ratchet)")
    for line in bad:
        print("  " + line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
