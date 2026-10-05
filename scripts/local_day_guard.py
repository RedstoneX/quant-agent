"""Local-day-as-exchange-day guard with no stored offender list.

An exchange day must be compared to an exchange day, never to the runner's.
This repo has hit that bug at least four times; each time a window of tests
reds on the SAME commit for a quarter-hour around ET midnight and a full test
round is lost.

The scanner below is unchanged in substance from the one that lived in
``tests/test_no_local_day_as_exchange_day.py``. What changed is where "before"
comes from. That test carried a hardcoded ``_BASELINE`` of current offenders —
a cached measurement committed to the repo, the same collision engine as the
JSON baselines, just written in Python. Every change that touched a listed file
had to edit it, so unrelated changes jammed each other.

So this stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md). At check time it
names every offending site in the working tree — file, kind, enclosing scope
and the call's own source text — names them again on ``origin/main``, and fails
on any IDENTITY the tree holds more copies of than the trunk. Never a total:
removing one offender and adding a different one must still fail. If
``origin/main`` cannot be read it REFUSES; it never passes by default.

WHAT IS FORBIDDEN
-----------------
In every tracked ``.py`` file (root ``main.py``, ``ops/``, ``scripts/`` included):

  local_today        ``date.today()`` / ``datetime.date.today()`` -- the
                     runner's local calendar day, never an exchange day.
  naive_now          ``datetime.now()`` with no timezone -- the runner's local
                     wall clock; its ``.date()`` is the local day.
  utc_day            ``.date()`` taken directly off ``datetime.now(<utc>)`` or
                     ``utcnow()`` -- a UTC calendar day used as a day.
In ``tests/`` only:
  import_time_stamp  a module-level assignment whose value reads the clock
                     (``et_today()``, ``et_now()``, ``todays_session_stamp()``,
                     ``datetime.now(...)``) -- a stamp frozen at collection and
                     compared against a run-time read.

It is deliberately an AST scan, not a grep: the indirect spellings are the ones
that slipped before. To extend it, add a shape to ``_classify_call`` with a
one-word kind; there is no list to update afterwards, because the trunk is the
list.

Run it directly: ``python -m scripts.local_day_guard``.
"""
from __future__ import annotations

import ast
import sys

from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    added_sites,
    enclosing_scopes,
    site_identity,
    trunk_blobs,
    working_paths,
)

#: One offending site: (path, kind, enclosing scope, source text of the site).
Site = tuple[str, str, str, str]

# No directory list: every tracked ``.py`` is scanned, the repository root
# included -- ``main.py`` is the live entry point and a list that named
# ``src``/``tests`` never saw it. Only ``import_time_stamp`` is tests-only.
SCAN_PATTERN = "*.py"

_CLOCK_READERS = {"et_today", "et_now", "todays_session_stamp",
                  "todays_session_bar_stamp", "todays_session_snapshot_stamps"}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_now_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _name(node.func) in {"now", "utcnow"}


def _classify_call(call: ast.Call) -> str | None:
    fn = call.func
    # date.today() / datetime.date.today() / datetime.today()
    if isinstance(fn, ast.Attribute) and fn.attr == "today" and _name(fn.value) in {"date", "datetime"}:
        return "local_today"
    # datetime.now() with no tz
    if isinstance(fn, ast.Attribute) and fn.attr == "now" and _name(fn.value) == "datetime" \
            and not call.args and not call.keywords:
        return "naive_now"
    # datetime.fromtimestamp(ts) / date.fromtimestamp(ts) with no tz: the runner's local time
    if isinstance(fn, ast.Attribute) and fn.attr == "fromtimestamp" \
            and _name(fn.value) in {"date", "datetime"} and len(call.args) == 1 and not call.keywords:
        return "naive_now"
    # <now(...)|utcnow()>.date()
    if isinstance(fn, ast.Attribute) and fn.attr == "date" and _is_now_call(fn.value):
        inner = fn.value
        if _name(inner.func) == "utcnow" or not inner.args and not inner.keywords:
            return "utc_day"
        tz = _name(inner.args[0]) if inner.args else _name(inner.keywords[0].value)
        if tz.lower() in {"utc", "timezone"} or "utc" in tz.lower():
            return "utc_day"
    return None


