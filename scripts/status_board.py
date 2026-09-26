#!/usr/bin/env python3
"""Generate the human status board by RE-DERIVING every claim from the system.

Why this exists
---------------
Every human-facing status document in this repo has gone stale, repeatedly and
expensively. A human-facing compass document, since retired, carried a production
SHA three deploys out of date and a claim that the account had no margin (it is a
4x margin account).
`STATE.md` named a production version three deploys stale. The remediation spec
said Phase 2b was undeployed when it had been live for a day. Five separate
wrong claims inside two days, every one of them a fact somebody had to REMEMBER
to update and did not.

So this board records nothing. It reads `docs/WORK.md` — the backlog that is the
single source of truth for what this desk is doing — and `docs/phases.yaml`,
where each phase carries mechanically checkable evidence rules. It re-evaluates
every rule against the current tree, reads live state off the production box and
its database, and renders what it found. A separate file, `docs/BOARD_NOTES.md`,
supplies the owner-facing prose rendered alongside each item — see "The prose
problem" below for why that is a second file rather than a section of WORK.md.

The important output is not "phase 3 is done". It is the DISAGREEMENT case: a
phase recorded as done whose evidence no longer holds is reported as
`CONTRADICTED`, loudly. That is the rot detector, and it is the whole point.

Anything that cannot be established mechanically renders as `unknown`. It never
guesses, and it never falls back to the recorded claim.

Who this page is for
--------------------
One reader: the owner, on a phone, over Tailscale. He is trader-minded and is
neither a developer nor a finance professional. The page he was reading before
this rewrite was written for engineers and he could not use it, so every
reader-facing string here is plain English: no file paths, no function names,
no commit references, no jargon. The engineering detail still exists — it lives
in the backlog and the incident history, which is where engineers read it.

The shape of the page follows the hand-maintained "one thing at a time" page it
replaces, because that shape worked for him:

  * one prominent RIGHT NOW card — exactly one thing needing him, never more;
  * for every item: what it is in plain language, a concrete real-world
    example, and where a ruling is needed, the decision plus a recommendation;
  * a short numbered queue of what is next, one line each;
  * what is being built RIGHT NOW — read off the open pull requests on
    GitHub (see `src/inflight.py`), never typed in — alongside what he has
    already decided, so his own rulings are never queued back at him as
    questions and he never has to ask what is under construction;
  * everything that needs nothing from him and is not being built — parked,
    checked-and-by-design, finished — behind ONE closed disclosure with a
    count on it. His words, 2026-09-12: "If it's done, it's done and in the
    past, don't carry it, don't tell me, I don't care." The buckets are still
    computed (the backlog's own tidiness checks depend on them); they are
    just not a list he scrolls past on a phone.

The prose problem, and the convention that solves it
----------------------------------------------------
A plain-language explanation, a real-world example and a recommendation cannot
be derived from the code — they are prose, and somebody has to write them.
Putting them in this script would recreate exactly the hand-maintained document
this board exists to replace. So they live in `docs/BOARD_NOTES.md`, keyed to
the item they describe by its NUMBER and section — never by its title, which
can be reworded without warning.

`docs/BOARD_NOTES.md` is deliberately a file of its own, separate from
`docs/WORK.md`. `docs/WORK.md` stays the agent-facing source of truth for what
an item IS — its number, title, status and ordering, everything this script
re-derives — and is mechanically capped at 100,000 bytes (see
`tests/test_status_board.py::test_work_md_stays_under_a_hundred_thousand_bytes`)
precisely because it is meant to stay a short, current backlog, not a growing
prose archive. Owner-facing explanation does not belong under that cap or in
that file: it has a different author, a different reader, and no mechanical
reason to be size-limited. An item's number and its section (the funnel queue,
the PM test gate, or a pending decision's due date) is the only thing that
connects the two files — see `load_board_notes`, `QueueItem.ref` and
`PendingDecision.ref` for exactly how that key is spelled.

The convention, inside `docs/BOARD_NOTES.md`, under a heading naming the item
it describes ("## item 32", "## gate item 4", "## decision due 2026-09-16"),
each field on its own line:

    **Plain language —** what this is, in words a trader understands.
    **Example —** a concrete case that makes it tangible.
    **The decision —** what the owner specifically has to rule on.
    **Recommendation —** what we think he should do, stated as a
    recommendation.

Rules, deliberately few and deliberately dumb:

  * The label must start the line. Leading whitespace is fine, the
    surrounding `**` is optional, and the separator may be an em dash, a
    hyphen or a colon.
  * A block runs until the next label, the next heading, or a BLANK LINE. One
    paragraph per label. Wrapped lines are fine; a blank line ends the block.
    That keeps ordinary commentary further down a note from being swallowed
    into the recommendation.
  * Nothing is mandatory, and nothing is invented. An item with no plain-
    language block — whether because nobody has written to
    `docs/BOARD_NOTES.md` for it yet, or because `docs/WORK.md` names an item
    number no note names — renders with an explicit "not yet explained in
    plain language" marker — never hidden, never dropped, and never
    auto-generated into fake-friendly prose. Inventing an explanation would
    recreate the staleness this board exists to prevent.
  * Prose is checked by the same mechanical jargon detector the phase
    summaries use, so an explanation written for a developer is visibly
    marked as one rather than quietly passing as plain English.

`docs/WORK.md` still supplies everything this convention does NOT: an item's
number, its title, its open/paused/resolved status, and its ordering. Only the
prose source moved.

Usage
-----
    python scripts/status_board.py --out data/board/index.html

Runs read-only. It never writes to the production checkout or its database.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# What is being built right now, read off GitHub's open pull requests. Lives
# in src/ rather than here because the board SERVER re-reads it at request
# time (src/api/server.py), and scripts/ may import src/, never the reverse.
from src import inflight  # noqa: E402


PROD_CHECKOUT = Path("/home/qamc/quant-agent")
ET = ZoneInfo("America/New_York")

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"

#: A test/symbol rule must name an identifier, not describe one in prose.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


# --------------------------------------------------------------------------
# shelling out
# --------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 20) -> tuple[int, str]:
    """Run a command, never raise. Returns (returncode, stdout+stderr)."""
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:  # noqa: BLE001 - a broken probe must not kill the board
        return 127, f"{type(exc).__name__}: {exc}"


def _read_prod(rel: str) -> tuple[bool, str]:
    """Read a file from the production checkout, whoever we happen to be.

    On the box this script runs as `qamc` and can read directly. From the
    engineering account it needs `sudo -n -u qamc`, which is configured. If
    neither works we report unknown rather than inventing a value — the whole
    point of this board.
    """
    target = PROD_CHECKOUT / rel
    if os.access(target, os.R_OK):
        try:
            return True, target.read_text()
        except OSError:
            pass
    rc, out = _run(["sudo", "-n", "-u", "qamc", "cat", str(target)])
    return (rc == 0), out


def _prod_git(*args: str) -> tuple[bool, str]:
    direct = ["git", "-C", str(PROD_CHECKOUT), *args]
    rc, out = _run(direct)
    if rc == 0:
        return True, out
    rc, out = _run(["sudo", "-n", "-u", "qamc", *direct])
    return (rc == 0), out


# --------------------------------------------------------------------------
# evidence rules
# --------------------------------------------------------------------------

@dataclass
class RuleResult:
    kind: str
    verdict: str
    note: str
    detail: str = ""


def _setting(cfg: dict, dotted: str) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return KeyError
        node = node[part]
    return node


def check_rule(rule: dict, cfg: dict, repo_root: Path = REPO_ROOT) -> RuleResult:
    kind = str(rule.get("kind", "?"))
    note = str(rule.get("note", ""))

    if kind == "manual":
        return RuleResult(kind, UNKNOWN, note, "needs a human to confirm")

    if kind == "commit_in_main":
        sha = str(rule.get("sha", ""))
        rc, _ = _run(["git", "merge-base", "--is-ancestor", sha, "origin/main"], repo_root)
        ok = rc == 0
        return RuleResult(kind, PASS if ok else FAIL, note,
                          f"{sha[:9]} {'is' if ok else 'is NOT'} in main")

    if kind == "pr_merged":
        num = rule.get("number")
        # Ask git before asking GitHub. A merged PR leaves its own merge
        # commit in main's history ("Merge pull request #N from ..."), which
        # is the same fact, checkable offline, with no credential.
        #
        # This matters where the board actually runs. `gh` is installed on the
        # production box but the runtime account is not authenticated, and
        # putting a GitHub token on the account that trades is a credential
        # decision for the owner, not a convenience for this script. Without
        # the git path, 13 of the manifest's rules would report `unknown` on
        # the box for no better reason than that.
        #
        # `repo_root` defaults to this checkout but is injectable so tests can
        # point it at a throwaway repo with known history — the production
        # box's git history is not a fixture and a CI runner's shallow clone
        # is not full history either.
        rc, out = _run(
            ["git", "log", "origin/main", "--merges", "--format=%s",
             f"--grep=^Merge pull request #{num} from ", "-1"],
            repo_root,
        )
        if rc == 0 and out.strip():
            return RuleResult(kind, PASS, note, f"PR #{num} merge commit is in main")
        # No merge commit found. That is not proof of absence — a squash or
        # rebase merge leaves none — so fall through to GitHub rather than
        # calling it a failure, and report unknown if that is unavailable too.
        rc, out = _run(["gh", "pr", "view", str(num), "--repo", "RedstoneX/quant-agent",
                        "--json", "state", "-q", ".state"], repo_root)
        if rc != 0:
            return RuleResult(
                kind, UNKNOWN, note,
                f"PR #{num}: no merge commit in main, and GitHub is unreachable "
                "from here (the runtime account has no gh credential)")
        return RuleResult(kind, PASS if out.strip() == "MERGED" else FAIL, note,
                          f"PR #{num} is {out.strip()}")

    if kind == "file_exists":
        p = REPO_ROOT / str(rule.get("path", ""))
        return RuleResult(kind, PASS if p.exists() else FAIL, note, str(rule.get("path")))

    if kind == "symbol_in_file":
        p = REPO_ROOT / str(rule.get("path", ""))
        sym = str(rule.get("symbol", ""))
        if not p.exists():
            return RuleResult(kind, FAIL, note, f"{rule.get('path')} is missing")
        try:
            found = sym in p.read_text(errors="replace")
        except OSError as exc:
            return RuleResult(kind, UNKNOWN, note, str(exc))
        return RuleResult(kind, PASS if found else FAIL, note,
                          f"{sym!r} {'found' if found else 'NOT found'} in {rule.get('path')}")

    if kind == "test_exists":
        p = REPO_ROOT / str(rule.get("path", ""))
        test = str(rule.get("test", ""))
        # A malformed rule must never read as a failing system. The first real
        # run of this board reported Phase 1 as CONTRADICTED because its rule
        # carried a prose description ("test_context.py exists as a dedicated
        # test module ... (27 tests per the spec's own note)") where a test
        # identifier belongs. The file was fine; the ruler was bent. A broken
        # instrument is reported as unknown, loudly, and never as rot.
        if not _IDENTIFIER.match(test):
            return RuleResult(kind, UNKNOWN, note,
                              f"malformed rule: {test!r} is prose, not a test name")
        if not p.exists():
            return RuleResult(kind, FAIL, note, f"{rule.get('path')} is missing")
        try:
            found = test in p.read_text(errors="replace")
        except OSError as exc:
            return RuleResult(kind, UNKNOWN, note, str(exc))
        return RuleResult(kind, PASS if found else FAIL, note,
                          f"{test} {'present' if found else 'MISSING'}")

    if kind == "setting_equals":
        got = _setting(cfg, str(rule.get("key", "")))
        if got is KeyError:
            return RuleResult(kind, FAIL, note, f"{rule.get('key')} not present in settings")
        want = rule.get("value")
        ok = str(got) == str(want)
        return RuleResult(kind, PASS if ok else FAIL, note,
                          f"{rule.get('key')} = {got!r} (expected {want!r})")

    if kind == "setting_present":
        # Unlike setting_equals, this rule makes no claim about the value —
        # only that the key exists at all. That is the shape needed for a
        # setting that is expected to keep changing (a stopgap re-tuned over
        # time): pinning a specific value would make every legitimate re-tune
        # look like rot, and the day the key is finally removed on purpose is
        # exactly the moment a value-pinned rule would go quiet instead of
        # flagging that the phase needs re-evaluating.
        got = _setting(cfg, str(rule.get("key", "")))
        present = got is not KeyError
        return RuleResult(kind, PASS if present else FAIL, note,
                          f"{rule.get('key')} {'is present' if present else 'is NOT present'} "
                          "in settings")

    return RuleResult(kind, UNKNOWN, note, f"unrecognised rule kind {kind!r}")


# --------------------------------------------------------------------------
# is this summary written for HIM, or for a developer?
# --------------------------------------------------------------------------
#
# `plain_summary` exists so the owner — who reads this board and nothing
# else, and is not a developer — can tell what happened without opening the
# code. It lives inside docs/phases.yaml, an engineering document maintained
# by engineering agents, so left unwatched it fills back up with PR numbers,
# file paths and function names the moment the next agent writes one. That
# already happened: several summaries below carry exactly that.
#
# Rejecting jargon outright — refusing to accept a summary that contains it —
# was tried and rejected, correctly. Blocking "bad words" produces
# jargon-free prose that is still useless to him; it does not produce good
# writing. What this board can do honestly is detect the MECHANICAL SHAPE of
# engineering text — a path, a PR number, a hash, a code identifier — and
# show it to him, the one person who can actually judge whether a summary
# reads like it was written for him. It never blocks and never rewrites: a
# bad description still renders, because an unreadable description is more
# useful than no description at all.
#
# Deliberately absent: a wordlist of "technical-sounding" words. That would
# flag ordinary sentences as readily as real jargon and teach him to ignore
# the marker — the exact failure mode blocking on it was rejected for.

_JARGON_PATH_EXT = re.compile(
    r"\b[\w-]+\.(?:py|ya?ml|md|html|json|db|sh|toml|cfg|ini|log|txt)\b", re.I)
#: A leading "/" not glued onto a digit, so "3/15/2026" (a date) and "1/3" (a
#: fraction) don't read as `/home/qamc/quant-agent` does.
_JARGON_ABS_PATH = re.compile(r"(?<!\w)/[\w.-]+(?:/[\w.-]+)+")
#: A directory this repo actually has, one more path segment deep, with no
#: extension required — catches a bare directory mention like `src/backtest/`
#: that `_JARGON_PATH_EXT` would miss. Anchored to real top-level directory
#: names (not e.g. bare "data/") so an ordinary slash pairing in prose —
#: "cost/benefit", "buy/sell", "his/her" — never matches: none of those
#: words is followed by a second "/segment".
_JARGON_DIR_ONLY = re.compile(r"\b(?:docs|src|scripts|tests|config|data)/[\w.-]+/[\w.-]*")
_JARGON_PR_REF = re.compile(r"#\d+\b")
_JARGON_HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
_JARGON_BACKTICK = re.compile(r"`[^`]+`")
_JARGON_SNAKE_CASE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
#: `word(...)`, but not `word(s)` / `word(es)` — ordinary English pluralises
#: that way ("trade(s)") and it must not read as a function call.
_JARGON_FUNC_CALL = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\((?!s\)|es\))[^()]*\)")


def _is_commit_hash_token(token: str) -> bool:
    """7+ hex characters is also a plain 7-digit number — a dollar figure, a
    count, a year. A decimal number can't contain a-f, so require at least
    one of those letters before calling it a hash; otherwise an ordinary
    big number sitting in the text would read as a commit reference."""
    return any(c in "abcdefABCDEF" for c in token)


def summary_engineering_markers(summary: str) -> list[str]:
    """The mechanical, engineer-facing markers found in `summary` — empty if
    none. Each one is a SHAPE (a path, a reference number, a hash, a code
    token), never a word choice, so this cannot flag a summary for sounding
    technical — only for literally containing developer syntax. See the
    section comment above for why that line is drawn there.
    """
    found: list[str] = []
    if (_JARGON_PATH_EXT.search(summary) or _JARGON_ABS_PATH.search(summary)
            or _JARGON_DIR_ONLY.search(summary)):
        found.append("a file path")
    if _JARGON_PR_REF.search(summary):
        found.append("a PR or issue number")
    if any(_is_commit_hash_token(t) for t in _JARGON_HEX_TOKEN.findall(summary)):
        found.append("a commit hash")
    if (_JARGON_BACKTICK.search(summary) or _JARGON_SNAKE_CASE.search(summary)
            or _JARGON_FUNC_CALL.search(summary)):
        found.append("a code identifier")
    return found


def summary_is_engineer_facing(summary: str) -> tuple[bool, str]:
    """Whether `summary` reads as written for an engineer instead of the
    owner, and, in his own words, why — the reason is what actually renders
    on the board, so he never has to take the flag on faith.

    A missing or empty summary is flagged too, with its own reason: silence
    is not neutral here, it is a plain-English description nobody wrote.
    """
    text = (summary or "").strip()
    if not text:
        return True, "no plain-English description was written for this item"
    markers = summary_engineering_markers(text)
    if not markers:
        return False, ""
    return True, "reads like engineering notes — it contains " + ", ".join(markers)


# --------------------------------------------------------------------------
# phases
# --------------------------------------------------------------------------

@dataclass
class PhaseView:
    id: str
    title: str
    # `plain_summary` in the manifest is either a plain string (legacy shape,
    # rendered exactly as it always has been) or a structured dict with
    # `verdict`, `points`, and an optional `outstanding` (see
    # `_render_summary`). Kept as `Any` and passed through unconverted so
    # both shapes survive to render time — coercing to `str` here would
    # flatten a dict into Python's `repr`, which is the bug this comment is
    # here to stop someone reintroducing.
    summary: Any
    recorded: str
    confidence: str
    results: list[RuleResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.verdict == PASS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.verdict == FAIL)

    @property
    def unknown(self) -> int:
        return sum(1 for r in self.results if r.verdict == UNKNOWN)

    @property
    def checkable(self) -> int:
        return self.passed + self.failed

    @property
    def summary_flagged(self) -> bool:
        """True when `summary` reads as written for an engineer, not him —
        see `summary_is_engineer_facing` for what that checks and why."""
        flagged, _ = summary_is_engineer_facing(_summary_text(self.summary))
        return flagged

    @property
    def summary_flag_reason(self) -> str:
        """Plain-English reason `summary_flagged` is True; "" when it isn't."""
        _, reason = summary_is_engineer_facing(_summary_text(self.summary))
        return reason

    @property
    def verdict(self) -> str:
        """CONFIRMED / CONTRADICTED / UNVERIFIED.

        CONTRADICTED is the one that matters: the manifest says this phase is
        done, but its own proof no longer holds. That is documentation rot
        caught the moment it happens instead of five days later.
        """
        if self.failed:
            return "CONTRADICTED"
        if self.checkable == 0:
            return "UNVERIFIED"
        return "CONFIRMED"


