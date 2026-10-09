"""Find the number ledger by what it IS, never by where it sits.

The ledger is the one tracked YAML file whose top-level key is ``numbers`` and
whose rows each name the code ``site`` that holds the number. Prompt-only rows
(keyed by ``sheet``) and the history files (keyed ``changes``) do not match.
Zero or several such files is a refusal: a guard that cannot find its subject
must not pass.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from scripts.guard_reference import ROOT, ReferenceUnavailable, trunk_blobs, trunk_paths, working_paths

_TOP_NUMBERS = re.compile(r"^numbers:\s*$", re.MULTILINE)
_ROW_SITE = re.compile(r"^  - id: .*\n(?:    .*\n)*?    site:", re.MULTILINE)
_ROW_SHEET = re.compile(r"^    sheet:", re.MULTILINE)


def _is_ledger(text: str) -> bool:
    return bool(_TOP_NUMBERS.search(text) and _ROW_SITE.search(text) and not _ROW_SHEET.search(text))


def locate(paths: list[str], read: Callable[[list[str]], dict[str, str]], where: str) -> str:
    """The ledger's path among ``paths``; refuses unless exactly one matches."""
    found = sorted(p for p, text in read(paths).items() if _is_ledger(text))
    if len(found) != 1:
        raise ReferenceUnavailable(
            f"cannot locate the number ledger in {where}: expected exactly one YAML with a "
            f"top-level 'numbers:' whose rows carry 'site:', found {found or 'none'}; "
            f"it refuses rather than pass."
        )
    return found[0]


def _yaml_paths(paths: list[str]) -> list[str]:
    return [p for p in paths if p.endswith((".yaml", ".yml"))]


def working_ledger(root: Path = ROOT) -> str:
    """Locate in the working tree; a ``root`` other than the repo is scanned on disk."""
    if root == ROOT:
        paths = working_paths("*.yaml") + working_paths("*.yml")
    else:
        paths = [str(p.relative_to(root)) for p in sorted(root.rglob("*.y*ml")) if p.is_file()]

    def read(cands):
        return {p: (root / p).read_text(encoding="utf-8", errors="replace") for p in cands}

    return locate(_yaml_paths(paths), read, "the working tree")


def trunk_ledger() -> str:
    return locate(_yaml_paths(trunk_paths("")), trunk_blobs, "the trunk")


def is_ledger_path(path: str, ledger: str) -> bool:
    """True for the ledger itself and its sibling history files (same stem)."""
    return path.startswith(str(Path(ledger).with_suffix("")))
