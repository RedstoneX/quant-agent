"""A settlement recording's value may not be handed over by a silent default.

WHY THIS GUARD EXISTS -- the failure CLASS, not one recording
-------------------------------------------------------------
Four settlement recordings in a row were built to settle a money-governing
number, were closed against on the board, and then recorded NOTHING. The
existing check (`tests/test_settlement_recording_writes.py`, via
`src.storage_write_index.written_columns`) reads the tree's own AST and
asks whether executable code writes the field. In every one of those four
cases it answered YES and was right: the column existed, the INSERT named it,
the migration had run. The defect was always ONE LAYER UP, in the expression
that hands the value to that INSERT:

  * `trades.entry_atr` -- `getattr(decision, "atr_14", None)` read ATR(14)
    off an object that has no such field, so the default answered on every
    single trade and the column was NULL for the whole life of the feature
    [measured: 0 of 80 rows, production, 2026-10-01].
  * `trades.stop_basis` -- `getattr(decision, "stop_rule", None)` at the same
    call site, still NULL on 84 of 84 rows while four other fields pinned by
    their own keywords at that exact call fill normally [measured 2026-10-04].

The shape is a three-argument `getattr`. It CANNOT FAIL. A typo, a renamed
field, a value that moved to a sibling object -- each turns into the default,
the writer still "passes the field", every test that checks the call still
passes, and the recording accrues nothing while the item reads as handled.
`src/recording_accessors.py` holds the loud alternative: absent object gives
None, absent attribute RAISES.

WHAT THIS GUARD CANNOT DECIDE, and what would
---------------------------------------------
The fourth instance (the noise band, PR #1177) failed a different way: the
write fired only on the branch where the band BLOCKED an exit, so the sample
was censored at exactly the threshold under study. Whether a condition
censors a sample is a question about the MEANING of the branch, not its
shape, and no AST can answer it -- an `if` around a write is ordinary and
correct in most code. What settles that one is a production measurement that
the accrued distribution holds observations on BOTH sides of the threshold,
which is a reading of the database, not of the source.

FIXED ALLOW-LIST
----------------
A three-argument ``getattr`` (a silent default) or a literal ``None`` supplied
for a built-route field is an ABSOLUTE ban. The only exceptions are the
existing sites named, by identity (path, enclosing scope, field, problem), in
``config/check_allowlists/code_settlement_fill.txt``: a committed, shrink-only
list. The guard fails on a site not in the list and on a listed entry that no
longer occurs. It reads no git ref, so unrelated merges cannot redden it.

Run it directly:
``PYTHONPATH=. .venv/bin/python -m scripts.settlement_fill_guard``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable

import yaml

from scripts.check_allowlist import ALLOWLIST_DIR, compare, report
from scripts.guard_reference import ROOT, _git, ReferenceUnavailable, enclosing_scopes
from scripts.ledger_locator import working_ledger

#: The storage layer is where the INSERT lives; the defect is in its CALLERS,
#: and `src/storage/db.py` legitimately uses defaulted reads on payload dicts.
EXCLUDED_PREFIXES = ("src/storage/",)

Identity = tuple[str, str, str, str]


def built_route_fields(ledger_text: str) -> set[str]:
    """Bare field names named by every `settles_by` route in state `built`."""
    fields: set[str] = set()
    for entry in (yaml.safe_load(ledger_text) or {}).get("numbers", []) or []:
        route = entry.get("settles_by")
        if not isinstance(route, dict) or route.get("state") != "built":
            continue
        for target in route.get("writes") or []:
            fields.add(str(target).rsplit(".", 1)[-1])
    return fields


def _supplier_problem(value: ast.AST) -> str | None:
    """Why this argument expression cannot be trusted to carry a value."""
    if isinstance(value, ast.Call):
        name = getattr(value.func, "id", None) or getattr(value.func, "attr", None)
        if name == "getattr" and len(value.args) >= 3:
            return "silent_default_getattr"
    if isinstance(value, ast.Constant) and value.value is None:
        return "hardcoded_none"
    return None


def offending_sites(source: str, path: str, fields: set[str]) -> list[Identity]:
    """Every keyword argument supplying a built-route field unsafely."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    scopes = enclosing_scopes(tree)
    out: list[Identity] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg is None or kw.arg not in fields:
                continue
            problem = _supplier_problem(kw.value)
            if problem is None:
                continue
            scope = scopes.get(id(node), "<module>")
            out.append((path, scope, kw.arg, problem))
    return out


def scan(blobs: dict[str, str], fields: set[str]) -> list[Identity]:
    found: list[Identity] = []
    for path, text in sorted(blobs.items()):
        if path.startswith(EXCLUDED_PREFIXES):
            continue
        found.extend(offending_sites(text, path, fields))
    return found


def _working_blobs(paths: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in paths:
        full = Path(ROOT) / path
        if full.exists():
            out[path] = full.read_text(encoding="utf-8")
    return out


def describe(identity: Identity) -> str:
    path, scope, field, problem = identity
    if problem == "silent_default_getattr":
        why = (
            "a three-argument getattr cannot fail, so a typo or a rename "
            "records NULL forever while the call still looks right; read it "
            "with src.recording_accessors.pinned_evidence instead"
        )
    else:
        why = "the argument is the literal None, so the recording can never fill"
    return f"{path}:{scope}: {field} <- {problem} -- {why}"


def _is_subject(rel: str) -> bool:
    """Production code only: every tracked ``.py`` outside ``tests/``, the root included."""
    return rel.endswith(".py") and rel.split("/", 1)[0] != "tests"


def _working_subject_paths() -> list[str]:
    """Tracked and not-yet-tracked ``.py`` files in the working tree, no directory list."""
    tracked = _git("ls-files", "--", "*.py")
    untracked = _git("ls-files", "--others", "--exclude-standard", "--", "*.py")
    if tracked.returncode or not tracked.stdout.strip():
        raise ReferenceUnavailable("git ls-files listed no working-tree sources; refusing")
    lines = tracked.stdout.splitlines() + untracked.stdout.splitlines()
    return sorted({p for p in lines if _is_subject(p)})


ALLOWLIST = ALLOWLIST_DIR / "code_settlement_fill.txt"


def check(blobs: dict[str, str], fields: set[str], allowlist: Path = ALLOWLIST) -> tuple[list[str], list[str]]:
    """``(unlisted, stale)``: silent suppliers missing from the fixed list, and listed ones now gone."""
    return compare(scan(blobs, fields), allowlist)


def main() -> int:
    try:
        paths = _working_subject_paths()
        ledger_rel = working_ledger()
    except ReferenceUnavailable as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    fields = built_route_fields((Path(ROOT) / ledger_rel).read_text(encoding="utf-8"))
    if not fields:
        print(
            "REFUSED: no settlement route is in state `built`; moving every "
            "route to `specified` is not a way past this guard",
            file=sys.stderr,
        )
        return 2

    unlisted, stale = check(_working_blobs(paths), fields)
    if not unlisted and not stale:
        print(
            f"settlement-fill guard: silent suppliers for {len(fields)} built-route "
            f"field(s) match the fixed allow-list exactly"
        )
        return 0
    print("SETTLEMENT RECORDING WOULD NOT FILL (a three-argument getattr or None is banned):", file=sys.stderr)
    print(report(unlisted, stale, ALLOWLIST), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
