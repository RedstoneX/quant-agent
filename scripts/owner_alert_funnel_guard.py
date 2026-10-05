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

   Rule 3 is ABSOLUTE and computed from the parsed source: any call whose
   receiver names a notifier and whose method is ``send`` is refused unless its
   file is in ``EXEMPT_DIRECT_SEND_SITES``, each with its reason. It matches
   code (the AST), never comments or docstrings, so prose cannot trip it and a
   rename or reformat cannot hide a call. No trunk read, no stored count.

WHAT IT COVERS, AND WHAT IT DOES NOT
------------------------------------
Covered: tracked ``.py`` and ``.sh`` files under ``src/``, ``ops/`` and
``scripts/``, plus the tracked ``.py``/``.sh`` files at the repository ROOT --
every place a production send can be written in. The root is named explicitly
because ``main.py`` is the live entry point and sends to the owner channel, and
a directory-only list skipped it.

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

#: The repository ROOT is scanned too, and separately, because `main.py` lives
#: there and sends to the owner channel twice. A directory list alone missed it
#: entirely: the guard reported a clean tree while the live entry point could
#: grow a new bypass nobody would be refused for. The pathspec uses git's
#: `:(glob)` magic, where `*` does not cross a `/`, so this adds top-level
#: files ONLY and does not silently re-scan the whole tree.
ROOT_PATTERNS = (":(glob)*.py", ":(glob)*.sh")

#: The funnel itself, and the package it lives in.
FUNNEL_FILE = "src/notifier/transport.py"
FUNNEL_PACKAGE = "src/notifier/"

#: This file's own path. Its docstring and advice quote a Bot API URL and a
#: ``notifier.send(...)`` example to explain the rule, so scanning it would
#: report the guard as its own violation. Exactly this one exact path is
#: skipped -- not a directory, not a pattern.
GUARD_FILE = "scripts/owner_alert_funnel_guard.py"

TELEGRAM_URL_MARKER = "api.telegram.org/bot"

#: Files allowed a bare notifier send, by exact path, each with its reason.
#: An entry that stops matching a call is itself a failure.
EXEMPT_DIRECT_SEND_SITES = {
    "scripts/alert_heartbeat.py": (
        "DELIBERATE: a daily probe of the raw send path; retrying or recording "
        "through the funnel would hide the very failure it exists to detect."
    ),
    "scripts/telegram_test.py": (
        "DELIBERATE: the operator's manual raw-path check; it must report the "
        "unretried truth of one send."
    ),
    "src/scheduler.py": (
        "A routine session report, not an alert: it has its own record and "
        "is not an owner warning."
    ),
}

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
    patterns += list(ROOT_PATTERNS)
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
    return sorted(p for p in paths if p != GUARD_FILE)


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


def _direct_send_lines(text: str, path: str = "<source>") -> list[str]:
    """Source of every ``<notifier-ish>.send(...)`` call, from the parsed tree."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise TreeUnreadable(
            f"cannot parse tracked file {path}, so this guard measured nothing "
            f"there and REFUSES rather than report a clean tree: {exc}"
        ) from exc
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send"
            and "notifier" in ast.unparse(node.func.value).lower()
        ):
            hits.append(ast.unparse(node))
    return sorted(hits)


def direct_send_sites(paths: list[str]) -> dict[str, list[str]]:
    """Per-file direct-send call lines, outside the notifier package."""
    out: dict[str, list[str]] = {}
    for path in paths:
        if not path.endswith(".py") or path.startswith(FUNNEL_PACKAGE):
            continue
        text = read(path)
        if ".send" not in text:
            continue
        hits = _direct_send_lines(text, path)
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
    for path in sorted(set(now) - set(EXEMPT_DIRECT_SEND_SITES)):
        for line in now[path]:
            bad.append(
                f"{path}: direct notifier send `{line}` outside the funnel. "
                "send_owner_alert_with_outcome retries on the desk's backoff policy "
                "and records one counted owner_alert_undelivered row when every "
                "attempt fails; a bare send does neither, so a lost alert reads "
                "as a delivered one."
            )
    for path in sorted(set(EXEMPT_DIRECT_SEND_SITES) - set(now)):
        bad.append(
            f"{path}: exempted here but no longer makes a direct send. A dead "
            "exemption silently widens the hole -- delete the entry."
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