# ---------------------------------------------------------------------------
# The funnel queue — read from docs/WORK.md, never typed in here.
#
# The owner asked, plainly: "is that accessible via webpage... there's code
# that pulls it from stuff that you look at so that I can see it
# automatically." That is the whole requirement. The ranked list of why
# trades do not happen lives in `docs/WORK.md` because that is the file a
# resumed session reads and CI checks; duplicating it here would create a
# second copy that drifts, which is the exact failure this project keeps
# hitting. So this parses the real thing.
#
# It degrades LOUDLY. If the heading moves or the item shape changes, the
# section says it could not read the queue rather than rendering empty and
# looking like there is no work — same principle as the rest of this board:
# say unknown, never guess.
# ---------------------------------------------------------------------------

#: The classifications the queue uses. Order matters: longest first, so
#: "TOO NEW TO CLASSIFY" is not swallowed by a prefix match on a shorter one.
_QUEUE_CLASSES = (
    "TOO NEW TO CLASSIFY",
    "NOT YET DIAGNOSED",
    "WORKING AS INTENDED",
    "TOO STRICT",
    "NO RECORD",
    "DEFECT",
)

#: The OPENING of an item: `**3. ` or `**~~3. `. Everything after it — the
#: rest of the bold headline, and any body prose sitting on the same physical
#: line — is handled by `_headline_and_body` below.
#:
#: This deliberately does NOT require the bold to close at end of line. The
#: older shape did, and it silently dropped every item whose author wrote body
#: text after the closing `**`, or whose headline wrapped onto a second line.
#: That is real and common: ten live backlog items were invisible on the
#: owner's board for that reason alone, three of them added on 2026-09-10 or
#: 2026-09-11 (dated from git, not from impression). An item he cannot see at
#: all is worse than one he sees imperfectly.
_ITEM_OPEN_RE = re.compile(r"^\*\*(?:~~)?(\d+)\.\s*(.*)$")

#: The legacy strict shape — bold from the item number to end of line — kept
#: ONLY for the CI closure check. See `find_closed_items_not_marked_done` for
#: why that one check is not widened along with the renderer.
_QUEUE_ITEM_RE = re.compile(r"^\*\*(?:~~)?(\d+)\.\s+(.+?)\*\*\s*$")

_QUEUE_SHARE_RE = re.compile(r"(\d+)\s+of\s+(\d+)\s*\((\d+)%\)")
_QUEUE_HEADING = "## THE FUNNEL QUEUE"

#: A markdown heading ends an item's body: the next section is not this
#: item's prose.
_HEADING_RE = re.compile(r"^#{1,6}\s")

#: How many physical lines a wrapped bold headline may span before we stop
#: hunting for its closing `**`. An unbounded search would swallow a whole
#: section into one title; three lines covers every real case in the backlog.
_MAX_HEADLINE_LINES = 3


# ---------------------------------------------------------------------------
# The plain-language convention — see this module's docstring for the rules,
# and for why this prose lives in the backlog instead of in here.
# ---------------------------------------------------------------------------

#: Label -> field. Aliases exist because these labels are typed by hand by
#: whoever writes the item, and "Decision" reads as naturally as "The
#: decision". Matched case-insensitively.
_PROSE_LABELS = {
    "plain language": "plain",
    "plain english": "plain",
    "example": "example",
    "the decision": "decision",
    "decision": "decision",
    "recommendation": "recommendation",
    "my recommendation": "recommendation",
    "why only you": "why_him",
    "why only him": "why_him",
    "why him": "why_him",
}

#: `**Plain language —** text`, `Plain language: text`, `  EXAMPLE - text`.
#: Bold markers and separator are optional; the label must START the line
#: (leading whitespace allowed, because a pending decision's body is indented
#: under its checkbox).
_PROSE_LINE_RE = re.compile(
    r"^\s*\*{0,2}\s*("
    + "|".join(re.escape(k) for k in sorted(_PROSE_LABELS, key=len, reverse=True))
    + r")\s*\*{0,2}\s*[—–:-]\s*\*{0,2}\s*(.*?)\s*$",
    re.I,
)

#: Inline markdown that must never reach the page as literal characters.
_MD_MARKS = re.compile(r"\*\*|__|~~")
_MD_CODE = re.compile(r"`([^`]*)`")

#: A SINGLE-asterisk emphasis delimiter — `*like this*`, which the backlog
#: uses and which used to reach the page as two stray asterisks.
#:
#: Deliberately not a bare `\*`. A lone asterisk between two word characters
#: is multiplication, and the backlog writes real arithmetic ("entry - 2*atr")
#: that must survive verbatim; mangling a formula would be worse than leaving
#: an asterisk in. So only a delimiter shape is removed: an asterisk that
#: OPENS a span (nothing word-like before it, something non-space after) or
#: CLOSES one (something non-space before it, nothing word-like after).
_MD_EMPH_OPEN = re.compile(r"(?<![\w*])\*(?=[^\s*])")
_MD_EMPH_CLOSE = re.compile(r"(?<=[^\s*])\*(?![\w*])")


def _strip_markdown(text: str) -> str:
    """Plain text for a human, out of markdown written for a file."""
    text = _MD_CODE.sub(r"\1", text)
    text = _MD_MARKS.sub("", text)
    text = _MD_EMPH_OPEN.sub("", text)
    text = _MD_EMPH_CLOSE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Prose:
    """The owner-facing prose an item carries, if its author wrote any.

    Every field defaults to empty, and empty means exactly that: nobody has
    written it yet. It is never filled in from elsewhere, never summarised out
    of the engineering body, and never guessed. An invented explanation is the
    same staleness this board exists to prevent, wearing a friendlier face.
    """

    plain: str = ""
    example: str = ""
    decision: str = ""
    recommendation: str = ""
    #: The one reason THIS decision cannot be made without him — money,
    #: mandate, risk appetite, or public disclosure. Deliberately separate
    #: from `decision`: a decision can be real and still not be his (a
    #: chart-structure constant is a real open question with nobody to rule
    #: on it but a published source or this desk's own data — see items
    #: 52/55/56/58, which all write "The decision — None for you" for
    #: exactly that reason). Only an item with BOTH a live `decision` and a
    #: `why_him` is drawn in "Waiting on you"; see `_is_live_owner_ask`.
    why_him: str = ""

    @property
    def has_any(self) -> bool:
        return bool(self.plain or self.example or self.decision
                    or self.recommendation or self.why_him)

    @property
    def jargon_markers(self) -> list[str]:
        """Engineering shapes found in the prose actually shown to him. Reuses
        the phase-summary detector, so one definition of "this was written for
        a developer" covers the whole page."""
        joined = " ".join(p for p in (self.plain, self.example, self.decision,
                                      self.recommendation) if p)
        return summary_engineering_markers(joined)


def parse_prose(body_lines: list[str]) -> Prose:
    """Pull the labelled plain-language blocks out of an item's body.

    Tolerant by design: an unrecognised line is ordinary body prose and is
    ignored, a repeated label wins on its last occurrence, and a blank line
    closes the current block so engineering notes further down the item cannot
    be absorbed into a recommendation.
    """
    found: dict[str, list[str]] = {}
    current: str | None = None
    for raw in body_lines:
        if not raw.strip():
            current = None
            continue
        m = _PROSE_LINE_RE.match(raw)
        if m:
            current = _PROSE_LABELS[m.group(1).lower()]
            found[current] = [m.group(2)] if m.group(2) else []
            continue
        if current:
            found[current].append(raw.strip())
    return Prose(
        plain=_strip_markdown(" ".join(found.get("plain", []))),
        example=_strip_markdown(" ".join(found.get("example", []))),
        decision=_strip_markdown(" ".join(found.get("decision", []))),
        recommendation=_strip_markdown(" ".join(found.get("recommendation", []))),
        why_him=_strip_markdown(" ".join(found.get("why_him", []))),
    )


#: The heading `docs/BOARD_NOTES.md` uses to key a prose block to the item it
#: describes: "## item 32", "## gate item 4", "## decision due 2026-09-16".
#: Deliberately the SAME strings `QueueItem.ref` and `PendingDecision.ref`
#: already render to the owner, so a lookup is one dict access and the key
#: survives an item being retitled in `docs/WORK.md` — it names the item's
#: number and section, never its title.
_BOARD_NOTES_HEADING_RE = re.compile(
    r"^#{1,6}\s+(item\s+\d+|gate\s+item\s+\d+|decision\s+due\s+\d{4}-\d{2}-\d{2})\s*$",
    re.I,
)


def load_board_notes(path: Path) -> dict[str, Prose]:
    """Parse `docs/BOARD_NOTES.md` into ``{identifier: Prose}``.

    Keyed by the owner-facing identifier the page already shows for that item
    (`QueueItem.ref` / `PendingDecision.ref`) — ``"item 32"``, ``"gate item
    4"``, ``"decision due 2026-09-16"`` — never by title, so a rename in
    `docs/WORK.md` cannot silently orphan the note written for it, and a
    lookup by the same two callers that already compute `.ref` is a single
    dict access.

    A missing file, or one with no recognised heading, is not an error: it
    just means nothing has been written yet, which every caller already
    renders as the explicit "not yet explained in plain language" marker
    rather than a crash. Same defensive posture as `load_funnel_queue` and
    `load_pending_decisions` — a malformed notes file must never break the
    page, only leave more of it unexplained.
    """
    if not path.exists():
        return {}
    notes: dict[str, Prose] = {}
    key: str | None = None
    lines: list[str] = []

    def _flush() -> None:
        if key is not None:
            notes[key] = parse_prose(lines)

    for raw in path.read_text().splitlines():
        m = _BOARD_NOTES_HEADING_RE.match(raw.strip())
        if m:
            _flush()
            key = " ".join(m.group(1).lower().split())
            lines = []
            continue
        if key is not None:
            lines.append(raw)
    _flush()
    return notes


#: An open item whose own headline carries one of these is not waiting on
#: anybody — it is parked on purpose. It goes in the "no decision needed"
#: section so it stops competing for the owner's attention, which is the
#: entire reason that section exists.
_PAUSED_WORDS = ("PAUSED", "PARKED", "DEFERRED", "ON HOLD", "NOT SCHEDULED",
                 "MOOT")

#: A status that NEGATES its own closure word — "deliberately NOT done yet",
#: "STILL BROKEN, this file's own FIXED claim was wrong". Both are real lines
#: from the live backlog, and both read as closure claims to a plain word
#: search. A marker that cries wolf gets ignored, which costs more than not
#: having the marker at all.
#:
#: The future-tense entries ("TO BE REPLACED", "WILL BE SHIPPED") are cheap
#: insurance added alongside the widened vocabulary below: the new words are
#: all past participles, and a past participle in a plan reads identically to
#: one in a result unless the tense in front of it is read too.
_CLOSURE_NEGATIONS = ("NOT ", "STILL BROKEN", "STILL OPEN", "NEVER ",
                      "WAS WRONG", "NO LONGER", "INCOMPLETE",
                      "TO BE ", "WILL BE ", "SHOULD BE ", "NEEDS TO BE ",
                      "YET TO BE ")

# ---------------------------------------------------------------------------
# IN HAND — decided, or being built. Nothing needed from him.
#
# The owner's complaint, in his words: "I know for a fact I've already
# approved a bunch of things. Either you're working on them or they're done
# ... why am I seeing it on the board? How am I supposed to find the next
# thing to do, to review?"
#
# Measured against the live backlog on 2026-09-12, the cause was that the
# board had NO state between "open" and "finished". An item he had already
# ruled on (item 49, "DECIDED 2026-09-12 by the owner") and an item being
# actively built (item 1, "IN FLIGHT") both fell into `open`, and `open` is
# what the running order and the RIGHT NOW card are drawn from. So his own
# decisions were queued back at him as if they were still his to make.
#
# Two vocabularies, read the same way the closure words are — on word
# boundaries, from the item's own status text, never from a neighbour's:
#
#   decided   — the owner has given his answer; the build has not landed
#   building  — somebody is actively on it
#
# Both go in one section, below everything that is actually waiting on him,
# each line saying which of the two it is.
# ---------------------------------------------------------------------------

#: The owner has ruled. "OWNER CALL" is deliberately absent: the backlog
#: writes "STILL OPEN, OWNER CALL" to mean a call is NEEDED, the opposite.
_IN_HAND_DECIDED_WORDS = ("DECIDED", "RATIFIED", "APPROVED", "OWNER-REQUESTED",
                          "OWNER'S DESIGN", "OWNER'S RULING", "OWNER RULING")

#: Somebody is on it now.
_IN_HAND_BUILDING_WORDS = ("IN FLIGHT", "IN PROGRESS", "IN BUILD", "BEING BUILT",
                           "UNDER WAY", "UNDERWAY", "BUILD UNDERWAY")

#: A word sitting immediately before a status word that reverses it:
#: "NOT DECIDED", "NOT YET APPROVED", "TO BE DECIDED", "AWAITING APPROVAL".
#: Only the few words BEFORE the hit are read, not the whole tail — item 20's
#: tail is "owner's design, 2026-09-02. Do NOT trade on partial evidence", and
#: the blanket "NOT anywhere" rule the closure words use would have read that
#: ruling as un-ruled.
_IN_HAND_NEGATIONS = ("NOT", "NEVER", "BE", "UNTIL", "AWAITING", "PENDING", "NEEDS")
_IN_HAND_LOOKBEHIND_WORDS = 3

#: A STATUS PARAGRAPH in an item's body: a bold lead that OPENS with one or
#: more capitalised words and a date — "**DECIDED 2026-09-12 by the owner:
#: ...**". That is how the backlog records a later change of state without
#: rewriting the headline (item 49 is the live example: its headline still
#: says OPEN, the paragraph under it says DECIDED). The date is required on
#: purpose: it is the same shape as a headline's own status ("SHIPPED
#: 2026-09-04"), and it is what keeps an ordinary bold sentence ("**Three
#: distinct defects, and they compound:**") from being read as a status.
_STATUS_PARAGRAPH_RE = re.compile(
    r"^\*\*([A-Z][A-Z'-]*(?:\s+[A-Z][A-Z'-]*)*),?\s+\d{4}-\d{2}-\d{2}\b")

#: A diagnosis that needs nothing fixed. "WORKING AS INTENDED" is already one
#: of `_QUEUE_CLASSES`; "NOT A DEFECT" is the other way the backlog says it.
#: These are kept in the file because the funnel queue is a ranked list of
#: CAUSES and a cause that turned out to be by design is still a cause — but
#: nothing about one is his to act on, so it must not sit in the running
#: order, and it must never be the RIGHT NOW card.
_NO_ACTION_WORDS = ("WORKING AS INTENDED", "NOT A DEFECT")


def _status_hit(text: str, words: tuple[str, ...]) -> bool:
    """Whether any of `words` appears in `text` as a whole word AND is not
    reversed by one of `_IN_HAND_NEGATIONS` within the few words before it."""
    for w in words:
        for m in re.finditer(r"\b" + re.escape(w) + r"\b", text):
            before = text[:m.start()].split()[-_IN_HAND_LOOKBEHIND_WORDS:]
            if not any(b.strip(",.;:") in _IN_HAND_NEGATIONS for b in before):
                return True
    return False


def status_paragraph_leads(body_lines: list[str]) -> tuple[str, ...]:
    """The capitalised, dated openings of an item's status paragraphs —
    ``("DECIDED",)`` for item 49 — upper-cased. Empty when the body has none,
    which is most items."""
    leads: list[str] = []
    for raw in body_lines:
        m = _STATUS_PARAGRAPH_RE.match(raw.strip())
        if m:
            leads.append(m.group(1).upper())
    return tuple(leads)


