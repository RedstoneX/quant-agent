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
  * what is paused and needs no decision, so he knows what to ignore;
  * what is already resolved, so finished work stops competing for attention.

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

    @property
    def has_any(self) -> bool:
        return bool(self.plain or self.example or self.decision
                    or self.recommendation)

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
        """
        tail = self.status_tail
        if any(w in tail for w in _CLOSURE_NEGATIONS):
            return ""
        if not _closure_hit(tail, _RENDER_CLOSURE_WORDS):
            return ""
        if _closure_hit(tail, _RENDER_PART_DONE_WORDS):
            return "part_done"
        if _closure_hit(tail, _RENDER_REVIEW_OWED_WORDS):
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
    def bucket(self) -> str:
        """Which section of the page this item belongs in. One item, one
        section, decided once here so no two renderers can disagree.

        ``finished_unmarked`` is the bucket that answers the owner's "déjà vu"
        complaint: an item whose status word the board did not used to
        recognise ("SHIPPED", "REPLACED", "REDESIGNED") was drawn in the
        running order, competing with live work. It is now drawn as finished.
        """
        if self.done:
            return "resolved"
        if self.claims_closure:
            return "finished_unmarked"
        if self.review_owed:
            return "review_owed"
        if self.paused:
            return "paused"
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
    for c in _QUEUE_CLASSES:
        title = re.sub(re.escape(c) + r"\.?", "", title, flags=re.I)
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

    items = _parse_numbered_items(body, source="pm-gate", notes=notes)
    if not items:
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


#: `- [ ] DECIDE BY 2026-09-16 — question` — the same shape
#: `test_no_pending_decision_is_overdue` enforces, deliberately, so the board
#: and the build are reading one format and cannot disagree about it.
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
#: any of these, by requirement: the owner is red/green colour blind.
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
# Status is ALWAYS carried by a word. The owner is red/green colour blind, so
# nothing on this page may depend on hue to be understood: every marker is a
# text label, and the shapes that carry emphasis are border weight, position
# and a glyph, never a colour swapped for another colour of similar lightness.
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


def _render_decisions(decisions: list[PendingDecision]) -> str:
    """Decisions waiting on the owner. Nothing here is an agent's to make."""
    if not decisions:
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
    return "\n".join(rows)


def _render_open_queue(items: list[QueueItem], problem: str | None) -> str:
    """What is next, in order: one line each, opening to the full explanation.

    Mobile first — the line is the whole tap target and everything else is
    behind it, so the owner can read the running order on one screen without
    scrolling past four paragraphs to reach item two.
    """
    if problem:
        return (f'<div class="note"><b>The running order could not be read.</b> '
                f'{_esc(problem)}</div>')
    if not items:
        return '<div class="note">Nothing is queued.</div>'
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
        names = "; ".join(f"{_strip_markdown(p.title)} (stage {p.id})"
                          for p in contradicted)
        n = len(contradicted)
        return card(
            "something that was true has stopped being true",
            "A piece of finished work no longer proves it is finished",
            f'<div class="pb pb-plain"><span class="pb-k">In plain language'
            f'</span><p>{n} thing{"s" if n != 1 else ""} recorded as done '
            'cannot be shown to still be done. Nobody broke a rule &mdash; '
            'this page re-checks the proof behind every finished item each '
            'time it is built, and this time the proof did not hold.</p></div>'
            f'<div class="pb pb-dec"><span class="pb-k">Affected</span>'
            f'<p>{_esc(names)}</p></div>'
            '<div class="pb pb-rec"><span class="pb-k">Our recommendation'
            '</span><p>Treat it as live breakage until it is re-checked. '
            'This is the one failure this page exists to catch, so it '
            'outranks everything else on it.</p></div>')

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
        it = open_items[0]
        return card("top of the running order", it.title,
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
           board_notes: Path | None = None) -> str:
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
            '<p>Something that was true has stopped being true. This is the failure '
            'this page exists to catch.</p></div>'
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
    body = body.replace("{{STAMP}}", now.strftime("%A %-d %B %Y &middot; %H:%M ET"))
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
    unexplained = [i for i in open_items if not i.prose.plain]

    body = body.replace("{{RIGHT_NOW}}",
                        _render_right_now(contradicted, decisions, open_items))
    body = body.replace("{{DECISIONS}}", _render_decisions(decisions))
    body = body.replace("{{QUEUE}}", _render_open_queue(open_items, queue_problem))
    body = body.replace("{{PAUSED}}", _render_one_liners(
        paused_items,
        "Nothing is parked. Everything in the backlog is either being worked "
        "on or already finished."))
    body = body.replace("{{FINISHED_UNMARKED}}",
                        _render_finished_unmarked(finished_unmarked))
    body = body.replace("{{REVIEW_OWED}}", _render_review_owed(review_owed))
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

    pm_gate_open = [i for i in pm_gate_items if not i.done]
    body = body.replace("{{PM_GATE}}", _render_open_queue(pm_gate_open, pm_gate_problem))
    body = body.replace("{{PM_GATE_DONE}}", _render_one_liners(
        [i for i in pm_gate_items if i.done],
        "None of the feeds have been signed off yet.", struck=True))
    body = body.replace("{{PM_GATE_OPEN}}", str(len(pm_gate_open)))
    body = body.replace("{{PM_GATE_TOTAL}}", str(len(pm_gate_items)))
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
    out.write_text(render(phases, state, REPO_ROOT / args.template, work_md,
                          board_notes))

    contradicted = [p.title for p in phases if p.verdict == "CONTRADICTED"]
    if args.json:
        print(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "out": str(out),
            "contradicted": contradicted,
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

    # A contradiction is worth a non-zero exit: the systemd unit that rebuilds
    # this board is configured to surface that as a failed unit rather than
    # swallowing it, so the finding is visible on the box too, not only on the
    # page.
    return 1 if contradicted else 0


if __name__ == "__main__":
    raise SystemExit(main())