def _reads_clock(expr: ast.AST) -> bool:
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Call) and (_name(sub.func) in _CLOCK_READERS or _is_now_call(sub)):
            return True
    return False


def scan_text(path: str, text: str) -> list[tuple[str, int]]:
    """Every (kind, line) offence in one module's source."""
    return [(kind, line) for kind, line, _scope, _src in scan_sites(path, text)]


def scan_sites(path: str, text: str) -> list[tuple[str, int, str, str]]:
    """Every (kind, line, enclosing scope, source text) offence in one module."""
    tree = ast.parse(text)
    scopes = enclosing_scopes(tree)
    found: list[tuple[str, int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kind = _classify_call(node)
            if kind:
                found.append((kind, node.lineno, *site_identity(node, scopes)))
    if path.split("/", 1)[0] == "tests":
        for node in tree.body:  # module level only
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None \
                    and _reads_clock(node.value):
                found.append(("import_time_stamp", node.lineno, *site_identity(node, scopes)))
    return found


def scanned_paths() -> list[str]:
    """Every tracked ``.py`` file, wherever git holds it; never a written-down directory list."""
    return sorted(set(working_paths(SCAN_PATTERN)))


def working_offences() -> dict[Site, list[int]]:
    """Every offending site in the working tree, with the lines it occurs on."""
    out: dict[Site, list[int]] = {}
    for path in scanned_paths():
        try:
            text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
            found = scan_sites(path, text)
        except SyntaxError:
            continue  # unparsable source is a different failure, caught elsewhere
        for kind, line, scope, src in found:
            out.setdefault((path, kind, scope, src), []).append(line)
    return out


def trunk_offences(paths: list[str]) -> dict[Site, int]:
    """How many copies of each offending site ``origin/main`` already holds.

    A path absent from the trunk simply has none — that is how a new file is
    recognised. A trunk blob that will not parse is unmeasurable, so the guard
    refuses rather than treat it as clean.
    """
    counts: dict[Site, int] = {}
    for path, text in trunk_blobs(paths).items():
        try:
            found = scan_sites(path, text)
        except SyntaxError as exc:
            raise ReferenceUnavailable(
                f"cannot parse {TRUNK}:{path} ({exc}), so this guard cannot measure "
                f"what that file already contained; it refuses rather than pass."
            ) from exc
        for kind, _line, scope, src in found:
            key = (path, kind, scope, src)
            counts[key] = counts.get(key, 0) + 1
    return counts


def violations() -> list[str]:
    """Every offending site this working tree holds that ``origin/main`` does not.

    Identity, not total: a site is (path, kind, enclosing scope, source text),
    so removing one offender never licenses adding a different one.
    """
    now = working_offences()
    before = trunk_offences(scanned_paths())
    bad: list[str] = []
    for (path, kind, scope, src), n, was in added_sites(
        {site: len(lines) for site, lines in now.items()}, before
    ):
        bad.append(
            f"{path} [{kind}] in {scope}: `{src}` x{n} on this branch vs x{was} on "
            f"{TRUNK} (+{n - was}); offending lines {sorted(now[(path, kind, scope, src)])}"
        )
    return bad


def main(argv: list[str] | None = None) -> int:
    try:
        bad = violations()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        print(
            "a local or import-time day is compared where an exchange day is meant; "
            "read the exchange day at the moment of comparison "
            "(src.trading_calendar.et_today / tests.desk_clock.freeze_desk_day) "
            "instead of adding these against %s:\n%s" % (TRUNK, "\n".join(bad)),
            file=sys.stderr,
        )
        return 1
    print(f"local-day guard: this tree adds no new local-day offender against {TRUNK}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
