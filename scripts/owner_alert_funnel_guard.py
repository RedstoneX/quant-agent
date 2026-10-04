"""Only the funnel may build a messenger: no NEW owner-alert send path.

Every owner-facing message is supposed to leave through one place --
``TelegramNotifier.send`` in ``src/notifier/transport.py`` -- because that is
the only path that writes the durable ``notifier_sends`` row the dashboard
reads. A send made anywhere else succeeds and fails identically from the
owner's side: he sees nothing either way, and the standing ruling is REPORT
THE TRUE STATE, NEVER AN ERROR. A lost alert is the same defect as a false one.

This guard refuses a SEVENTH such path. It is deliberately absolute rather
than a delta against the trunk: the answer to "who may build their own
messenger" is a short, reasoned list that belongs in code, so the guard needs
no reference commit, cannot go stale against a moving trunk, and cannot be
defeated by a cached trunk read.

WHAT IT CHECKS
--------------
Two kinds of "own messenger", both computed from the source tree at check time:

1. A file that builds the Telegram Bot API URL itself (the literal
   ``api.telegram.org/bot``) instead of calling the funnel.
2. A class defined outside ``src/notifier/`` that exposes a ``send`` method --
   a stand-in notifier object handed to code that expects the real one.
3. A NEW direct ``notifier.send(...)`` call outside the notifier package. The
   funnel ``send_owner_alert`` adds what a bare ``send`` does not: the retry on
   ``src.infra_retry_policy``'s backoff, and, when every attempt fails, the one
   counted ``owner_alert_undelivered`` row the dashboard reads. A bare ``send``
   writes only a plain ``failed`` row, so the alert is lost quietly.

   Rule 3 alone is a DELTA against ``origin/main`` rather than an absolute, and
   deliberately so: three such calls exist today and converting them is a
   separate, reviewed change. An allow-list naming them would be a record of
   the current state that goes stale; comparing to the trunk stores nothing and
   still refuses a fourth. When the trunk cannot be read, rule 3 REFUSES.

WHAT IT COVERS, AND WHAT IT DOES NOT
------------------------------------
Covered: tracked ``.py`` and ``.sh`` files under ``src/``, ``ops/`` and
``scripts/`` -- every directory a production send can be written in.

NOT covered, so the gap is visible rather than assumed closed: ``tests/``
(faking the wire is the point of a test), ``frontend/`` (no Python, reads the
dashboard rather than sending), ``config/`` and ``docs/``. A send written in
any of those is not refused by this guard.

NO STORED BOOKKEEPING
---------------------
Nothing is recorded and no baseline file exists. ``EXEMPT_URL_SITES`` and
``EXEMPT_SENDER_CLASSES`` below are policy, not measurement: each entry is a
bypass a human reasoned about, with the reason written next to it. An entry
that stops matching anything is itself a failure (see ``violations``), so a
dead exemption cannot sit here quietly widening the hole.

REFUSES, NEVER PASSES QUIETLY
-----------------------------
If the tree cannot be listed, comes back empty, or a scanned Python file will
not parse, this raises ``TreeUnreadable`` and the caller reports a refusal. A
guard that returns "clean" when it could not look is decoration.

Run it directly: ``python -m scripts.owner_alert_funnel_guard``.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Where a production send could plausibly be written. Tests are excluded on
#: purpose: a test that fakes the wire is the point of a test.
SCAN_DIRS = ("src", "ops", "scripts")

#: The funnel itself, and the package it lives in.
FUNNEL_FILE = "src/notifier/transport.py"
FUNNEL_PACKAGE = "src/notifier/"

TELEGRAM_URL_MARKER = "api.telegram.org/bot"

TRUNK = "origin/main"

#: Attribute-call shapes that mean "send through a notifier object".
DIRECT_SEND_RE = re.compile(r"\b(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\(\))?\.send\s*\(")

#: Files allowed to build the Bot API URL themselves. Keep this at two.
EXEMPT_URL_SITES = {
    FUNNEL_FILE: "the funnel itself -- this is the one place the wire is touched",
    "scripts/run_if_et_window.sh": (
        "DELIBERATE and load-bearing: this fires when python has been SIGKILLed "
        "and so cannot run its own notifier (thirteen straight days of morning "
        "kills produced zero notifications before it existed). Routing it through "
        "the python funnel would delete the only case it exists to cover."
    ),
}

#: ``(path, class name)`` pairs allowed to expose their own ``send``.
EXEMPT_SENDER_CLASSES = {
    ("src/cost_circuit/breaker.py", "_LocalOnlyNotifier"): (
        "DELIBERATE: constructed only in the except branch taken when importing "
        "or building the real notifier raised. It is the fallback for a broken "
        "funnel, so it must not call the funnel; it sends nothing and returns "
        "False, which is honest rather than silent."
    ),
    ("ops/rehearsal/runner.py", "RehearsalNotifier"): (
        "DELIBERATE: a capture double for the offline rehearsal, which must not "
        "reach the wire at all. It satisfies the cost circuit's real "
        "require_telegram_alerts check honestly by capturing and printing every "
        "message instead of switching alerting off."
    ),
}


class TreeUnreadable(RuntimeError):
    """The source tree could not be read, so no measurement was made."""


def scanned_paths() -> list[str]:
    """Tracked ``.py`` and ``.sh`` files in the scanned directories."""
    patterns = [f"{d}/*{ext}" for d in SCAN_DIRS for ext in (".py", ".sh")]
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *patterns],
        cwd=ROOT, capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise TreeUnreadable(
            "`git ls-files` failed, so this guard measured nothing and REFUSES "
            "rather than report a clean tree. git said: "
            + (out.stderr.strip() or "no stderr")
        )
    paths = [p for p in out.stdout.split("\0") if p]
    if not paths:
        raise TreeUnreadable(
            "`git ls-files` listed no source files under "
            f"{', '.join(SCAN_DIRS)}; that cannot be right, so this guard "
            "REFUSES rather than report a clean tree."
        )
    return sorted(paths)


def read(path: str) -> str:
    try:
        return (ROOT / path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TreeUnreadable(f"cannot read tracked file {path}: {exc}") from exc


def url_sites(paths: list[str]) -> set[str]:
    """Files that build the Bot API URL themselves."""
    return {p for p in paths if TELEGRAM_URL_MARKER in read(p)}


def sender_classes(paths: list[str]) -> set[tuple[str, str]]:
    """Classes outside the notifier package that expose a ``send`` method."""
    found: set[tuple[str, str]] = set()
    for path in paths:
        if not path.endswith(".py") or path.startswith(FUNNEL_PACKAGE):
            continue
        text = read(path)
        if "def send(" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            raise TreeUnreadable(
                f"cannot parse tracked file {path}, so this guard measured "
                f"nothing there and REFUSES rather than report a clean tree: {exc}"
            ) from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "send":
                    found.add((path, node.name))
    return found


def _direct_send_lines(text: str) -> list[str]:
    """Source lines that call ``.send(`` on a notifier-shaped object."""
    return sorted(
        line.strip()
        for line in text.splitlines()
        if DIRECT_SEND_RE.search(line) and "send_document" not in line
    )


def direct_send_sites(paths: list[str]) -> dict[str, list[str]]:
    """Per-file direct-send call lines, outside the notifier package."""
    out: dict[str, list[str]] = {}
    for path in paths:
        if not path.endswith(".py") or path.startswith(FUNNEL_PACKAGE):
            continue
        hits = _direct_send_lines(read(path))
        if hits:
            out[path] = hits
    return out


def trunk_direct_send_sites(paths: list[str]) -> dict[str, list[str]]:
    """The same measurement taken on the trunk, read fresh at check time."""
    sha = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{TRUNK}^{{commit}}"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if sha.returncode != 0 or not sha.stdout.strip():
        raise TreeUnreadable(
            f"cannot read {TRUNK}: rule 3 compares the working tree against the "
            "trunk and stores nothing, so without it this guard REFUSES rather "
            "than pass. Fix: `git fetch origin main`; in CI use fetch-depth: 0."
        )
    out: dict[str, list[str]] = {}
    for path in paths:
        if not path.endswith(".py") or path.startswith(FUNNEL_PACKAGE):
            continue
        blob = subprocess.run(
            ["git", "show", f"{TRUNK}:{path}"], cwd=ROOT, capture_output=True, text=True,
        )
        if blob.returncode != 0:
            continue  # a file new on this branch has no trunk version
        hits = _direct_send_lines(blob.stdout)
        if hits:
            out[path] = hits
    return out


def violations() -> list[str]:
    """Every unexempt own-messenger site, plus any exemption gone dead."""
    paths = scanned_paths()
    bad: list[str] = []

    urls = url_sites(paths)
    for path in sorted(urls - set(EXEMPT_URL_SITES)):
        bad.append(
            f"{path}: builds {TELEGRAM_URL_MARKER} itself. Send through "
            "src.notifier.send_owner_alert (or TelegramNotifier.send) so the "
            "attempt lands in notifier_sends and a failure shows on the dashboard."
        )
    for path in sorted(set(EXEMPT_URL_SITES) - urls - {FUNNEL_FILE}):
        bad.append(
            f"{path}: exempted here but no longer builds the Bot API URL. A dead "
            "exemption silently widens the hole -- delete the entry."
        )

    classes = sender_classes(paths)
    for site in sorted(classes - set(EXEMPT_SENDER_CLASSES)):
        bad.append(
            f"{site[0]}: class {site[1]} defines its own send(). A stand-in "
            "messenger writes no notifier_sends row, so a dropped owner alert "
            "looks exactly like a delivered one. Use the funnel, or add a narrow "
            "exemption here with the reason it cannot."
        )
    now = direct_send_sites(paths)
    before = trunk_direct_send_sites(sorted(now))
    for path in sorted(now):
        added = [ln for ln in now[path] if now[path].count(ln) > before.get(path, []).count(ln)]
        for line in added:
            bad.append(
                f"{path}: NEW direct notifier send `{line}` outside the funnel. "
                "send_owner_alert retries on the desk's backoff policy and records "
                "one counted owner_alert_undelivered row when every attempt fails; "
                "a bare send does neither, so a lost alert reads as a delivered one."
            )

    for site in sorted(set(EXEMPT_SENDER_CLASSES) - classes):
        bad.append(
            f"{site[0]}: class {site[1]} is exempted here but no longer exists. A "
            "dead exemption silently widens the hole -- delete the entry."
        )
    return bad


FIX_ADVICE = (
    "The funnel is src.notifier.send_owner_alert -> TelegramNotifier.send; it is "
    "the only path that writes the notifier_sends row the dashboard surfaces as "
    "undelivered. The owner reads the dashboard and nothing else."
)


def main(argv: list[str] | None = None) -> int:
    try:
        bad = violations()
    except TreeUnreadable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        print(
            "Owner-alert send path(s) outside the funnel:\n"
            + "\n".join(f"  - {b}" for b in bad) + "\n" + FIX_ADVICE,
            file=sys.stderr,
        )
        return 1
    print("owner-alert funnel guard: no send path outside the funnel.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
