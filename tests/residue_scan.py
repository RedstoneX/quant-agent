"""The file set the deleted-mechanism residue scan reads."""
from __future__ import annotations

from pathlib import Path


def residue_files(root: Path, search_roots: tuple[Path, ...]) -> list[Path]:
    out: list[Path] = []
    for r in search_roots:
        if not r.exists():
            continue
        out.extend(p for p in r.rglob("*.py") if "__pycache__" not in p.parts)
        out.extend(r.rglob("*.md"))
    out.extend(root.glob("*.py"))  # the root itself: main.py and friends
    out.extend(root.glob("*.md"))
    return sorted(set(out))