# ---------------------------------------------------------------------------
# Identifiers: what the owner can actually quote back
#
# His complaint, in his own words, was that he could see titles but had to
# recite a whole title to ask a question about an item. So every rendered item
# carries the identifier it has in the backlog.
#
# A bare number is NOT that identifier. The backlog runs more than one
# numbered sequence — the funnel queue counts 1..43, and the PM TEST GATE
# counts 1..8 independently — so "item 4" names two different things. Each
# sequence therefore gets a distinct prefix, and the prefix is part of the
# identifier he sees and quotes.
# ---------------------------------------------------------------------------

#: source key -> how the identifier is spoken on the page. The value is the
#: whole disambiguator: "item 32" is a funnel-queue item, "gate item 4" is a
#: PM-test-gate item, and neither can be mistaken for the other when quoted.
_SOURCE_REF_LABEL = {
    "backlog": "item",
    "pm-gate": "gate item",
}

#: Real tracking references, extracted ONLY where they literally appear in the
#: item's own source text. Nothing here is derived, looked up or guessed: if
#: the backlog does not name a PR, the page shows no PR.
_REF_PR_RE = re.compile(r"(?:PR\s*)?#(\d{2,5})\b")
_REF_INCIDENT_RE = re.compile(r"INCIDENT_HISTORY\.md", re.I)
#: A branch name, which the backlog does write ("SHIPPED on
#: `feat/replace-budget-reservation`"). Deliberately excludes `docs/` — that
#: is a real directory in this repo, so `docs/INCIDENT_HISTORY.md` would
#: otherwise be announced to him as a branch. The trailing segment must carry
#: no dot for the same reason: a branch name is not a filename.
_REF_BRANCH_RE = re.compile(r"\b(?:feat|fix|chore|refactor|hotfix)/[A-Za-z0-9_-]{3,}\b")

#: More than this many PR numbers on one item is a list, not a reference (item
#: 37 names a range of eleven). Past the cap the page says how many there are
#: instead of printing all of them.
_MAX_PR_REFS = 3


def extract_refs(source_text: str) -> tuple[str, ...]:
    """Tracking references that are ACTUALLY present in `source_text`.

    Returns owner-readable strings ("PR #252", "incident history"), in a
    stable order, deduplicated. An empty tuple means the backlog named no
    reference for this item — which is reported as nothing at all, never as a
    guess at which PR it might have been.
    """
    refs: list[str] = []

    prs = list(dict.fromkeys(_REF_PR_RE.findall(source_text)))
    if len(prs) > _MAX_PR_REFS:
        refs.append(f"{len(prs)} pull requests named in the backlog")
    else:
        refs.extend(f"PR #{n}" for n in prs)

    if _REF_INCIDENT_RE.search(source_text):
        refs.append("incident history")

    for branch in dict.fromkeys(_REF_BRANCH_RE.findall(source_text)):
        refs.append(f"branch {branch}")

    return tuple(refs)


@dataclass
class QueueItem:
    """One ranked cause of trades not happening."""

    rank: int
    title: str
    classification: str
    share: str
    pct: int | None
    done: bool
    prose: Prose = field(default_factory=Prose)
    #: The item's own headline, markdown stripped. Kept because the bucket
    #: tests below must read the AUTHOR's words, not our tidied title.
    headline: str = ""
    #: Which numbered sequence in the backlog this item came from. Drives the
    #: identifier prefix, so a funnel-queue "4" and a PM-gate "4" are never
    #: quoted back as the same thing. See `_SOURCE_REF_LABEL`.
    source: str = "backlog"
    #: The item's own body text, markdown stripped — the raw engineering notes
    #: as written. Shown only inside the clearly-marked container in
    #: `_render_prose`, and never as if it were a plain-language explanation.
    raw_body: str = ""
    #: Tracking references literally present in the item's source text.
    refs: tuple[str, ...] = ()
    #: The dated, capitalised openings of the body's status paragraphs — see
    #: `status_paragraph_leads`. This is where a later change of state lands
    #: when the headline is not rewritten (item 49's "DECIDED 2026-09-12").
    status_leads: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        """The identifier the owner sees and quotes back. Unambiguous across
        the backlog's several independently-numbered sequences."""
        return f"{_SOURCE_REF_LABEL.get(self.source, 'item')} {self.rank}"

    @property
    def state(self) -> str:
        """Short, owner-facing status. Never a file path or a branch name."""
        if self.done:
            return "done"
        return "open"

    @property
    def status_tail(self) -> str:
        """The STATUS half of the headline: everything after the last em dash.

        The backlog's own shape is `Title — MEASURED SHARE. STATUS.`, and only
        that tail is a claim about where the item stands. Reading the whole
        headline instead produces exactly the false positives that make a
        marker worth ignoring: "a fixed-interval poll" is a description, not a
        claim of being fixed, and three of the six items this flagged on its
        first real run were wrong for that reason.
        """
        head = self.headline
        return (head.rsplit("—", 1)[1] if "—" in head else head).upper()

    @property
    def closure_claim(self) -> str:
        """What this item's OWN status says about being finished.

        Three answers, deliberately, because collapsing them is what makes the
        board misleading in one direction or the other:

          ``"finished"``     nothing is outstanding — "SHIPPED 2026-09-04"
          ``"review_owed"``  the work is done, a review is not — "FIXED,
                             pending review"
          ``"part_done"``    real work remains — "MOSTLY FIXED, one real
                             judgment call left"
          ``""``             it makes no closure claim at all

        A negated status ("deliberately NOT done yet", "STILL BROKEN, this
        file's own FIXED claim was wrong") is not a claim, and reads as ``""``.
        Both of those are real backlog lines that a plain word search got
        wrong, and a marker that cries wolf gets ignored — which costs more
        than not having the marker at all.

        Reads only `status_tail`, never the whole headline, for the same
        reason: "a fixed-interval poll" describes a mechanism and claims
        nothing.

        The closure-word search itself reads `tail` with every cross-
        reference to another PR or item stripped first (`_strip_cross_
        references`) — a status word cited about something ELSE this item's
        tail happens to mention ("...while PR #343 (merged) repaired...")
        is not a claim about this item. Negation is still read on the
        UNSTRIPPED tail: "STILL OPEN" and friends are claims about the item
        itself and must not depend on whether a reference happens to sit
        nearby.
        """
        tail = self.status_tail
        if any(w in tail for w in _CLOSURE_NEGATIONS):
            return ""
        scan = _strip_cross_references(tail)
        if not _closure_hit(scan, _RENDER_CLOSURE_WORDS):
            return ""
        if _closure_hit(scan, _RENDER_PART_DONE_WORDS):
            return "part_done"
        if _closure_hit(scan, _RENDER_REVIEW_OWED_WORDS):
            return "review_owed"
        return "finished"

    @property
    def claims_closure(self) -> bool:
        """Its status says FULLY finished, but it was never marked finished.

        Reported, never believed. An item saying one thing while the backlog's
        strike-through says another is the backlog's version of a CONTRADICTED
        phase — so the page shows it as finished (which is what its own author
        wrote) while saying plainly that the backlog has not been ticked off,
        rather than filing it as live work, which is the statement he called
        déjà vu.
        """
        return not self.done and self.closure_claim == "finished"

    @property
    def review_owed(self) -> bool:
        """Finished by its own account, with a review still outstanding."""
        return not self.done and self.closure_claim == "review_owed"

    @property
    def part_done(self) -> bool:
        """Mostly or partly finished, with real work still outstanding. Stays
        in the running order — labelled, not moved."""
        return not self.done and self.closure_claim == "part_done"

    @property
    def paused(self) -> bool:
        if self.done or self.claims_closure or self.review_owed:
            return False
        return any(w in self.status_tail for w in _PAUSED_WORDS)

    @property
    def in_hand_state(self) -> str:
        """Why this item needs nothing from him, in his words — or ``""``.

          ``"decided, not yet built"``  he has ruled; the build has not landed
          ``"being built"``             somebody is actively on it

        Read from the headline's status tail and from any dated status
        paragraph in the body (`status_leads`), because the backlog records a
        ruling either way. A mostly-finished item (`part_done`) is deliberately
        NOT in hand: real work is outstanding and the tests pin it to the
        running order, labelled.
        """
        if self.done or self.claims_closure or self.review_owed or self.paused:
            return ""
        if self.part_done:
            return ""
        texts = (self.status_tail, *self.status_leads)
        if any(_status_hit(t, _IN_HAND_BUILDING_WORDS) for t in texts):
            return "being built"
        if any(_status_hit(t, _IN_HAND_DECIDED_WORDS) for t in texts):
            return "decided, not yet built"
        return ""

    @property
    def no_action(self) -> bool:
        """A diagnosis that turned out to need nothing fixed — "WORKING AS
        INTENDED", "CHECKED, NOT A DEFECT". Still a ranked cause of trades not
        happening, so still listed; never his to act on."""
        if self.done or self.claims_closure or self.review_owed or self.paused:
            return False
        if self.part_done or self.in_hand_state:
            return False
        return _closure_hit(self.status_tail, _NO_ACTION_WORDS)

    @property
    def bucket(self) -> str:
        """Which section of the page this item belongs in. One item, one
        section, decided once here so no two renderers can disagree.

        ``finished_unmarked`` is the bucket that answers the owner's "déjà vu"
        complaint: an item whose status word the board did not used to
        recognise ("SHIPPED", "REPLACED", "REDESIGNED") was drawn in the
        running order, competing with live work. It is now drawn as finished.

        ``in_hand`` answers the next complaint in the same family: an item he
        had already ruled on, or that was already being built, was drawn in
        the running order too — and, when nothing else outranked it, as the
        RIGHT NOW card, asking him for an answer he had already given.

        ``no_action`` is a cause that turned out to be by design. Listed,
        never queued.
        """
        if self.done:
            return "resolved"
        if self.claims_closure:
            return "finished_unmarked"
        if self.review_owed:
            return "review_owed"
        if self.paused:
            return "paused"
        if self.in_hand_state:
            return "in_hand"
        if self.no_action:
            return "no_action"
        return "open"


def _parse_numbered_items(body: str, source: str = "backlog",
                           notes: dict[str, Prose] | None = None) -> list[QueueItem]:
    """Shared parser behind every `**N. Title — ...**` numbered section this
    board reads. One shape, one parser, so a funnel-queue item and a PM-gate
    item can never silently drift into two different conventions.

    `source` names WHICH numbered sequence these items belong to, and is what
    makes the identifier on the page unambiguous — the funnel queue and the PM
    test gate both number from 1. See `_SOURCE_REF_LABEL`.

    `notes` is `docs/BOARD_NOTES.md`, already parsed by `load_board_notes`
    into ``{identifier: Prose}``. An item's prose is looked up by its own
    `ref` (``"item 32"``, ``"gate item 4"``) — never parsed out of this body
    text, which is `docs/WORK.md` and carries the item itself, not the
    owner-facing explanation of it. Omitted (the default) for callers that
    only care about the item shape, in which case every item's prose is
    empty, which is exactly what an item with no note should show.
    """
    notes = notes or {}
    lines = body.splitlines()
    items: list[QueueItem] = []
    i = 0
    while i < len(lines):
        if not _ITEM_OPEN_RE.match(lines[i].strip()):
            i += 1
            continue
        rank = int(_ITEM_OPEN_RE.match(lines[i].strip()).group(1))
        headline, body_lines, i = _headline_and_body(lines, i)
        share_m = _QUEUE_SHARE_RE.search(headline)
        ref = f"{_SOURCE_REF_LABEL.get(source, 'item')} {rank}"
        items.append(QueueItem(
            rank=rank,
            title=_tidy_title(headline),
            classification=next(
                (c for c in _QUEUE_CLASSES if c in headline.upper()), ""),
            share=share_m.group(0) if share_m else "",
            pct=int(share_m.group(3)) if share_m else None,
            # The opening `~~` sits BEFORE the item number and is consumed by
            # `_ITEM_OPEN_RE`, so only the closing one survives into the
            # headline. Either spelling counts as struck through.
            done="~~" in headline,
            prose=notes.get(ref, Prose()),
            headline=_strip_markdown(headline),
            source=source,
            raw_body=_strip_markdown(" ".join(body_lines)),
            # References are read from the headline AND the body, because
            # that is where the backlog actually writes them, and only from
            # this item's own text — never from a neighbour's.
            refs=extract_refs(headline + " " + " ".join(body_lines)),
            status_leads=status_paragraph_leads(body_lines),
        ))
    return sorted(items, key=lambda i: i.rank)


def _headline_and_body(lines: list[str], start: int) -> tuple[str, list[str], int]:
    """Split one item into (headline, body lines, index of the next line).

    `lines[start]` is known to open an item. The headline is the bold span,
    which may wrap onto the next line or two; the body is everything after the
    closing `**` \u2014 INCLUDING the remainder of that same physical line, which
    is where authors most often put it \u2014 up to the next item or heading.
    """
    m = _ITEM_OPEN_RE.match(lines[start].strip())
    assert m is not None  # the caller has already checked
    rest = m.group(2)
    consumed = 1
    while "**" not in rest and consumed < _MAX_HEADLINE_LINES:
        nxt = start + consumed
        if nxt >= len(lines):
            break
        candidate = lines[nxt].strip()
        if (not candidate or _HEADING_RE.match(candidate)
                or _ITEM_OPEN_RE.match(candidate)):
            break
        rest = rest + " " + candidate
        consumed += 1

    if "**" in rest:
        headline, trailing = rest.split("**", 1)
    else:
        # The bold never closed. Take the headline as written rather than
        # dropping the item: a malformed line must still be visible to him.
        headline, trailing = rest, ""

    body: list[str] = []
    if trailing.strip():
        body.append(trailing.strip())
    i = start + consumed
    while i < len(lines):
        stripped = lines[i].strip()
        if _ITEM_OPEN_RE.match(stripped) or _HEADING_RE.match(stripped):
            break
        body.append(lines[i])
        i += 1
    return headline, body, i


def _tidy_title(rest: str) -> str:
    """The owner-facing name of an item, out of its headline."""
    # Title is everything before the first em dash, which is where the
    # measured share starts. No dash means the whole line is the title.
    title = rest.split("\u2014", 1)[0]
    # An item with no em dash carries its classification inline; strip it
    # so the title stays a plain-English name and never shouts a label.
    #
    # WHOLE WORDS ONLY. Without the boundaries this matched a class name
    # INSIDE an ordinary English word and silently deleted letters out of
    # the middle of a title. It was not hypothetical: the work-queue Stop
    # hook handed back "Most ideas die inside the machinery with ed reason
    # (no_order_built)" because "NO RECORD" matched inside "no recorded".
    # "defective" lost its stem to "DEFECT" the same way. A renderer whose
    # whole job is to hand work back by an accurate name cannot delete
    # words from that name — a mangled title reads as a different item.
    for c in _QUEUE_CLASSES:
        title = re.sub(r"\b" + re.escape(c) + r"\b\.?", "", title, flags=re.I)
    # Stripping a mid-sentence label leaves orphaned punctuation
    # ("misattributes vetoes. , pre-existing"); tidy it so the board never
    # shows the seam where a label used to be.
    title = re.sub(r"\s*[.,]\s*(?=[.,])", "", title)
    title = _strip_markdown(title)
    return title.strip().rstrip(".,").strip("~ ").strip()


def load_funnel_queue(work_md: Path,
                       notes: dict[str, Prose] | None = None
                       ) -> tuple[list[QueueItem], str | None]:
    """Parse the ranked funnel queue out of docs/WORK.md.

    `notes` is `docs/BOARD_NOTES.md`, already parsed by `load_board_notes` —
    the item itself (number, title, status) still comes from `work_md`, only
    its plain-language prose is looked up from `notes`.

    Returns `(items, problem)`. `problem` is a plain-English sentence when the
    queue could not be read, and None when it could — the caller renders the
    sentence instead of an empty list, so a shape change is visible rather
    than silently looking like an empty backlog.
    """
    if not work_md.exists():
        return [], "The backlog file is missing, so the queue could not be read."
    text = work_md.read_text()
    if _QUEUE_HEADING not in text:
        return [], (
            "The backlog no longer has a section headed "
            f"{_QUEUE_HEADING.lstrip('# ')!r}, so the queue could not be read."
        )
    body = text.split(_QUEUE_HEADING, 1)[1]
    # The queue ends at the re-measure gate; anything after is other backlog.
    for stop in ("### Re-measure gate", "\n## ", "\n### "):
        if stop in body:
            body = body.split(stop, 1)[0]

    items = _parse_numbered_items(body, source="backlog", notes=notes)
    if not items:
        return [], (
            "The queue heading is there but no numbered items could be read "
            "from it, so its shape has changed."
        )
    return items, None


#: `## PM TEST GATE` — the owner's own framing, repeated over multiple
#: sessions: the PM model-choice test cannot mean anything until everything
#: feeding the PM is clean. This is a curated INDEX into work already
#: recorded elsewhere in WORK.md (the data-quality audit, the PM-input
#: architecture note) — it exists so the board can show these specific
#: items as their own line items rather than them being buried in prose the
#: board does not otherwise render at all.
_PM_GATE_HEADING = "## PM TEST GATE"
_PM_GATE_STOP = "<!-- END PM TEST GATE -->"

#: An empty gate is a legitimate, deliberate state (every gate item closed)
#: and must be DECLARED in the text, never inferred from the absence of
#: numbered items — the same "shape changed" guard below still has to catch
#: an edit that accidentally deletes every item without meaning to empty the
#: gate. Exact marker, start of a line, inside the gate body.
_PM_GATE_EMPTY_MARKER = "**The gate is EMPTY"


