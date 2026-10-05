"""Ledger prose must NAME things that still exist (RESOLUTION, not substantiation).

`config/number_ledger.yaml` explains each money number in prose. A refactor can
leave the code correct and the tests green while a note still describes a
symbol that is gone. This guard decides only what is mechanically decidable:
every code symbol or repo path a note NAMES must still exist. It does not judge
whether the prose is true, and it is separate from `src/ledger_citations.py`
(which resolves `path::symbol` / `path@text` citations) and from any check that
a citation SUBSTANTIATES its number.

A backticked token is a symbol claim when it is identifier-shaped AND carries
code shape: an underscore, a dot-qualified name, CamelCase, or a trailing `()`.
Plain words in backticks (`funding`, `count`) are prose, not claims. Each
segment of a claimed name must appear as a word in tracked source (src, scripts,
config minus the ledger itself, frontend/src); when the leading segments name a
module (`src.rotation.x`, `src/rotation.py::x`) the rest must appear in THAT file.

Stores nothing. It reads the working tree and REFUSES (exit 2) when
`origin/main` cannot be read, like every guard on the shared reference module.
No tolerance, no allowed count: every violation is listed, any is a failure.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import guard_reference  # noqa: E402
from scripts.ledger_locator import is_ledger_path, working_ledger  # noqa: E402
from src.number_sources import load_ledger  # noqa: E402

ROOT = guard_reference.ROOT
_SKIP_FIELDS = {"id", "value", "base_value", "status"}
_BACKTICK = re.compile(r"`([^`\n]+)`")
_IDENT = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\(\))?$")
_PATH = re.compile(r"^(?:docs|src|config|tests|scripts)/[\w./-]+\.(?:md|py|yaml|yml|json|toml)$")
_ANCHOR = re.compile(
    r"\b((?:docs|src|config|tests|scripts)/[\w./-]+\.(?:md|py|yaml|yml|json|toml))::([A-Za-z_][\w.]*)"
)
_WORD = re.compile(r"[A-Za-z_]\w*")
_SOURCE_SUFFIXES = (".py", ".sql", ".yaml", ".yml", ".json", ".toml", ".ts", ".tsx")
_SOURCE_ROOTS = ("src/", "scripts/", "config/", "frontend/src/")


def _code_shaped(name: str) -> bool:
    if name.endswith("()"):
        return True
    return "_" in name or "." in name or bool(re.search(r"[a-z][A-Z]|^[A-Z][a-z]+[A-Z]", name))


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for k, v in value.items() if k not in _SKIP_FIELDS for t in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _strings(v)]
    return []


def _source_files(root: Path) -> list[str]:
    paths = guard_reference.working_paths("*")
    ledger = working_ledger()  # found by shape; refuses if not exactly one
    return [
        p for p in paths
        if p.endswith(_SOURCE_SUFFIXES)
        and p.startswith(_SOURCE_ROOTS)
        and not is_ledger_path(p, ledger)
    ]


def violations(ledger: dict | None = None, root: Path | None = None) -> list[str]:
    """Every stale name in the ledger's prose, as `row | name | why`."""
    guard_reference.require_trunk()  # refuse, never pass, without the reference
    root = root or ROOT
    ledger = load_ledger() if ledger is None else ledger
    words: dict[str, set[str]] = {}
    for rel in _source_files(root):
        words[rel] = set(_WORD.findall((root / rel).read_text(encoding="utf-8", errors="replace")))
    everywhere = set().union(*words.values()) if words else set()
    # the ledger's own field names are legitimately named in its prose
    everywhere |= {key for entry in ledger.values() for key in entry}
    out: list[str] = []

    def check_in_file(row: str, name: str, rel: str, parts: list[str]) -> None:
        if not (root / rel).is_file():
            out.append(f"{row} | {name} | path {rel} does not exist")
        elif rel in words:
            for part in parts:
                if part not in words[rel]:
                    out.append(f"{row} | {name} | `{part}` not found in {rel}")
                    break

    for row, entry in ledger.items():
        for field_text in _strings(entry):
            for rel, sym in _ANCHOR.findall(field_text):
                check_in_file(row, f"{rel}::{sym}", rel, sym.rstrip(".").split("."))
            for token in _BACKTICK.findall(field_text):
                token = token.strip()
                if _PATH.match(token):
                    if not (root / token).is_file():
                        out.append(f"{row} | {token} | path does not exist")
                    continue
                if not _IDENT.match(token) or not _code_shaped(token):
                    continue
                parts = token.removesuffix("()").split(".")
                # leading segments that spell a module: src.a.b.Name -> src/a/b.py
                for cut in range(len(parts), 0, -1):
                    rel = "/".join(parts[:cut]) + ".py"
                    pkg = "/".join(parts[:cut]) + "/__init__.py"
                    hit = rel if (root / rel).is_file() else pkg if (root / pkg).is_file() else None
                    if hit:
                        if parts[cut:]:
                            check_in_file(row, token, hit, parts[cut:])
                        break
                else:
                    for part in parts:
                        if part not in everywhere:
                            out.append(f"{row} | {token} | `{part}` is defined nowhere in source")
                            break
    return sorted(set(out))


def main() -> int:
    try:
        found = violations()
    except guard_reference.ReferenceUnavailable as exc:
        print(f"REFUSED: {exc}")
        return 2
    for line in found:
        print(line)
    if found:
        print(f"{len(found)} ledger note(s) name something that no longer exists.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