def load_pm_gate(work_md: Path,
                  notes: dict[str, Prose] | None = None
                  ) -> tuple[list[QueueItem], str | None]:
    """Parse the PM-test-readiness gate out of docs/WORK.md.

    `notes` is `docs/BOARD_NOTES.md`, already parsed by `load_board_notes` —
    same lookup-by-`ref` arrangement as `load_funnel_queue`.

    Same shape and same failure behaviour as `load_funnel_queue`: a missing
    heading or an unparseable body is reported as a plain-English problem,
    never rendered as a silent empty (and therefore falsely "nothing is
    blocking this") section.

    Three outcomes for the body once the heading is found:

    * zero items, `_PM_GATE_EMPTY_MARKER` present — the gate is deliberately
      empty (every item closed). Returns `([], None)`.
    * zero items, no marker — the section's shape has changed underneath the
      parser (a bad edit, not a deliberate empty gate). Returns the existing
      "shape has changed" problem.
    * marker present AND items still parse — inconsistent: the body claims
      to be empty while still listing work. Returns a problem rather than
      silently picking one side.
    """
    if not work_md.exists():
        return [], "The backlog file is missing, so the gate could not be read."
    text = work_md.read_text()
    if _PM_GATE_HEADING not in text:
        return [], (
            "The backlog no longer has a section headed "
            f"{_PM_GATE_HEADING.lstrip('# ')!r}, so the gate could not be read."
        )
    body = text.split(_PM_GATE_HEADING, 1)[1]
    for stop in (_PM_GATE_STOP, "\n## ", "\n### "):
        if stop in body:
            body = body.split(stop, 1)[0]

    has_empty_marker = any(
        line.strip().startswith(_PM_GATE_EMPTY_MARKER)
        for line in body.splitlines())
    items = _parse_numbered_items(body, source="pm-gate", notes=notes)

    if items and has_empty_marker:
        return [], (
            "The gate body declares itself EMPTY with "
            f"{_PM_GATE_EMPTY_MARKER!r} but still lists "
            f"{len(items)} numbered item(s), so its shape is inconsistent."
        )
    if not items:
        if has_empty_marker:
            return [], None
        return [], (
            "The gate heading is there but no numbered items could be read "
            "from it, so its shape has changed."
        )
    return items, None


# ---------------------------------------------------------------------------
# TWO closure vocabularies, deliberately
#
# There are two consumers of "does this item's own status say it is finished?",
# and they must NOT share a word list:
#
#   * `find_closed_items_not_marked_done` FAILS THE BUILD. Every word added to
#     its vocabulary is a new way for CI to go red on a backlog nobody has
#     edited, and the only fix available to a board change is a backlog edit —
#     a different job, on a file several sessions write to at once.
#   * the RENDERER only decides which section of the owner's page an item is
#     drawn in. Getting that wrong costs him attention; getting it wrong in
#     the direction of "finished work still looks live" is precisely the
#     complaint this widening answers ("déjà vu every day dealing with the
#     same stuff over and over").
#
# So the build check keeps the original, narrow list below, unchanged. The
# renderer reads the wider `_RENDER_*` lists underneath it. Measured against
# the live backlog on 2026-09-11, widening the BUILD list to match would newly
# fail CI, which is why it is not done here.
# ---------------------------------------------------------------------------

#: BUILD-CHECK ONLY. Words an item's own title uses to claim it is fully
#: closed. Deliberately excludes "WITHDRAWN" acting alone from nothing else —
#: see `_CLOSURE_EXEMPT_WORDS` below for the qualifiers that mean "not actually
#: closed yet" even in the presence of one of these. Do not widen this without
#: first checking `find_closed_items_not_marked_done` against the live backlog.
_CLOSURE_WORDS = ("FIXED", "DONE", "MERGED", "RESOLVED", "WITHDRAWN")

#: BUILD-CHECK ONLY. A closure word next to one of these means the item is
#: claiming progress, not a finished state — it must stay visibly open, not be
#: struck through.
_CLOSURE_EXEMPT_WORDS = ("PARTIALLY", "PARTIAL", "PENDING", "MOSTLY")

#: RENDERING. The closure vocabulary the owner's page reads. Every word beyond
#: the build list above was reported by the owner from his own backlog, where
#: it read as finished to him and as live work to the board:
#:
#:   SHIPPED     — items 14 and 36 ("SHIPPED 2026-09-04")
#:   REPLACED    — item 42 ("REPLACED 2026-09-10")
#:   REDESIGNED  — item 43 ("REDESIGNED 2026-09-11, owner call")
#:   CLOSED      — the natural partner of DONE; appears as "PARTIALLY CLOSED"
#:   LANDED / SUPERSEDED / DELIVERED / COMPLETE(D) — the same shape, added so
#:                 the next synonym somebody reaches for is already covered
#:
#: Matched on WORD BOUNDARIES (see `_closure_hit`), not as substrings: that is
#: what keeps "INCOMPLETE" from reading as "COMPLETE" and "MERGE ORDER
#: MATTERS" from reading as "MERGED".
_RENDER_CLOSURE_WORDS = _CLOSURE_WORDS + (
    "SHIPPED", "REPLACED", "REDESIGNED", "CLOSED", "LANDED", "SUPERSEDED",
    "DELIVERED", "COMPLETE", "COMPLETED",
)

#: RENDERING. A closure word next to one of these means the work itself is
#: done and only a REVIEW is owed — "FIXED, pending review" (items 33 and 34).
#: Collapsing that into plain "done" would be the same kind of false statement
#: the board exists to catch, so it gets its own section: not competing with
#: live work, not claimed as signed off either.
_RENDER_REVIEW_OWED_WORDS = (
    "PENDING REVIEW", "PENDING SIGN-OFF", "PENDING SIGNOFF", "PENDING OWNER",
    "AWAITING REVIEW", "AWAITING SIGN-OFF", "AWAITING SIGNOFF",
    "NEEDS REVIEW", "NEEDS REVIEWING", "UNREVIEWED", "PENDING",
)

#: RENDERING. A closure word next to one of these means real work is still
#: outstanding — "PARTIALLY FIXED, one gap open", "MOSTLY FIXED, one real
#: judgment call left". These items STAY in the running order, because moving
#: them out would hide live work, which is a worse failure than the one being
#: fixed. They are labelled instead, so a mostly-finished item does not read
#: as untouched.
_RENDER_PART_DONE_WORDS = ("PARTIALLY", "PARTIAL", "MOSTLY")


def _closure_hit(tail: str, words: tuple[str, ...]) -> bool:
    """Whether any of `words` appears in `tail` as a whole word.

    Substring matching is what makes a closure vocabulary dangerous as it
    grows: "INCOMPLETE" contains "COMPLETE", "MERGE ORDER" nearly contains
    "MERGED", and "UNRESOLVED" contains "RESOLVED". A word boundary on both
    sides costs nothing and removes the whole class of error.
    """
    return any(re.search(r"\b" + re.escape(w) + r"\b", tail) for w in words)


#: A closure word describing a DIFFERENT artifact — a pull request, another
#: item — cited inline in this item's own status tail. Item 60's real tail
#: is "OPEN, owner call, deferred 2026-09-13 while PR #343 (merged) repaired
#: the seat's honesty about the exceptions rather than replacing it.": the
#: item's own word is "OPEN", but "PR #343 (merged)" put "MERGED" — a real
#: closure word — right next to it, about a PR, not about item 60. Read
#: naively that put a live, owner-call item in the "finished, not struck
#: through" bucket, i.e. it looked FINISHED. Same failure family the
#: `status_tail` split already guards (`status_tail`'s own docstring: reading
#: the whole headline read "a fixed-interval poll" as a claim of being
#: fixed) — a closure vocabulary word means nothing until it is confirmed to
#: be ABOUT this item, not about something this item's own text happens to
#: mention. Matches "PR #343 (merged)", "#343 (merged)", "item 12 (fixed)" —
#: a numbered reference immediately followed by its own parenthetical status
#: — and only that shape, so an item's OWN status is never touched: nothing
#: here strips a bare "MERGED" or "FIXED" sitting on its own.
_CROSS_REF_STATUS_RE = re.compile(
    r"(?:\bPR\s*)?#\d+\s*\([^)]*\)"       # "PR #343 (merged)", "#343 (fixed)"
    r"|\bitems?\s+#?\d+\s*\([^)]*\)",     # "item 12 (fixed)"
    re.I)


def _strip_cross_references(tail: str) -> str:
    """`tail` with every parenthetical status about a DIFFERENT numbered
    artifact removed, so `_closure_hit` can never read one as a claim about
    the item whose own tail merely cites it. See `_CROSS_REF_STATUS_RE`."""
    return _CROSS_REF_STATUS_RE.sub(" ", tail)


def find_closed_items_not_marked_done(work_md: Path) -> list[str]:
    """Items whose own title claims full closure but were never marked
    `done` (the `~~title~~` convention `load_funnel_queue` reads).

    This exists because it already happened silently: eleven items in the
    real backlog said "FIXED", "DONE" or "MERGED" in their own title — one
    even said "FIXED AND MERGED" — while still rendering as open work on the
    owner's status board, because striking a title through has always been a
    remembered step, never a checked one. The board is supposed to be the one
    place that would rather say `unknown` than something false; an item
    contradicting its own title is exactly that kind of false statement, and
    it stood for days before anyone noticed. Returns a list of plain
    descriptions for CI to fail on, empty when there is nothing to flag.

    Why this reads the STRICT item shape while the renderer reads the wide one
    ------------------------------------------------------------------------
    This check fails the build. The renderer only draws a page. Widening this
    one to match the renderer would immediately fail CI on several live
    backlog items that genuinely do claim closure without being struck
    through — a real finding, but one that can only be resolved by EDITING
    THE BACKLOG, which is a different job from generating the board and must
    not be done as a side effect of it.

    So the board reports that same contradiction to the owner itself, on the
    page, in its own section (`QueueItem.claims_closure`). Nothing is hidden
    from him; what is deliberately deferred is turning it into a build
    failure. When the backlog's strike-throughs are brought up to date, this
    function should be switched to `_ITEM_OPEN_RE` and the title taken from
    the bold span only — taking it from the WHOLE line is what caused the
    historical false positive, where body prose containing the word "fixed"
    was read as the item's own closure claim.
    """
    if not work_md.exists():
        return []
    text = work_md.read_text()
    if _QUEUE_HEADING not in text:
        return []
    body = text.split(_QUEUE_HEADING, 1)[1]
    for stop in ("### Re-measure gate", "\n## ", "\n### "):
        if stop in body:
            body = body.split(stop, 1)[0]

    flagged = []
    for raw in body.splitlines():
        m = _QUEUE_ITEM_RE.match(raw.strip())
        if not m:
            continue
        rank, rest = m.group(1), m.group(2)
        if "~~" in raw:
            continue
        upper = rest.upper()
        claims_closed = any(w in upper for w in _CLOSURE_WORDS)
        actually_open = any(w in upper for w in _CLOSURE_EXEMPT_WORDS)
        if claims_closed and not actually_open:
            flagged.append(f"item {rank}: {rest[:100]}")
    return flagged


#: BOARD-HYGIENE CHECK ONLY (`find_near_duplicate_open_items`, item 140).
#:
#: Four duplicate items were filed on the same day by parallel agents, each
#: writing up the same finding in different words without reading the board
#: first. Nothing mechanically compared a new filing against what was
#: already open. This check is deliberately narrow: it compares the
#: TIDIED TITLE `load_funnel_queue` already extracts for each OPEN item
#: (`QueueItem.title` -- the text before the first em dash, classification
#: words and markdown already stripped), not the body prose. A title match
#: is a high-precision signal that the same headline was filed twice; a
#: prose/topic match is not -- two items can legitimately discuss the same
#: area of the desk (the same file, the same seat) without being the same
#: finding, and flagging that would be noise the owner would learn to
#: ignore. Two thresholds, both conservative on purpose:
#:
#:   * IDENTICAL titles once normalized (lowercased, punctuation and
#:     whitespace collapsed) -- the same words, filed twice.
#:   * NEAR-identical titles above a tight similarity ratio, and only once
#:     both titles are long enough that a short generic title cannot match
#:     by coincidence (`_DUP_TITLE_MIN_LEN`).
#:
#: This does not catch a genuine paraphrase that changes enough of the
#: wording -- the same limitation `find_closed_items_not_marked_done` notes
#: for its own vocabulary match. That is the deliberate trade: a check that
#: never flags a legitimate pair of distinct items, at the cost of missing
#: a rarer, better-disguised duplicate.
_DUP_TITLE_RATIO = 0.90
_DUP_TITLE_MIN_LEN = 12


def _normalize_title_for_dup_check(title: str) -> str:
    """Lowercase, punctuation and whitespace collapsed out, for comparing
    two backlog titles as the same words rather than the same characters."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


#: The board already writes this override by hand, in two phrasings seen on
#: the live backlog: item 138's body says "Item 118 is a NEAR-NEIGHBOUR and
#: does NOT cover this", item 183's says "Distinct from item 138, which
#: tracks the order-PRICE buffers...". Neither pair actually trips the title
#: check today (their titles are dissimilar enough on their own), but the
#: title wording is free to change, and a future near-neighbour pair COULD
#: land above `_DUP_TITLE_RATIO` by coincidence. When that happens the
#: filing author needs a way to say "I know, and here is the other item" —
#: without renaming a title just to dodge a mechanical check, which is its
#: own kind of drift. The override must NAME the item it claims distinctness
#: from; a bare "not a duplicate" with no number is not accepted, because
#: that would let any flagged pair opt out with no accountable claim behind
#: it. Matched against `raw_body` (markdown already stripped by
#: `_parse_numbered_items`), case-insensitively, and read from BOTH sides of
#: a flagged pair — either item may carry the marker naming the other.
_NEAR_NEIGHBOUR_RE = re.compile(
    r"item\s+(\d+)\s+is\s+a\s+near-neighbour", re.IGNORECASE)
_DISTINCT_FROM_RE = re.compile(
    r"distinct\s+from\s+item\s+(\d+)", re.IGNORECASE)


def _explicit_distinct_targets(raw_body: str) -> set[int]:
    """Item numbers this item's own body explicitly declares itself distinct
    from — see the note above `_NEAR_NEIGHBOUR_RE`. Empty when the body
    carries neither marker."""
    targets = {int(n) for n in _NEAR_NEIGHBOUR_RE.findall(raw_body)}
    targets |= {int(n) for n in _DISTINCT_FROM_RE.findall(raw_body)}
    return targets


def find_near_duplicate_open_items(work_md: Path) -> list[str]:
    """OPEN funnel-queue items whose own tidied title is the same finding
    filed twice. See the module note above `_DUP_TITLE_RATIO` for why this
    reads only the title, not the body, and why the two thresholds are set
    where they are. Returns plain-English descriptions for CI to fail on,
    empty when nothing looks duplicated. A file that cannot be parsed at all
    is a job for the existing `load_funnel_queue` problem-reporting path,
    not this check, so an unparseable file reports nothing here rather than
    raising.

    A pair that would otherwise be flagged is let through when either item's
    own body NAMES the other as a deliberate near-neighbour (see
    `_explicit_distinct_targets`) — the mechanical equivalent of the board's
    existing "item N is a NEAR-NEIGHBOUR and does NOT cover this" / "distinct
    from item N" prose, so that convention keeps working instead of being
    overridden by a title coincidence.
    """
    items, problem = load_funnel_queue(work_md)
    if problem:
        return []
    open_items = [item for item in items if item.state == "open"]
    normalized = [
        (item, _normalize_title_for_dup_check(item.title))
        for item in open_items
    ]
    flagged: list[str] = []
    for idx, (item_a, norm_a) in enumerate(normalized):
        if not norm_a:
            continue
        for item_b, norm_b in normalized[idx + 1:]:
            if not norm_b:
                continue
            if norm_a == norm_b:
                kind = "identical"
            elif (min(len(norm_a), len(norm_b)) >= _DUP_TITLE_MIN_LEN
                  and difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
                  >= _DUP_TITLE_RATIO):
                kind = "near-identical"
            else:
                continue
            if (item_b.rank in _explicit_distinct_targets(item_a.raw_body)
                    or item_a.rank in _explicit_distinct_targets(item_b.raw_body)):
                continue
            flagged.append(
                f"item {item_a.rank} and item {item_b.rank} look like the "
                f"same finding filed twice ({kind} title): "
                f"{item_a.title!r} / {item_b.title!r}"
            )
    return flagged


#: BOARD-HYGIENE CHECK ONLY (`find_finished_items_still_on_board`). Reuses
#: the renderer's own wide closure/no-action vocabularies rather than
#: inventing a third one that could disagree with them:
#:
#:   `_RENDER_CLOSURE_WORDS` -- FIXED / DONE / MERGED / RESOLVED / WITHDRAWN /
#:       SHIPPED / REPLACED / REDESIGNED / CLOSED / LANDED / SUPERSEDED /
#:       DELIVERED / COMPLETE(D) -- the item's own status says the work landed.
#:   `_NO_ACTION_WORDS` -- WORKING AS INTENDED / NOT A DEFECT -- the item was
#:       investigated and needs no fix; still finished work, just never
#:       "shipped" anything.
#:   "SETTLED" -- added here because items 55/56/63/64/65/70/74 use it as
#:       their own word for "the research question now has an answer", a
#:       shape `_RENDER_CLOSURE_WORDS` does not otherwise cover.
#:
#: `QueueItem.claims_closure` and `QueueItem.no_action` already read these on
#: WORD BOUNDARIES, off `status_tail` only (never the body), with
#: `_CLOSURE_NEGATIONS` and cross-reference stripping applied first -- see
#: their docstrings. This constant only widens that vocabulary by "SETTLED";
#: it does not re-implement the reading.
_BOARD_FINISHED_WORDS = _RENDER_CLOSURE_WORDS + _NO_ACTION_WORDS + ("SETTLED",)

#: Any of these appearing in an item's own `status_tail` means the item is
#: still, in its own words, open -- and must suppress a finished verdict even
#: when one of `_BOARD_FINISHED_WORDS` also appears in the same tail.
#:
#: Found by running this check against the real backlog: item 80's tail is
#: "every SHIPPED stop must trace to a computed level, the signal bar, or the
#: volatility band. OPEN; the REFUSAL path is contested, filed 2026-09-17." --
#: "SHIPPED" there describes the KIND of stop the rule is about (a stop that
#: has already gone out to the broker), not this item's own state; the
#: item's actual, self-declared state is the literal word "OPEN" two clauses
#: later. Reusing the renderer's own `_RENDER_PART_DONE_WORDS` /
#: `_RENDER_REVIEW_OWED_WORDS` / `_CLOSURE_EXEMPT_WORDS` / `_PAUSED_WORDS`
#: covers "PARTIALLY", "PENDING REVIEW", "DEFERRED" and friends the same way
#: `claims_closure`/`no_action` already exempt them; "OPEN" and "STILL OPEN"
#: are added because the backlog's own convention opens a live item's status
#: with exactly that word ("OPEN, filed 2026-09-14", "OPEN; the REFUSAL path
#: is contested"), and no genuinely finished item in the real backlog uses it
#: to describe itself.
_BOARD_STILL_OPEN_WORDS = (
    _RENDER_PART_DONE_WORDS + _RENDER_REVIEW_OWED_WORDS
    + _CLOSURE_EXEMPT_WORDS + _PAUSED_WORDS
    + ("OPEN", "STILL OPEN")
)

#: The SECOND, independent way `find_finished_items_still_on_board` decides
#: an item is finished: its own `DONE WHEN:` checkboxes, not its headline
#: prose. Found on the real board 2026-09-26: ten items were fully
#: ticked under their own `DONE WHEN` block while their headline still read
#: as open work, because nothing ever read the boxes -- the exact failure
#: this whole check exists to prevent, just reached through the door the
#: headline-only reading left open.
#:
#: `docs/WORK.md` uses exactly two marks in practice -- `- [ ]` (open) and
#: `- [x]` (checked), always lower-case `x` [checked against the current
#: file, 2026-09-26: 26 open boxes, 23 checked, no `[X]`, no other bullet
#: character, no emoji tick]. Upper-case `[X]` is still accepted here since
#: nothing stops an author typing it later; it would be a stricter parser,
#: not a looser one, to reject a mark nobody has used only because nobody
#: has used it yet.
_DONE_WHEN_CHECKBOX_RE = re.compile(r"\[( |x|X)\]")

#: The literal marker an item's own body may write to say its `DONE WHEN`
#: block can be fully ticked and the item STILL cannot close, because
#: closing it needs an event the desk cannot manufacture -- a real fill, a
#: real provider fault, or real capital moving -- not more work. Item 86 is
#: the one real example on the board today ("item stays OPEN until a live
#: attempt proves it"), but that is one sentence written for that one item,
#: not a convention the rest of the board repeats: nothing else on the
#: board uses "OPEN until a live" or any close variant of it [checked
#: against the current file, 2026-09-26], so there is no existing wording
#: to key off. This marker is the mechanical replacement going forward --
#: an item that needs the exemption should say so in exactly these words --
#: rather than teaching the checker to special-case item 86's one sentence
#: as if it were a pattern. It does not retroactively apply to item 86
#: (its box is unchecked anyway, so it never reaches the checkbox check;
#: see the module note above `_BOARD_FINISHED_WORDS` for its headline-side
#: handling, unchanged here).
_LIVE_EVENT_BLOCKED_RE = re.compile(r"BLOCKED ON A LIVE EVENT", re.IGNORECASE)


def _done_when_checkbox_marks(raw_body: str) -> list[str]:
    """Every checkbox mark inside this item's own `DONE WHEN:` block, in the
    order written, empty when the item carries no such block at all.

    `raw_body` is one item's OWN body (already isolated per-item by
    `_parse_numbered_items` before this ever runs), markdown-stripped and
    joined onto one line, so a `DONE WHEN` marker found inside it can only
    belong to this item, never a neighbour's. Only text AFTER that marker is
    scanned, so an ordinary body sentence written before it can contain a
    literal `[` (a citation, a code fragment) without being mistaken for a
    criterion.
    """
    idx = raw_body.find("DONE WHEN")
    if idx == -1:
        return []
    return _DONE_WHEN_CHECKBOX_RE.findall(raw_body[idx:])


def _all_done_when_boxes_checked(raw_body: str) -> bool:
    """True only when the item HAS a `DONE WHEN` block AND every box in it
    is ticked.

    An item with no block at all is never "all checked" here -- that is a
    separate, already-known gap (22 open items on the real board carry no
    `DONE WHEN` block at all [checked 2026-09-26]) and solving it is not
    this function's job; it can only be judged finished by the headline
    half of `find_finished_items_still_on_board`, same as before this
    function existed.
    """
    marks = _done_when_checkbox_marks(raw_body)
    return bool(marks) and all(m.lower() == "x" for m in marks)


def find_finished_items_still_on_board(
        work_md: Path, board_notes: Path) -> list[str]:
    """Board items that declare themselves finished -- in their own status
    text OR in their own `DONE WHEN` checkboxes -- but are still sitting in
    `docs/WORK.md`.

    `docs/WORK.md` opens with the owner's own rule: it holds only open work.
    Finished work belongs in `docs/INCIDENT_HISTORY.md`, with its
    `## item N` block deleted from `docs/BOARD_NOTES.md` and its number
    added to the retired line. Nothing previously checked the OUTFLOW half
    of that rule -- `test_no_board_item_disappears_without_being_retired` and
    `test_work_md_stays_under_a_hundred_thousand_bytes` only stop the file
    from growing past its cap without being pruned; neither one notices a
    single item that has quietly finished and simply never been moved.

    Reuses `load_funnel_queue` / `load_pm_gate` -- the same `QueueItem`
    parser and the same `status_tail` (only the status half of the headline,
    cross-references to OTHER items' PRs stripped, negations honoured) the
    render path already relies on -- rather than a second parser that could
    disagree with it. An item is flagged when EITHER of two independent
    readings says it is finished:

      * HEADLINE: its `status_tail` hits one of `_BOARD_FINISHED_WORDS`, and
        hits none of `_BOARD_STILL_OPEN_WORDS` -- which is what keeps a
        partially-fixed item with a listed follow-on, a deferred owner
        decision, or an item whose own words are "OPEN" from firing. This
        is the original reading and is unchanged.
      * CHECKBOXES (added 2026-09-26): the item carries a `DONE WHEN` block
        and every box in it is ticked (`_all_done_when_boxes_checked`),
        regardless of what the headline prose says -- this is the half that
        was missing, and the reason ten items sat open with every one of
        their own done-criteria met before this change. An item with no
        `DONE WHEN` block at all cannot trip this half (see
        `_all_done_when_boxes_checked`'s docstring); an item whose body
        contains the literal marker `BLOCKED ON A LIVE EVENT`
        (`_LIVE_EVENT_BLOCKED_RE`) is exempted from it too, because a fully
        ticked block does not mean the item can close when what remains is
        an event the desk cannot manufacture -- see the note above
        `_LIVE_EVENT_BLOCKED_RE`.

    `board_notes` is `docs/BOARD_NOTES.md`'s path; it is loaded only so the
    lookup-by-`ref` prose attaches the same way the renderer attaches it --
    this check does not read the notes' own text, since an item's *headline*
    is where the backlog records its status, and the notes file is the
    owner-facing writeup, not a second place a status could be declared.

    Returns plain-English strings, empty when nothing is flagged. Each
    string names the item, says WHICH reading tripped it, and spells out
    every step of the retirement procedure, because whoever trips this will
    not otherwise know it.
    """
    notes = load_board_notes(board_notes)
    flagged: list[str] = []
    for items, _problem in (load_funnel_queue(work_md, notes=notes),
                             load_pm_gate(work_md, notes=notes)):
        for item in items:
            if item.done:
                continue
            tail = item.status_tail
            headline_finished = False
            if not any(w in tail for w in _CLOSURE_NEGATIONS):
                scan = _strip_cross_references(tail)
                if (_closure_hit(scan, _BOARD_FINISHED_WORDS)
                        and not _closure_hit(scan, _BOARD_STILL_OPEN_WORDS)):
                    headline_finished = True
            checkbox_finished = (
                _all_done_when_boxes_checked(item.raw_body)
                and not _LIVE_EVENT_BLOCKED_RE.search(item.headline)
                and not _LIVE_EVENT_BLOCKED_RE.search(item.raw_body)
            )
            if not headline_finished and not checkbox_finished:
                continue
            if headline_finished and checkbox_finished:
                reason = (
                    "declares itself finished "
                    f"({item.headline[:120]!r}) AND every box in its own "
                    "DONE WHEN block is ticked")
            elif checkbox_finished:
                reason = (
                    "every box in its own DONE WHEN block is ticked, even "
                    "though its headline status does not say so "
                    f"({item.headline[:120]!r})")
            else:
                reason = f"declares itself finished ({item.headline[:120]!r})"
            flagged.append(
                f"{item.ref} {reason} but is still on the board. "
                "Write it up in docs/INCIDENT_HISTORY.md (newest first, "
                "opening with one plain-language line), then delete its "
                "docs/WORK.md block AND its matching '## " + item.ref +
                "' block in docs/BOARD_NOTES.md, and add its number to "
                "the retired line at the end of the relevant list in "
                "docs/WORK.md."
            )
    return flagged


#: `- [ ] DECIDE BY 2026-09-16 — question` — the same shape
#: `test_no_pending_decision_is_overdue` enforces, deliberately, so the board
#: and the build are reading one format and cannot disagree about it.
#: PROVISIONAL — no owner ruling states this fraction; it is a judgement
#: call, made here rather than left as an unstated assumption in a test.
#:
#: `work_md_growth_budget` spends this share of whatever headroom remains
#: below the 100,000-byte cap (`test_work_md_stays_under_a_hundred_thousand_
#: bytes` owns that number; this module never re-types it, see
#: `WORK_MD_GROWTH_CAP_BYTES` below) on ONE change. That makes the allowance
#: shrink automatically as the file fills — half the remaining room at 30%
#: full is enormous (unrestricted in practice), half the remaining room at
#: 95% full is a couple thousand bytes (enough for a short item, not enough
#: to dump an afternoon's findings without pruning first) — without pinning
#: separate numbers at arbitrary bands (60%, 85%, 95%, ...) that would each
#: need their own justification. 0.5 was picked only because it is the
#: simplest value that produces that shape; it is not measured from
#: anything. Owner ruling 2026-09-17: the old rule (a change may never leave
#: docs/WORK.md larger than it found it) was replacing a housekeeping
#: problem with a recording-defects problem, and had to go — see
#: `test_work_md_growth_is_bounded_and_shrinks_as_the_cap_fills`.
WORK_MD_GROWTH_SHARE = 0.5

#: Kept EQUAL to the cap `test_work_md_stays_under_a_hundred_thousand_bytes`
#: enforces — that test owns the number, this is a second, independent
#: place it is used, and `test_work_md_growth_cap_matches_the_byte_ceiling`
#: reads the cap test's own source (the same way
#: `scripts/check_board_hygiene.py:read_cap_bytes` already does) and fails
#: if the two ever disagree, so this cannot drift silently if the ceiling
#: test is ever edited.
WORK_MD_GROWTH_CAP_BYTES = 100_000


def work_md_growth_budget(before_size: int,
                           cap: int = WORK_MD_GROWTH_CAP_BYTES,
                           share: float = WORK_MD_GROWTH_SHARE) -> int:
    """How many bytes `docs/WORK.md` may grow in a single change, given its
    size before that change.

    Replaces the 2026-09-14 rule that a change could never leave the file
    larger than it found it. That rule was written for a real problem —
    finished work piling up unpruned — but had no escape hatch, so it also
    blocked recording a brand-new, genuine defect on a night when far more
    defects were found than were closed, while the file sat at ~30,000 of
    its 100,000-byte cap: comfortably under it, with nothing to prune.
    Owner's ruling (2026-09-17, in substance): the rule was badly written;
    he wants housekeeping enforced, not recording blocked. This function is
    the mechanical replacement.

    Returns a budget that SHRINKS as the file fills, rather than a flat
    allowance: `share` of whatever headroom remains below `cap`. Near-empty,
    the budget is effectively unrestricted for a normal edit; near the cap,
    it is small enough that anything but a short item forces pruning first.
    The 100,000-byte hard cap itself is untouched and still the final
    backstop (`test_work_md_stays_under_a_hundred_thousand_bytes`) — this
    only shapes how a single change may approach it. Never negative: a
    `before_size` at or past `cap` returns 0.

    This does not, by itself, make housekeeping happen — it only bounds how
    much can be added without it. The mechanical push to actually retire
    finished work is `find_finished_items_still_on_board` /
    `find_closed_items_not_marked_done`, run unconditionally against the
    real board on every change (`test_the_real_backlog_has_no_finished_
    item_still_on_the_board`, `test_the_real_backlog_has_no_item_
    contradicting_its_own_title`) — not gated on growth, so a change that
    adds nothing still fails if it leaves a self-declared-finished item
    sitting on the board.
    """
    headroom = max(cap - before_size, 0)
    return int(headroom * share)


_DECISION_RE = re.compile(r"^- \[ \] DECIDE BY (\d{4})-(\d{2})-(\d{2}) [-\u2014] (.+)$")


@dataclass
class PendingDecision:
    """A decision waiting on the owner, with how long is left."""

    due: dt.date
    question: str
    days_left: int
    prose: Prose = field(default_factory=Prose)
    #: Tracking references literally present in this decision's own text.
    refs: tuple[str, ...] = ()
    #: The decision's own indented body, markdown stripped — shown only inside
    #: the marked container in `_render_prose`, same as a backlog item's.
    raw_body: str = ""

    @property
    def overdue(self) -> bool:
        return self.days_left < 0

    @property
    def ref(self) -> str:
        """The identifier the owner quotes back for a decision.

        A pending decision has no number in the backlog — its shape is
        `- [ ] DECIDE BY <date> — question`, so the DATE is the only stable
        handle it has. Spelled out in full rather than abbreviated, so
        "the decision due 2026-09-16" names exactly one line in the file.
        """
        return f"decision due {self.due.isoformat()}"


def load_pending_decisions(work_md: Path, today: dt.date | None = None,
                            notes: dict[str, Prose] | None = None
                            ) -> list[PendingDecision]:
    """Decisions the owner still owes an answer on, soonest first.

    `notes` is `docs/BOARD_NOTES.md`, already parsed by `load_board_notes`.
    A decision has no number of its own, so it is keyed by its due date —
    `"decision due 2026-09-16"`, the same string `PendingDecision.ref`
    renders — which is looked up here so a decision can carry its own
    explanation, example and recommendation instead of the owner having to
    reconstruct the question from engineering notes. A decision with no
    matching note is shown as having none — this never picks one for him.
    """
    notes = notes or {}
    if not work_md.exists():
        return []
    today = today or dt.date.today()
    lines = work_md.read_text().splitlines()
    out: list[PendingDecision] = []
    for idx, line in enumerate(lines):
        m = _DECISION_RE.match(line.strip())
        if not m:
            continue
        y, mo, d, question = m.groups()
        try:
            due = dt.date(int(y), int(mo), int(d))
        except ValueError:
            continue  # the build test already fails loudly on a bad date
        # The body of a decision is the indented block under it. It ends at
        # the next unindented line — another list item, a heading, or the
        # next paragraph — which is the same rule markdown itself uses.
        body: list[str] = []
        j = idx + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.strip() and not nxt.startswith((" ", "\t")):
                break
            body.append(nxt)
            j += 1

        # The question itself usually wraps. Keep pulling wrapped lines into
        # it until the paragraph ends, a bold sub-note starts (a bold run at
        # the start of a line is a separate note, not the tail of the
        # question), or a plain-language label starts — otherwise a
        # multi-line question renders as a sentence fragment, which is how
        # "What should the macro freshness bar be, given ...?" reached his
        # phone as the four words "What should the macro".
        text = [question.strip()]
        for nxt in body:
            s = nxt.strip()
            if not s or s.startswith("**") or _PROSE_LINE_RE.match(nxt):
                break
            text.append(s)
        out.append(PendingDecision(
            due, _strip_markdown(" ".join(text)), (due - today).days,
            notes.get(f"decision due {due.isoformat()}", Prose()),
            refs=extract_refs(question + " " + " ".join(body)),
            raw_body=_strip_markdown(" ".join(body)),
        ))
    return sorted(out, key=lambda p: p.due)


def load_phases(manifest: Path, cfg: dict) -> list[PhaseView]:
    raw = yaml.safe_load(manifest.read_text())
    entries = raw["phases"] if isinstance(raw, dict) and "phases" in raw else raw
    views: list[PhaseView] = []
    for e in entries:
        v = PhaseView(
            id=str(e.get("id", "?")),
            title=str(e.get("title", "?")),
            summary=e.get("plain_summary", ""),
            recorded=str(e.get("status", "?")),
            confidence=str(e.get("confidence", "?")),
        )
        for rule in e.get("evidence", []) or []:
            v.results.append(check_rule(rule, cfg))
        views.append(v)
    return views


# --------------------------------------------------------------------------
# live state
# --------------------------------------------------------------------------

def live_state() -> dict[str, Any]:
    s: dict[str, Any] = {}

    _run(["git", "fetch", "origin", "--quiet"], REPO_ROOT)
    rc, main_sha = _run(["git", "rev-parse", "origin/main"], REPO_ROOT)
    s["main_sha"] = main_sha[:9] if rc == 0 else None

    ok, box_sha = _prod_git("rev-parse", "HEAD")
    # The full, untruncated SHA is what gets stamped into the page for the
    # serve-time freshness check (src/api/server.py) to compare against a
    # freshly-read live SHA — a 9-char prefix is fine for a human footer but
    # is a needless (if tiny) collision risk for a machine equality check.
    s["box_sha_full"] = box_sha.strip() if ok else None
    s["box_sha"] = s["box_sha_full"][:9] if s["box_sha_full"] else None

    if s["main_sha"] and s["box_sha"]:
        rc, out = _run(
            ["git", "log", "--oneline", "--merges", f"{box_sha.strip()}..origin/main"],
            REPO_ROOT,
        )
        merges = [l for l in out.splitlines() if "Merge pull request" in l]
        s["undeployed_merges"] = len(merges)
        s["in_sync"] = box_sha.strip().startswith(main_sha.strip()[:9])
    else:
        s["undeployed_merges"] = None
        s["in_sync"] = None

    ok, dirty = _prod_git("status", "--porcelain")
    if ok:
        tracked = [l for l in dirty.splitlines() if l and not l.startswith("??")]
        s["box_uncommitted"] = len(tracked)
    else:
        s["box_uncommitted"] = None

    # --- the ledger, read strictly read-only -------------------------------
    s.update(_read_ledger())
    return s


def _read_ledger() -> dict[str, Any]:
    """Read today's spend and the circuit latch from the production database.

    Opened through a read-only URI so this can never mutate the live ledger,
    and copied first if we cannot open it in place.
    """
    out: dict[str, Any] = {
        "spend_today": None, "day": None, "circuit": None,
        "sessions_today": None, "costs_exact": None,
    }
    db = PROD_CHECKOUT / "data" / "quant_agent.db"
    tmp: Path | None = None
    try:
        if os.access(db, os.R_OK):
            src = db
        else:
            tmp = Path("/tmp") / f"board-snapshot-{os.getpid()}.db"
            rc, _ = _run(["bash", "-c", f"sudo -n cat {db} > {tmp}"], timeout=60)
            if rc != 0 or not tmp.exists():
                return out
            src = tmp
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        today = datetime.now(ET).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT day, incremental_cost_usd AS spend, costs_exact "
            "FROM llm_budget_days WHERE day=?", (today,),
        ).fetchone()
        if row:
            out["day"] = row["day"]
            out["spend_today"] = float(row["spend"] or 0)
            out["costs_exact"] = bool(row["costs_exact"])
        st = conn.execute(
            "SELECT suspended, trigger_code FROM llm_circuit_state WHERE singleton=1"
        ).fetchone()
        if st:
            out["circuit"] = ("halted: " + str(st["trigger_code"] or "unknown trigger")
                              if int(st["suspended"] or 0) else "clear")
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM llm_budget_sessions WHERE day=?", (today,)
        ).fetchone()
        out["sessions_today"] = int(n["n"]) if n else None
        # A day with no sessions has no budget row, which is not the same thing
        # as a budget that could not be read. Reporting "unknown" there is a
        # lie of omission: the board sat next to "0 sessions ran today" and
        # still claimed it could not tell what had been spent. If the ledger
        # opened and nothing ran, the answer is exactly zero.
        if out["spend_today"] is None and out["sessions_today"] == 0:
            out["spend_today"] = 0.0
            out["costs_exact"] = True
            out["day"] = today
        conn.close()
    except Exception as exc:  # noqa: BLE001 - unknown beats a wrong number
        out["circuit"] = None
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if tmp and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return out


def read_settings() -> dict:
    """Prefer the box's live settings; fall back to the repo's."""
    ok, text = _read_prod("config/settings.yaml")
    if not ok:
        try:
            text = (REPO_ROOT / "config" / "settings.yaml").read_text()
        except OSError:
            return {}
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return '<span class="unk">unknown</span>'
    return f"{_esc(value)}{unit}"


def _summary_text(summary: Any) -> str:
    """Flatten a `plain_summary` (either shape) to plain text for the jargon
    detector, which only ever reasons about a string. The legacy shape is
    already a string; the structured shape concatenates `verdict`, every
    `points` entry, and `outstanding` (if present) into one string — the
    detector runs on the words actually shown to him, in either shape."""
    if not isinstance(summary, dict):
        return str(summary or "")
    parts = [str(summary.get("verdict", ""))]
    parts.extend(str(pt) for pt in (summary.get("points") or []))
    outstanding = summary.get("outstanding")
    if outstanding:
        parts.append(str(outstanding))
    return " ".join(p for p in parts if p)


def _render_summary(summary: Any) -> str:
    """Render a phase's `plain_summary`, in either shape the manifest may use.

    Legacy shape: a plain string, rendered exactly as it always has been (one
    escaped italic block) — entries that have not been migrated must keep
    looking the same.

    Structured shape: a dict with `verdict` (one line, rendered prominently),
    `points` (a real bulleted list), and an optional `outstanding` line
    (rendered visually distinct, in the warning colour). Every field is
    escaped individually — this function must never emit a manifest value as
    raw HTML.
    """
    if not isinstance(summary, dict):
        return f"<i>{_esc(summary)}</i>"

    parts = [f'<p class="ps-verdict">{_esc(summary.get("verdict", ""))}</p>']

    points = summary.get("points") or []
    if points:
        items = "".join(f"<li>{_esc(pt)}</li>" for pt in points)
        parts.append(f'<ul class="ps-points">{items}</ul>')

    outstanding = summary.get("outstanding")
    if outstanding:
        parts.append(
            f'<p class="ps-outstanding"><span class="ps-label">Still outstanding'
            f'&nbsp;&mdash;</span> {_esc(outstanding)}</p>'
        )

    return f'<div class="ps">{"".join(parts)}</div>'


#: Every verdict is spelled out in words. There is no colour-only version of
#: any of these, by requirement.
VERDICT_PILL = {
    "CONFIRMED": ("chip-quiet", "still proves out"),
    "CONTRADICTED": ("chip-strong", "proof no longer holds"),
    "UNVERIFIED": ("chip-gap", "cannot be checked by machine"),
}

#: The one recorded status that means "nothing left to do, and the board's
#: own re-check agrees." Everything else -- PARTIAL, NOT STARTED, OPEN,
#: OVERTAKEN, or a status the board can't confirm -- stays in the visible,
#: uncollapsed list. This governs presentation only; it reads fields the
#: renderer already computes and changes no evidence rule.
_SETTLED_STATUSES = {"done and live"}


def _is_settled(p: PhaseView) -> bool:
    """Fully verified AND finished: the manifest claims the canonical done
    state, and the board's live re-check still confirms it. A phase that is
    merely CONFIRMED but recorded as PARTIAL/OPEN/etc. still has open work
    and must stay visible, not tucked into the collapsed section."""
    return p.verdict == "CONFIRMED" and p.recorded.strip().lower() in _SETTLED_STATUSES


def _row(p: PhaseView) -> str:
    cls, label = VERDICT_PILL[p.verdict]
    detail = f"{p.passed} of {p.checkable} checks pass"
    if p.unknown:
        detail += f" &middot; {p.unknown} need a human"
    # The flag renders ABOVE the summary, never instead of it — the original
    # text still shows underneath even when it's flagged, because an
    # unreadable description is more useful to him than no description.
    jargon = ""
    if p.summary_flagged:
        jargon = (
            '<div class="jargon-flag">Not written for you &mdash; this reads '
            'like a note for a developer. Needs a plain-English rewrite.'
            f'<span class="jargon-why">{_esc(p.summary_flag_reason)}</span></div>'
        )
    return (
        f'<tr><td><span class="chip {cls}">{_esc(label)}</span></td>'
        f'<td><b>{_ref_tag("stage " + p.id)}{_esc(_strip_markdown(p.title))}</b>'
        f'{jargon}'
        f'{_render_summary(p.summary)}'
        f'<u>recorded as &ldquo;{_esc(p.recorded.lower())}&rdquo; &middot; {detail}</u></td></tr>'
    )


# --------------------------------------------------------------------------
# Rendering the plain-language blocks
#
# Status is ALWAYS carried by a word. Nothing on this page may depend on hue
# to be understood: every marker is a text label, and the shapes that carry
# emphasis are border weight, position and a glyph, never a colour swapped
# for another colour of similar lightness.
# --------------------------------------------------------------------------

#: What an item is missing, phrased as the gap it is rather than as an error.
#: The board says "nobody has written this yet" and stops there. It does not
#: write it, and it does not paper over it.
_NO_PLAIN = ("Nobody has written the plain-English version of this one yet. "
             "The backlog's own engineering notes are below, marked as such "
             "&mdash; they are not a substitute for it, and nothing has been "
             "invented to stand in.")
_NO_EXAMPLE = ("No real-world example yet. Without one this item is hard to "
               "judge &mdash; it needs a concrete case adding to the backlog.")


def _prose_block(label: str, text: str, cls: str) -> str:
    return (f'<div class="pb {cls}"><span class="pb-k">{label}</span>'
            f'<p>{_esc(text)}</p></div>')


#: How much of an item's raw engineering body is worth showing inside the
#: contained block before it stops being a glance and becomes a document. Past
#: this it is cut and says so — the backlog has the rest, and the container is
#: there to be secondary, not to reproduce the file.
_RAW_SOURCE_CHARS = 600


def _raw_source_block(raw: str) -> str:
    """The item's own engineering notes, contained and clearly labelled.

    This is the answer to the top card reading as a structural mess. When
    nobody has written the plain-English version, the honest thing to show is
    (a) that fact, and (b) the source text that DOES exist — but the source
    text must not be laid out as if it were the explanation. So it goes in a
    collapsed container, under a label saying who it was written for, in
    smaller secondary type.

    Nothing in here is rewritten, summarised or paraphrased. Markdown marks
    are stripped and a long body is cut with the cut declared; that is all.
    Paraphrasing engineering notes into friendly prose would be inventing an
    explanation, which is the staleness this board exists to prevent wearing a
    friendlier face.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    cut = len(text) > _RAW_SOURCE_CHARS
    if cut:
        text = text[:_RAW_SOURCE_CHARS].rsplit(" ", 1)[0] + "…"
    tail = ('<span class="raw-cut">Shortened here. The rest is in the backlog, '
            'unchanged.</span>' if cut else "")
    return ('<details class="raw"><summary>Engineering notes from the backlog '
            '&mdash; not an explanation</summary>'
            f'<div class="raw-body"><p>{_esc(text)}</p>{tail}</div></details>')


def _render_prose(prose: Prose, *, want_example: bool = True,
                  want_recommendation: bool = False,
                  raw_source: str = "") -> str:
    """One item's owner-facing prose, with its gaps stated rather than hidden.

    `want_example` / `want_recommendation` say whether the ABSENCE of that
    block is itself worth reporting. An example is always worth reporting as
    missing — the owner has said repeatedly that an item without one is no use
    to him. A recommendation is only expected where a ruling is actually
    being asked for, so it is only reported missing there.
    """
    parts: list[str] = []
    # Nothing written at all is one statement, not three. Repeating the same
    # three "nobody wrote this" paragraphs down a list of thirty items trains
    # him to scroll past the marker, which is the exact failure that got
    # jargon-blocking rejected in the first place.
    #
    # The un-written state is a two-part shape, and it is the same shape on
    # every card on the page — the prominent top one included, which is where
    # it was reading as a mess:
    #
    #   1. one short, plain sentence saying nobody has written this yet;
    #   2. the raw engineering notes, contained and labelled as such.
    #
    # Never an invented explanation, and never the raw notes presented as
    # though they were one.
    if not prose.has_any:
        tail = (' There is no recommendation either, so there is nothing here '
                'to agree or disagree with &mdash; ask for one before ruling.'
                if want_recommendation else '')
        return ('<div class="pb pb-gap"><span class="pb-k">No plain-English '
                'version yet</span><p>Nobody has written this one up for you '
                'yet &mdash; no explanation and no example &mdash; so there is '
                'nothing on this card that was written for you. It is listed '
                'rather than hidden, and nothing has been invented to fill the '
                f'gap.{tail}</p></div>'
                + _raw_source_block(raw_source))

    if prose.plain:
        parts.append(_prose_block("In plain language", prose.plain, "pb-plain"))
    else:
        parts.append(f'<div class="pb pb-gap"><span class="pb-k">No '
                     f'plain-English version yet</span><p>{_NO_PLAIN}</p></div>')
        parts.append(_raw_source_block(raw_source))

    if prose.example:
        parts.append(_prose_block("For example", prose.example, "pb-eg"))
    elif want_example:
        parts.append(f'<div class="pb pb-gap"><span class="pb-k">No example '
                     f'yet</span><p>{_NO_EXAMPLE}</p></div>')

    if prose.decision:
        parts.append(_prose_block("What you have to decide", prose.decision,
                                  "pb-dec"))
    if prose.recommendation:
        parts.append(_prose_block("Our recommendation", prose.recommendation,
                                  "pb-rec"))
    elif want_recommendation:
        parts.append('<div class="pb pb-gap"><span class="pb-k">No '
                     'recommendation yet</span><p>Nobody has written a '
                     'recommendation for this one, so there is nothing here to '
                     'agree or disagree with. Ask for one before ruling.</p>'
                     '</div>')

    markers = prose.jargon_markers
    if markers:
        parts.append(
            '<div class="pb pb-jargon"><span class="pb-k">Written for a '
            'developer</span><p>The words above still say what they say, but '
            'they contain ' + _esc(", ".join(markers)) + ' &mdash; so this one '
            'needs rewriting for you. Nothing is hidden.</p></div>')
    return "".join(parts)


def _ref_tag(ref: str) -> str:
    """The identifier, rendered so he can read it and quote it on a phone.

    Sized to be legible and selectable but not to compete with the item's
    name: monospace, one step down, muted. It is a handle, not a headline.
    """
    return f'<span class="ref">{_esc(ref)}</span>'


def _ref_chips(refs: tuple[str, ...]) -> str:
    """Tracking references, one quiet chip each. Empty when the backlog named
    none — this never invents a reference to fill the row out."""
    return "".join(f'<span class="chip chip-quiet">{_esc(r)}</span>'
                   for r in refs)


def _wording_note(text: str, what: str) -> str:
    """Mark a HEADLINE that is itself written in developer syntax.

    The explanation blocks are checked by `Prose.jargon_markers`; this covers
    the item's own name, which comes from the backlog and often carries a
    file name or a code identifier. Marked, never rewritten — renaming an
    item here would put a second name on it that drifts from the real one.
    """
    markers = summary_engineering_markers(text)
    if not markers:
        return ""
    return ('<div class="pb pb-jargon"><span class="pb-k">Developer wording'
            f'</span><p>The {_esc(what)} above is the backlog\'s own, and it '
            'contains ' + _esc(", ".join(markers)) + '. It needs renaming in '
            'plain English; it is shown as written rather than quietly '
            'reworded here.</p></div>')


def _when_label(d: PendingDecision) -> str:
    if d.overdue:
        return f"{abs(d.days_left)} day{'s' if abs(d.days_left) != 1 else ''} overdue"
    if d.days_left == 0:
        return "due today"
    return f"{d.days_left} day{'s' if d.days_left != 1 else ''} left"


#: A `Prose.decision` that says, in as many words, "not currently his" —
#: the backlog's own convention (see items 52/55/56/58: "The decision — None
#: for you", and item 30/57's "Not yet.", and the model-choice decision's
#: "Not yet yours to make."). Checked at the START of the field only, so a
#: decision that merely MENTIONS "none" or "not yet" mid-sentence still
#: counts as live.
_NOT_LIVE_DECISION_RE = re.compile(r"^\s*(none\b|not yet\b|possibly\b)", re.I)


def _is_live_owner_ask(prose: Prose) -> bool:
    """Whether this item belongs in "Waiting on you", derived from nothing
    but its own two authored fields — never inferred from engineering text.

    Both must be true:

      * `decision` is real AND currently his (not "None for you" and not
        "Not yet" — see `_NOT_LIVE_DECISION_RE`). A live decision on a
        market-structure number (items 55-58: how wide a level's zone is,
        how a stop should be sized) is real work, but it is answered by a
        published source or this desk's own data, never by him — which is
        exactly why those items write "None for you" and are excluded here.
      * `why_him` is written — the one reason it is MONEY, MANDATE, RISK
        APPETITE or PUBLIC DISCLOSURE, not a technical or research call.
        Requiring an explicit reason (rather than guessing from keywords)
        is what keeps a number-only item from sneaking in here just because
        somebody wrote a decision paragraph for it.
    """
    if not prose.decision or not prose.why_him:
        return False
    return not _NOT_LIVE_DECISION_RE.match(prose.decision)


#: Where a `Prose.decision` paragraph naturally ends its first sentence —
#: used only to build the one-line headline "Waiting on you" shows; the
#: full paragraph still renders underneath via `_render_prose`. Splits on a
#: '.' or '?' followed by a space and a capital letter or open-paren, so
#: "3.0x ATR" and "R/R" never trigger a false break.
_SENTENCE_END_RE = re.compile(r"(?<=[.?])\s+(?=[A-Z(])")


def _first_sentence(text: str) -> str:
    """The first sentence of a decision paragraph, for use as a headline."""
    text = text.strip()
    return _SENTENCE_END_RE.split(text, maxsplit=1)[0]


def owner_call_items(items: list[QueueItem]) -> list[QueueItem]:
    """Open items whose own prose names a live decision only he can make.

    Reused by both the funnel queue and the PM test gate, so "Waiting on
    you" never has to know which numbered sequence an item came from.
    """
    return [i for i in items if not i.done and _is_live_owner_ask(i.prose)]


def _render_owner_call_cards(items: list[QueueItem]) -> str:
    """One card per backlog item flagged `_is_live_owner_ask` — the same
    card shape as a formal pending decision, so the two read as one list."""
    rows = []
    for i in items:
        rows.append(
            '<article class="card">'
            '<div class="chips"><span class="chip chip-strong">Your call</span>'
            f'{_ref_chips(i.refs)}</div>'
            f'<h3>{_ref_tag(i.ref)}{_esc(_first_sentence(i.prose.decision))}</h3>'
            f'{_prose_block("Why only you", i.prose.why_him, "pb-dec")}'
            f'{_render_prose(i.prose, want_recommendation=True, raw_source=i.raw_body)}'
            '</article>'
        )
    return "\n".join(rows)


def _render_decisions(decisions: list[PendingDecision],
                      owner_calls: list[QueueItem] | None = None) -> str:
    """Everything waiting on the owner: formal DECIDE-BY lines first (they
    carry a due date CI enforces), then backlog items whose own prose names
    a live decision that is his alone. Nothing here is an agent's to make.

    A single combined empty state, not two — an owner with nothing pending
    in either source should see one quiet line, not two.
    """
    owner_calls = owner_calls or []
    if not decisions and not owner_calls:
        return ('<div class="note">Nothing is waiting on you. Every judgement '
                'call that was open has been answered.</div>')
    rows = []
    for d in decisions:
        rows.append(
            f'<article class="card {"card-urgent" if d.overdue or d.days_left == 0 else ""}">'
            f'<div class="chips"><span class="chip chip-strong">Your call</span>'
            f'<span class="chip">{_esc(_when_label(d))}</span>'
            f'<span class="chip chip-quiet">by {_esc(d.due.strftime("%-d %B"))}</span>'
            f'{_ref_chips(d.refs)}</div>'
            f'<h3>{_ref_tag(d.ref)}{_esc(d.question)}</h3>'
            f'{_render_prose(d.prose, want_recommendation=True, raw_source=d.raw_body)}'
            f'{_wording_note(d.question, "question")}'
            '</article>'
        )
    return "\n".join(rows) + _render_owner_call_cards(owner_calls)


def _render_open_queue(items: list[QueueItem], problem: str | None,
                       empty_message: str = "Nothing is queued.") -> str:
    """What is next, in order: one line each, opening to the full explanation.

    Mobile first — the line is the whole tap target and everything else is
    behind it, so the owner can read the running order on one screen without
    scrolling past four paragraphs to reach item two.

    `empty_message` lets a caller whose empty state means something more
    specific than "nothing queued" say so — e.g. the PM gate, where an empty
    result is a deliberately cleared gate, not an absence of work.
    """
    if problem:
        return (f'<div class="note"><b>The running order could not be read.</b> '
                f'{_esc(problem)}</div>')
    if not items:
        return f'<div class="note">{_esc(empty_message)}</div>'
    rows = []
    for it in items:
        meta = []
        if it.share:
            meta.append(f'<span class="chip chip-quiet">{_esc(it.share)} of blocked trades</span>')
        if it.classification:
            meta.append(f'<span class="chip">{_esc(it.classification.lower())}</span>')
        # A mostly-finished item stays in the running order — moving it out
        # would hide real outstanding work — but it must not read as
        # untouched. See `_RENDER_PART_DONE_WORDS`.
        if it.part_done:
            # "Partly", not "mostly": the same chip covers "PARTIALLY FIXED"
            # and "MOSTLY FIXED", and calling a partial fix mostly done
            # overstates it in the one direction that costs him a surprise.
            meta.append('<span class="chip">partly done &mdash; some work '
                        'still outstanding</span>')
        meta.append(_ref_chips(it.refs))
        if not it.prose.plain:
            meta.append('<span class="chip chip-gap">no plain-English version yet</span>')
        rows.append(
            '<details class="q">'
            f'<summary><span class="q-n">{_esc(it.ref)}</span>'
            f'<span class="q-t">{_esc(it.title)}</span></summary>'
            f'<div class="q-body"><div class="chips">{"".join(meta)}</div>'
            f'{_render_prose(it.prose, raw_source=it.raw_body)}'
            f'{_wording_note(it.title, "name")}</div>'
            '</details>'
        )
    return "\n".join(rows)


def _render_one_liners(items: list[QueueItem], empty: str,
                       *, struck: bool = False) -> str:
    """A plain one-line-each list — used for what is paused and what is
    already resolved. Neither needs anything from him, so neither gets the
    weight of a card."""
    if not items:
        return f'<div class="note">{empty}</div>'
    rows = []
    for it in items:
        cls = "ol ol-done" if struck else "ol"
        rows.append(f'<div class="{cls}"><span class="q-n">{_esc(it.ref)}</span>'
                    f'<span>{_esc(it.title)}</span></div>')
    return "\n".join(rows)


def _render_finished_unmarked(items: list[QueueItem]) -> str:
    """Items their own author has written up as finished, which the backlog
    has not struck through.

    Presented as FINISHED, because that is what the item's own status says,
    with the untidied strike-through stated as the small record-keeping point
    it actually is. Drawing these as live work is what the owner described as
    "déjà vu every day dealing with the same stuff over and over" — the words
    "SHIPPED", "REPLACED" and "REDESIGNED" were simply not in the board's
    vocabulary, so finished work queued up alongside work that was not.

    Nothing is believed on the item's behalf: the page says which half of the
    backlog is claiming what, and never picks one.
    """
    if not items:
        return ('<div class="note">Every finished item in the backlog is also '
                'ticked off as finished.</div>')
    rows = []
    for it in items:
        rows.append(
            '<div class="ol ol-done ol-untidy">'
            f'<span class="q-n">{_esc(it.ref)}</span>'
            f'<span>{_esc(it.title)} '
            '<em>&mdash; finished according to its own note; the backlog has '
            'not ticked it off yet, so that one line needs tidying.</em>'
            '</span></div>')
    return "\n".join(rows)


def _render_in_hand(items: list[QueueItem]) -> str:
    """Decided, or being built. Nothing here is his to answer.

    Each line says WHICH of the two it is, in words, because "you already
    decided this" and "somebody is building this" are different facts and
    the owner asked to be able to tell them apart from the things that are
    still his to rule on. Never struck through: none of it is finished.
    """
    if not items:
        return ('<div class="note">Nothing is in hand. Every item that has '
                'been decided or started is either finished or waiting on '
                'you.</div>')
    rows = []
    for it in items:
        rows.append(
            '<div class="ol ol-inhand"><span class="q-n">'
            f'{_esc(it.ref)}</span>'
            f'<span>{_esc(it.title)} '
            f'<em>&mdash; {_esc(it.in_hand_state)}; nothing needed from you.</em>'
            '</span></div>')
    return "\n".join(rows)


def _render_no_action(items: list[QueueItem]) -> str:
    """Causes that were checked and turned out to be by design."""
    if not items:
        return ('<div class="note">Nothing has been checked and found to be '
                'working as intended.</div>')
    rows = []
    for it in items:
        rows.append(
            '<div class="ol ol-noaction"><span class="q-n">'
            f'{_esc(it.ref)}</span>'
            f'<span>{_esc(it.title)} '
            '<em>&mdash; checked; working as intended, nothing to fix.</em>'
            '</span></div>')
    return "\n".join(rows)


def _render_review_owed(items: list[QueueItem]) -> str:
    """Finished work with a review still owed on it.

    Deliberately NOT collapsed into "done". The backlog says "FIXED, pending
    review", and that is two facts: the work is finished, and somebody still
    owes it a look. Reporting only the first would be a false all-clear;
    reporting only the second puts finished work back in the running order.
    """
    if not items:
        return ('<div class="note">Nothing is waiting on a review.</div>')
    rows = []
    for it in items:
        rows.append(
            '<div class="ol ol-review"><span class="q-n">'
            f'{_esc(it.ref)}</span>'
            f'<span>{_esc(it.title)} '
            '<em>&mdash; the work is done; a review is still owed.</em>'
            '</span></div>')
    return "\n".join(rows)


# --------------------------------------------------------------------------
# RIGHT NOW — exactly one thing, chosen mechanically
#
# The retired hand-maintained page had a single card at the top and that is
# what made it usable: one thing, never a list. The order below is fixed and
# derived, never authored, so the card cannot become another thing somebody
# has to remember to update.
#
#   1. something recorded as finished stopped proving out  (rot beats all)
#   2. a decision of his that is past its date
#   3. a decision of his that is due
#   4. the highest-ranked open item in the running order
#   5. nothing — and it says so, rather than inventing urgency
# --------------------------------------------------------------------------

#: Turn a mechanical rule detail into words the owner can read. The evidence
#: rules name settings and tests by their code identifiers, which is correct
#: for the rules and wrong for this page — see "Who this page is for". This
#: does not summarise or interpret: it renames, and the numbers it reports are
#: the ones the rule actually found.
_HUMAN_WORDS = {
    "pct": "percent", "usd": "dollars", "atr": "daily range",
    "min": "minimum", "max": "maximum", "llm": "AI", "pm": "portfolio manager",
    "rr": "reward to risk", "cfg": "configuration",
}


def _humanise_identifier(name: str) -> str:
    """`risk.min_stop_atr_multiple` -> "minimum stop daily range multiple"."""
    tail = name.rsplit(".", 1)[-1]
    if tail.startswith("test_"):
        tail = tail[len("test_"):]
    words = [_HUMAN_WORDS.get(w, w) for w in tail.split("_") if w]
    #: A date written as separate underscore-joined parts (2026_08_28) would
    #: read as three unrelated numbers once the underscores become spaces.
    joined = " ".join(words)
    return re.sub(r"\b(\d{4}) (\d{2}) (\d{2})\b", r"\1-\2-\3", joined)


def _plain_failure(result: "RuleResult") -> str:
    """One failing evidence rule, in plain words. Empty if it is not a
    failure this card should speak about."""
    detail = result.detail or ""
    m = re.match(r"^(\S+)\s*=\s*(.+?)\s*\(expected\s*(.+?)\)\s*$", detail)
    if m:
        return (f"it expects the {_humanise_identifier(m.group(1))} to be "
                f"{m.group(3)}, and it is {m.group(2)}")
    m = re.match(r"^(\S+)\s+is NOT present in settings\s*$", detail)
    if m:
        return (f"it looks for a setting called {_humanise_identifier(m.group(1))}, "
                "which no longer exists under that name")
    m = re.match(r"^(\S+)\s+MISSING\s*$", detail)
    if m:
        return (f"it looks for a test called {_humanise_identifier(m.group(1))}, "
                "which no longer exists under that name")
    return ""


def _failure_lines(contradicted: list["PhaseView"]) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for ph in contradicted:
        reasons = [r for r in (_plain_failure(x) for x in ph.results
                               if x.verdict == FAIL) if r]
        if reasons:
            out.append((_strip_markdown(ph.title), reasons))
    return out


def _render_right_now(contradicted: list[PhaseView],
                      decisions: list[PendingDecision],
                      open_items: list[QueueItem]) -> str:
    #: A backlog headline is often a full sentence of engineering prose. Set
    #: at the top card's display size it stops being a heading and becomes a
    #: paragraph in heading clothing, which is what made this card read as a
    #: structural mess. Past this length the card steps the heading down a
    #: size instead. Presentation only: the title is never shortened, because
    #: a shortened title is a second name that drifts from the real one.
    long_title = 72

    def card(kind: str, title: str, inner: str, *,
             ref: str = "", refs: tuple[str, ...] = ()) -> str:
        h_cls = "rn-h rn-h-long" if len(title) > long_title else "rn-h"
        return (f'<article class="rn"><div class="chips">'
                f'<span class="chip chip-strong">Right now</span>'
                f'<span class="chip">{_esc(kind)}</span>'
                f'{_ref_chips(refs)}</div>'
                f'<h2 class="{h_cls}">'
                f'{_ref_tag(ref) if ref else ""}{_esc(title)}</h2>{inner}</article>')

    if contradicted:
        n = len(contradicted)
        groups = _failure_lines(contradicted)
        total = sum(len(r) for _, r in groups)
        detail = "".join(
            f'<p><strong>{_esc(title)}</strong><br>'
            + "<br>".join(_esc(r[0].upper() + r[1:]) for r in reasons)
            + "</p>"
            for title, reasons in groups)
        example = (groups[0][1][0] if groups and groups[0][1] else "")
        return card(
            "a proof that has gone out of date",
            f'{n} finished item{"s" if n != 1 else ""} can no longer prove '
            f'{"they are" if n != 1 else "it is"} still finished',
            '<div class="pb pb-plain"><span class="pb-k">In plain language'
            '</span><p>Every finished piece of work carries a short list of '
            'automatic checks that prove it is still in place, and this page '
            're-runs all of them each time it is built. '
            f'{total} of those checks now fail. A failing check means the '
            'check and the system disagree &mdash; it does NOT by itself mean '
            'the work broke. A setting deliberately changed since the check '
            'was written fails it exactly the same way real breakage would, '
            'so each one has to be read before it is believed.</p></div>'
            + (f'<div class="pb pb-ex"><span class="pb-k">For example</span>'
               f'<p>One failing check says {_esc(example)}. '
               'If that difference is a decision already taken, the check is '
               'simply out of date. If nobody decided it, something moved '
               'that should not have.</p></div>' if example else "")
            + f'<div class="pb pb-dec"><span class="pb-k">What is failing'
              f'</span>{detail}</div>'
            '<div class="pb pb-dec"><span class="pb-k">What you have to decide'
            '</span><p>For each one: was this a change you made on purpose, or '
            'not? That answer decides whether the check gets updated or the '
            'system gets investigated. Nobody can settle it from this page '
            'alone.</p></div>'
            '<div class="pb pb-rec"><span class="pb-k">Our recommendation'
            '</span><p>Work through them one at a time rather than treating '
            'the whole group as an alarm. Where a check names a setting or a '
            'test that was renamed, updating the check is the fix. Where a '
            'check expects a number you have since changed on purpose, the '
            'check is stale and should follow your decision. Anything left '
            'over after that is the real finding, and it is the only part '
            'worth alarm.</p></div>')

    overdue = [d for d in decisions if d.overdue]
    due = overdue or [d for d in decisions if d.days_left <= 7]
    if due:
        d = due[0]
        return card(
            "a judgement call only you can make",
            d.question,
            f'<div class="chips"><span class="chip">{_esc(_when_label(d))}</span>'
            f'<span class="chip chip-quiet">by '
            f'{_esc(d.due.strftime("%-d %B %Y"))}</span></div>'
            + _render_prose(d.prose, want_recommendation=True,
                            raw_source=d.raw_body)
            + _wording_note(d.question, "question"),
            ref=d.ref, refs=d.refs)

    if open_items:
        # Nothing is waiting on his decision — the card says so in its own
        # label rather than dressing the next work item up as one. The
        # callers pass only genuinely open items here: anything decided,
        # being built, or checked-and-fine has already been bucketed away.
        it = open_items[0]
        return card("no decision is waiting on you — this is next in the "
                    "running order", it.title,
                    _render_prose(it.prose, raw_source=it.raw_body)
                    + _wording_note(it.title, "name"),
                    ref=it.ref, refs=it.refs)

    return ('<article class="rn rn-clear"><div class="chips">'
            '<span class="chip chip-strong">Right now</span></div>'
            '<h2 class="rn-h">Nothing needs you</h2>'
            '<div class="pb pb-plain"><span class="pb-k">In plain language'
            '</span><p>No finished work has stopped proving out, no judgement '
            'call is waiting on you, and the running order is empty. There is '
            'nothing on this page to act on.</p></div></article>')


def _safely(loader: Any, work_md: Path, what: str,
            notes: dict[str, Prose] | None = None
            ) -> tuple[list[QueueItem], str | None]:
    """Run a backlog loader; turn any breakage into a sentence, never a crash.

    The loaders already report a moved heading or a changed item shape as a
    plain-English problem. This catches the rest — an unreadable file, a
    decoding error, a shape nobody anticipated — and reports it the same way,
    because the one thing this page must never do is fail to load.

    `notes` is forwarded to the loader (`docs/BOARD_NOTES.md`, already
    parsed) — a broken notes file must degrade the same way a broken backlog
    does: reported, never crashed on.
    """
    try:
        return loader(work_md, notes=notes)
    except Exception as exc:  # noqa: BLE001 - a sentence beats a stack trace
        return [], (f"The backlog could not be read, so {what} is not shown "
                    f"here. The file itself needs looking at "
                    f"({type(exc).__name__}).")


def _pm_gate_lede(open_count: int, total: int) -> str:
    """The one-line status under 'Before the model test can mean anything'.

    A plain 'N of M feeds are still not signed off' reads as '0 of 0' when
    the gate is cleared, which looks like a parse failure rather than a
    deliberately empty gate. Say the clear state in words instead.
    """
    if total == 0 and open_count == 0:
        return ("Every feed is signed off. Nothing is blocking the model "
                "test on this gate.")
    return (f"{open_count} of {total} feeds are still not signed off.")


def _unexplained_note(unexplained: int, total: int) -> str:
    """One honest line about how much of this page is not yet written for him.

    Counted, never concealed and never filled in. The gap is the finding.
    """
    if total == 0:
        return ""
    if unexplained == 0:
        return ("Every live item below has a plain-English explanation and a "
                "real example.")
    return (f"{unexplained} of the {total} have no plain-English explanation "
            "yet. They are still listed, and marked as such &mdash; nothing "
            "on this page is invented to fill a gap.")


def render(phases: list[PhaseView], state: dict[str, Any], template: Path,
           work_md: Path | None = None,
           board_notes: Path | None = None,
           in_flight: inflight.InFlight = inflight.NOT_ATTEMPTED) -> str:
    """`in_flight` is what GitHub said was open when `main` asked it (see
    `inflight.read_in_flight`). The default is the explicit "nobody asked"
    state, so a preview or a test renders an honest "could not read" line
    rather than reaching for the network — and never an empty list."""
    now = datetime.now(ET)
    total_rules = sum(len(p.results) for p in phases)
    total_pass = sum(p.passed for p in phases)
    total_fail = sum(p.failed for p in phases)
    total_unknown = sum(p.unknown for p in phases)
    contradicted = [p for p in phases if p.verdict == "CONTRADICTED"]
    jargon_flagged = [p for p in phases if p.summary_flagged]

    # Relevance ordering: anything CONTRADICTED first (loud, never
    # collapsed), then everything else still open or unverified, then --
    # only at the very bottom, and only inside a closed <details> -- the
    # phases that are both fully verified and recorded as finished. This
    # changes how phases are grouped and displayed, not which rule kind
    # produced which verdict.
    settled = [p for p in phases if _is_settled(p)]
    attention = contradicted + [p for p in phases if p not in contradicted and not _is_settled(p)]

    if attention:
        rows = f'<table class="plan">{"".join(_row(p) for p in attention)}</table>'
    else:
        rows = '<p class="lede">Nothing needs attention right now &mdash; every open item has settled.</p>'
    if settled:
        rows += (
            '<details class="finished">'
            f'<summary>{len(settled)} finished and verified &mdash; expand to review</summary>'
            f'<table class="plan">{"".join(_row(p) for p in settled)}</table>'
            '</details>'
        )

    alarm = ""
    if contradicted:
        names = ", ".join(_esc(_strip_markdown(p.title)) for p in contradicted)
        alarm = (
            '<div class="item gap"><span class="chip chip-strong">Proof failed</span>'
            f'<h3>{len(contradicted)} piece(s) of finished work no longer prove '
            'they are finished</h3>'
            f'<p>{names}</p>'
            '<p>A check and the system disagree. That can mean the work broke, '
            'or it can mean the check still expects something you changed on '
            'purpose. The card at the top of this page lists each one.</p></div>'
        )
    else:
        alarm = (
            '<div class="item done"><span class="chip chip-quiet">All clear</span>'
            '<h3>Everything recorded as finished still proves it</h3>'
            '<p>Nothing claims to be done on evidence that has since stopped '
            'holding.</p></div>'
        )

    # Reports, never gates: a flagged summary still renders in full further
    # down (see `_row`). This is only the count, placed where the freshness
    # banner already lives — right at the top, before he has to scroll past
    # anything else — so he does not have to hunt through the list to find
    # out how many descriptions were written for a developer instead of him.
    jargon_banner = ""
    if jargon_flagged:
        n = len(jargon_flagged)
        jargon_banner = (
            '<div class="jargon-banner"><b>'
            f'{n} description{"s" if n != 1 else ""} below {"are" if n != 1 else "is"} '
            'written for a developer, not for you.</b> They are marked where they '
            'appear so you can tell them apart from the ones already in plain '
            'English &mdash; nothing is hidden, they still say what they say.'
            '</div>'
        )

    if state.get("in_sync") is True:
        deploy = ('<span class="dot ok"></span> The machine is running the latest '
                  'finished work.')
    elif state.get("undeployed_merges"):
        deploy = (f'<span class="dot warn"></span> '
                  f'{state["undeployed_merges"]} finished change(s) are not on the '
                  'machine yet.')
    else:
        deploy = '<span class="dot unk"></span> Deploy state could not be read.'

    spend = state.get("spend_today")
    limit = 2.75
    pct = int(round(100 * spend / limit)) if isinstance(spend, (int, float)) else None

    body = template.read_text()
    body = body.replace("{{JARGON_BANNER}}", jargon_banner)
    # Get the commit hash of the current repo where the board is being generated
    rc, cur_sha = _run(["git", "rev-parse", "HEAD"], REPO_ROOT)
    cur_sha_short = cur_sha.strip()[:9] if rc == 0 else ""
    timestamp = now.strftime("%A %-d %B %Y &middot; %H:%M ET")
    body = body.replace("{{STAMP}}", timestamp)
    # Commit hash in short form, visually subordinate with smaller font
    stamp_hash = f"<span style=\"font-size:10px;opacity:0.65\">built from {cur_sha_short}</span>" if cur_sha_short else ""
    body = body.replace("{{STAMP_HASH}}", stamp_hash)
    # The full commit this page was built against, stamped into a
    # machine-readable <meta> tag. src/api/server.py reads it back out at
    # serve time and compares it to a freshly-read live SHA — that comparison
    # (fact vs. fact), not page age, is what decides whether a freshness
    # banner is shown. Empty when the box's SHA could not be read, which the
    # server-side check treats as UNKNOWN, never as "fine".
    body = body.replace("{{BUILT_SHA}}", _esc(state.get("box_sha_full") or ""))
    body = body.replace("{{DEPLOY}}", deploy)
    body = body.replace("{{CIRCUIT}}", _fmt(state.get("circuit")))
    body = body.replace("{{SPEND}}", f"${spend:.2f}" if spend is not None else
                        '<span class="unk">unknown</span>')
    body = body.replace("{{SPEND_PCT}}", str(pct if pct is not None else 0))
    body = body.replace("{{SPEND_NOTE}}",
                        ("nothing has run today" if spend == 0
                         and state.get("sessions_today") == 0
                         else f"of the ${limit:.2f} daily limit &mdash; {pct}% used")
                        if pct is not None else "daily spend could not be read")
    body = body.replace("{{SESSIONS}}", _fmt(state.get("sessions_today")))

    # --- everything below comes out of the backlog, the source of truth ----
    #
    # Read defensively. The backlog is edited by hand, constantly, by several
    # sessions at once. A malformed edit must produce a page that SAYS it
    # could not read the backlog — it must never produce a stack trace on his
    # phone, because a board that 500s is a board he stops trusting.
    work_md = work_md or (REPO_ROOT / "docs" / "WORK.md")
    # docs/BOARD_NOTES.md carries only the owner-facing prose, keyed by each
    # item's number and section (see `load_board_notes`). It is read
    # defensively too, for the same reason: a broken notes file must fall
    # back to "not yet explained" for every item, never a stack trace.
    board_notes = board_notes or (REPO_ROOT / "docs" / "BOARD_NOTES.md")
    try:
        notes = load_board_notes(board_notes)
    except Exception:  # noqa: BLE001 - a blank prose set beats a stack trace
        notes = {}
    queue_items, queue_problem = _safely(load_funnel_queue, work_md,
                                         "the running order", notes=notes)
    pm_gate_items, pm_gate_problem = _safely(load_pm_gate, work_md,
                                             "the model-test gate", notes=notes)
    try:
        decisions = load_pending_decisions(work_md, notes=notes)
    except Exception:  # noqa: BLE001 - see above
        decisions = []

    open_items = [i for i in queue_items if i.bucket == "open"]
    paused_items = [i for i in queue_items if i.bucket == "paused"]
    resolved_items = [i for i in queue_items if i.bucket == "resolved"]
    # Finished by their own account, not ticked off in the backlog. Drawn as
    # finished — see `_render_finished_unmarked` for why that is the honest
    # reading and why drawing them as live work was the defect.
    finished_unmarked = [i for i in queue_items
                         if i.bucket == "finished_unmarked"]
    review_owed = [i for i in queue_items if i.bucket == "review_owed"]
    # Decided or being built: his answer is already given, or the work is
    # under way. Drawn BELOW everything that is actually his to answer.
    in_hand = [i for i in queue_items if i.bucket == "in_hand"]
    no_action = [i for i in queue_items if i.bucket == "no_action"]

    # Roadblocks only he can clear — money, mandate, risk appetite, or
    # public disclosure (see `_is_live_owner_ask`). Pulled out of BOTH
    # numbered sequences and lifted to "Waiting on you" so they stop
    # competing with ordinary engineering work for his attention, which was
    # his own complaint. Removed from every bucket below by reference, never
    # copied, so one item can never show twice.
    owner_calls = sorted(
        owner_call_items(queue_items) + owner_call_items(pm_gate_items),
        key=lambda i: (i.source, i.rank))
    owner_call_refs = {i.ref for i in owner_calls}
    open_items = [i for i in open_items if i.ref not in owner_call_refs]
    paused_items = [i for i in paused_items if i.ref not in owner_call_refs]
    resolved_items = [i for i in resolved_items if i.ref not in owner_call_refs]
    finished_unmarked = [i for i in finished_unmarked
                         if i.ref not in owner_call_refs]
    review_owed = [i for i in review_owed if i.ref not in owner_call_refs]
    in_hand = [i for i in in_hand if i.ref not in owner_call_refs]
    no_action = [i for i in no_action if i.ref not in owner_call_refs]
    unexplained = [i for i in open_items if not i.prose.plain]

    body = body.replace("{{RIGHT_NOW}}",
                        _render_right_now(contradicted, decisions, open_items))
    body = body.replace("{{DECISIONS}}", _render_decisions(decisions, owner_calls))
    body = body.replace("{{QUEUE}}", _render_open_queue(open_items, queue_problem))
    body = body.replace("{{PAUSED}}", _render_one_liners(
        paused_items,
        "Nothing is parked. Everything in the backlog is either being worked "
        "on or already finished."))
    body = body.replace("{{FINISHED_UNMARKED}}",
                        _render_finished_unmarked(finished_unmarked))
    body = body.replace("{{REVIEW_OWED}}", _render_review_owed(review_owed))
    body = body.replace("{{IN_HAND}}", _render_in_hand(in_hand))
    body = body.replace("{{IN_HAND_COUNT}}", str(len(in_hand)))
    # Under construction, from GitHub — fenced so the server can swap in a
    # fresher read on every request; the build-time copy carries its own
    # read time so it can never pass for current when it is not.
    body = body.replace("{{IN_FLIGHT}}", inflight.render_in_flight(in_flight))
    body = body.replace("{{NO_ACTION}}", _render_no_action(no_action))
    body = body.replace("{{NO_ACTION_COUNT}}", str(len(no_action)))
    body = body.replace("{{RESOLVED}}", _render_one_liners(
        resolved_items,
        "Nothing has been signed off as finished yet.", struck=True))
    body = body.replace("{{QUEUE_OPEN}}", str(len(open_items)))
    body = body.replace("{{QUEUE_TOTAL}}", str(len(queue_items)))
    body = body.replace("{{PAUSED_COUNT}}", str(len(paused_items)))
    body = body.replace("{{RESOLVED_COUNT}}", str(len(resolved_items)))
    body = body.replace("{{FINISHED_UNMARKED_COUNT}}", str(len(finished_unmarked)))
    body = body.replace("{{REVIEW_OWED_COUNT}}", str(len(review_owed)))
    body = body.replace("{{UNEXPLAINED_NOTE}}",
                        _unexplained_note(len(unexplained), len(open_items)))

    pm_gate_open = [i for i in pm_gate_items
                   if not i.done and i.ref not in owner_call_refs]
    body = body.replace("{{PM_GATE}}", _render_open_queue(
        pm_gate_open, pm_gate_problem,
        empty_message="Gate clear — nothing is blocking the model test."))
    body = body.replace("{{PM_GATE_LEDE}}",
                        _pm_gate_lede(len(pm_gate_open), len(pm_gate_items)))
    body = body.replace("{{PM_GATE_DONE}}", _render_one_liners(
        [i for i in pm_gate_items if i.done],
        "None of the feeds have been signed off yet.", struck=True))
    body = body.replace("{{ROWS}}", rows)
    body = body.replace("{{ALARM}}", alarm)
    body = body.replace("{{RULES_TOTAL}}", str(total_rules))
    body = body.replace("{{RULES_PASS}}", str(total_pass))
    body = body.replace("{{RULES_FAIL}}", str(total_fail))
    body = body.replace("{{RULES_UNKNOWN}}", str(total_unknown))
    body = body.replace("{{BOX_SHA}}", _fmt(state.get("box_sha")))
    body = body.replace("{{MAIN_SHA}}", _fmt(state.get("main_sha")))
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/board/index.html")
    ap.add_argument("--manifest", default="docs/phases.yaml")
    ap.add_argument("--template", default="scripts/status_board_template.html")
    ap.add_argument("--work-md", default="docs/WORK.md",
                    help="the backlog to render from; point it elsewhere to "
                         "preview a page without touching the real one")
    ap.add_argument("--board-notes", default="docs/BOARD_NOTES.md",
                    help="the owner-facing prose to render alongside the "
                         "backlog's items; point it elsewhere to preview a "
                         "page without touching the real one")
    ap.add_argument("--no-github", action="store_true",
                    help="do not ask GitHub what is in flight; the section "
                         "then says the page was built without asking, "
                         "never that nothing is in flight")
    ap.add_argument("--json", action="store_true", help="also print the findings as JSON")
    ap.add_argument("--explain", metavar="PHASE_ID", default=None,
                    help="print every rule and its verdict for one phase, then exit")
    args = ap.parse_args()

    manifest = REPO_ROOT / args.manifest
    if not manifest.exists():
        print(f"manifest not found: {manifest}", file=sys.stderr)
        return 2

    cfg = read_settings()

    if args.explain:
        raw = yaml.safe_load(manifest.read_text())
        entries = raw["phases"] if isinstance(raw, dict) and "phases" in raw else raw
        hit = [e for e in entries if str(e.get("id")) == args.explain]
        if not hit:
            print(f"no phase with id {args.explain!r}. ids: "
                  + ", ".join(str(e.get('id')) for e in entries), file=sys.stderr)
            return 2
        e = hit[0]
        print(f"{e.get('title')}  —  recorded as {e.get('status')}")
        for rule in e.get("evidence", []) or []:
            r = check_rule(rule, cfg)
            mark = {PASS: " ok ", FAIL: "FAIL", UNKNOWN: " ?? "}[r.verdict]
            print(f"  [{mark}] {r.kind:16s} {r.detail}")
            if r.verdict == FAIL:
                print(f"           rule: {rule}")
        return 0

    phases = load_phases(manifest, cfg)
    state = live_state()

    out = Path(args.out)
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    work_md = Path(args.work_md)
    if not work_md.is_absolute():
        work_md = REPO_ROOT / work_md
    board_notes = Path(args.board_notes)
    if not board_notes.is_absolute():
        board_notes = REPO_ROOT / board_notes
    in_flight = (inflight.NOT_ATTEMPTED if args.no_github
                 else inflight.read_in_flight())
    try:
        out.write_text(render(phases, state, REPO_ROOT / args.template, work_md,
                              board_notes, in_flight=in_flight))
    except OSError as exc:
        print(f"failed to write {out}: {exc}", file=sys.stderr)
        return 2

    contradicted = [p.title for p in phases if p.verdict == "CONTRADICTED"]
    if args.json:
        print(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "out": str(out),
            "contradicted": contradicted,
            "in_flight": ({"problem": in_flight.problem} if not in_flight.readable
                          else {"read_at": in_flight.read_at,
                                "items": [{"number": i.number, "title": i.title,
                                           "stage": i.stage}
                                          for i in in_flight.items]}),
            "state": {k: v for k, v in state.items()},
            "phases": [
                {"id": p.id, "recorded": p.recorded, "verdict": p.verdict,
                 "pass": p.passed, "fail": p.failed, "unknown": p.unknown}
                for p in phases
            ],
        }, indent=2, default=str))
    else:
        print(f"wrote {out}")
        print(f"phases: {len(phases)}  contradicted: {len(contradicted)}")
        if contradicted:
            print("CONTRADICTED: " + ", ".join(contradicted))

    # Contradicted phases stay on the page (that is the rot detector). They
    # are not a process failure: the systemd unit that rebuilds this board
    # used to treat exit 1 as a failed unit even after a successful write,
    # so `systemctl status` said "failed" while data/board/index.html was
    # current. Crash or write failure still returns non-zero above.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
