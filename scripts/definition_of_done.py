#!/usr/bin/env python3
"""Definition of done, decided by code rather than by convention.

Deterministic and read-only. No model call, no daemon, no network, no
credential. Every input is either a file in the tree, a `git` read of the
commit this change is measured against, or a commit message in
`base..HEAD` — all three available to `pytest` in GitHub Actions without a
token, which is the reason none of it reads the GitHub API.

THE PROBLEM THIS CLOSES
-----------------------
Work on this desk half-lands, and the surviving half is always the one
somebody was looking at. Measured cases, each verifiable in this tree:

  * the Portfolio Manager was stopped from being TOLD it has no margin
    and was never SHOWN its real buying power;
  * `trading_calendar.trading_sessions_held` was added on 2026-09-04 to
    replace a calendar-day count, and exactly ONE of its consumers was
    switched over. `src/pipeline.py` still computes a calendar-day
    `days_held` at three sites, and at the pace computation divides it by
    `pinned_horizon`, which is counted in SESSIONS — the same unit
    mismatch the 2026-09-04 fix existed to remove, three lines below a
    correctly-computed `sessions_held` that goes unused there;
  * a catalogue of arbitrary numbers was completed and sourcing them was
    never started;
  * a credential file was built and the units that read it were never
    installed;
  * four salvage fixes from an abandoned branch were agreed and never
    taken.

The shape is identical every time. Nobody decided to ship half. The half
in front of the author got fixed, and the rest became permanent because
nothing ever asked about it again.

`scripts/work_queue.py` is the existing attempt and it says its own limit
out loud at its line 104: *"It does not judge the adversary's argument,
only that one is written down. A dishonest `Adversary:` line passes."*
That is presence-only. This module replaces the presence test with four
checks whose failures a non-author can reproduce.

WHY IT LIVES IN pytest
----------------------
`pytest` is the only check branch protection requires, so it is the only
place a gate can block rather than advise. `tests/test_definition_of_done.py`
is the wiring; everything decidable lives here so it can be unit-tested
against synthetic repositories instead of only against whatever the
ambient checkout happens to contain.

THE FOUR CHECKS
---------------
1. `declared_criteria_problems` — an item declares its completion criteria
   when it is FILED. Closing it accounts for every criterion declared at
   the base commit: met, or deferred onto a named item that exists. A
   criterion cannot simply stop being mentioned.

2. `consumer_completeness_problems` — a change to a registered shared
   quantity must account for every site that READS it. The consumer set is
   DERIVED from the tree by AST walk, never taken from the author, and the
   author's list of deliberately-unchanged consumers is checked against
   that derivation in both directions: a name not in the derived set is a
   stale or invented entry and fails just as a missing one does. This is
   the check the calendar-days leftover would have caught.

3. `adversary_record_problems` — objections enumerated one per line, each
   with a disposition. A `CHANGED` disposition must cite a path this diff
   actually touches, which is the one part of an adversary record a
   non-author can falsify mechanically.

4. `acceptance_observable_problems` — a closure names something a human can
   confirm after a real live session, citing a path that exists. The
   companion half — is the fixing commit actually IN the production
   checkout — cannot run in CI, which has no access to the box; it is
   `scripts/check_item_deployment.py` and it runs there.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
Stated here because a gate believed to cover more than it does is worse
than one nobody trusts.

  * Every check is DIFF-SCOPED. Nothing fires on an item this change does
    not file or close, or on a quantity this change does not touch. An
    existing item carrying no criteria is untouched by check 1 forever
    unless somebody closes it. That is deliberate: forcing a hundred-odd
    open items through a new schema in one change is how a good gate dies.
  * It cannot tell whether an adversary was actually consulted, whether
    the objections recorded are the strongest available ones, or whether a
    `REJECTED` reason is sound. It can tell that a `CHANGED` claim is
    false, and that is all it claims.
  * It cannot tell whether an acceptance observable is the RIGHT one.
  * Consumer completeness covers the quantities in `SHARED_QUANTITIES` and
    nothing else. An unregistered quantity is uncovered. The registry is
    the false-positive control and the coverage limit at the same time.
  * A shared quantity read through `getattr(obj, name_from_a_variable)` is
    invisible to an AST walk, so the derived consumer set is a lower bound.

WHY NOTHING HERE ABORTS ON ITS OWN INFRASTRUCTURE
-------------------------------------------------
The board-size check in `tests/test_status_board.py` raises when a shallow
clone yields no baseline, and a required check that fails for its own
reasons is itself a known problem on this desk. So `base_ref` deepens on
demand exactly as that helper does, and every check here returns NO
PROBLEMS when the base cannot be read. A baseline that could not be
fetched is not evidence that an obligation was dropped. The cost of that
choice is stated plainly: on a clone with no reachable base, all four
checks are silent.

Usage
-----
    scripts/definition_of_done.py            # every check against HEAD
    scripts/definition_of_done.py --json
    scripts/definition_of_done.py --consumers trading_sessions_held
                                             # the derived consumer set

Exit codes
----------
    0  no problems found, or no base commit to measure against
    1  at least one problem — the same text pytest fails with
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_MD = "docs/WORK.md"

# ---------------------------------------------------------------------------
# git reads
# ---------------------------------------------------------------------------


def _git(*args: str, repo: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo or REPO_ROOT), *args],
        capture_output=True, text=True, check=False,
    )


def base_ref(repo: Path | None = None) -> str | None:
    """The commit this change is measured against, or None when unknowable.

    Same derivation as `tests/test_status_board.py::_work_md_base_ref`, for
    the same reason: in CI both events check out a commit whose FIRST PARENT
    is main as it stands, so `HEAD^1` is "main before this change" for a
    pull-request run and for a push of the merge alike, measured against main
    at run time rather than a stale fork point.

    Item 132 (2026-09-18): the `tests` workflow's checkout used to default to
    depth 1, which left not just `HEAD^1` unresolvable but the earlier
    commits of a multi-commit PR branch unreadable even once the base was
    found — a trailer written two commits back on the branch never showed up
    in `git log base..HEAD`, and the failure said nothing about why. The
    workflow now passes `fetch-depth: 0` (`.github/workflows/test.yml`), so
    the FIRST attempt below is a real `merge-base` against `origin/main`, the
    same computation a local run does. The on-demand deepen-to-2 fetch below
    is kept only as a fallback for a checkout that for some other reason
    still ran shallow (a manual `workflow_dispatch`, a third-party fork of
    this workflow, a local shallow clone) — on a full-history checkout it
    never fires because `origin/main` is already reachable.

    The on-demand fetch fetches the exact checked-out SHA into an explicit
    destination ref, not a bare `--deepen=1`: a pull_request run's HEAD lives
    at `refs/remotes/pull/<N>/merge`, outside the remote's default fetch
    refspec (`refs/heads/*`), so a refspec-less deepen fetches nothing for it
    and silently leaves `HEAD^1` unresolvable on every such run — see the
    dated evidence in `tests/test_status_board.py::_work_md_base_ref`, which
    hit exactly this and shares the fix. Even that fallback only resolves the
    base commit; it does NOT recover a multi-commit branch's earlier commit
    messages, which is why `fetch-depth: 0` in the workflow is the real fix
    and this is a fallback for base resolution only, not a substitute for it.

    Duplicated rather than shared because that helper is inside a module
    `pytest` collects, and importing a test module from a script to reuse one
    twelve-line function is a worse coupling than the copy.
    """
    if os.environ.get("GITHUB_ACTIONS"):
        _git("fetch", "-q", "origin", "main", repo=repo)
        mb = _git("merge-base", "HEAD", "origin/main", repo=repo)
        if mb.returncode == 0 and mb.stdout.strip():
            return mb.stdout.strip()
        if _git("rev-parse", "--verify", "-q", "HEAD^1", repo=repo).returncode != 0:
            head = _git("rev-parse", "HEAD", repo=repo)
            if head.returncode == 0:
                sha = head.stdout.strip()
                _git("fetch", "-q", "--depth=2", "origin",
                     f"+{sha}:refs/ci-work-md-base-probe", repo=repo)
        r = _git("rev-parse", "--verify", "-q", "HEAD^1", repo=repo)
        return r.stdout.strip() or None
    _git("fetch", "-q", "origin", "main", repo=repo)
    r = _git("merge-base", "HEAD", "origin/main", repo=repo)
    return r.stdout.strip() or None


def is_shallow(repo: Path | None = None) -> bool:
    """Whether this checkout's history is truncated.

    Read by `read_scope_note` so a false-empty adversary/trailer record can
    be told apart from a genuinely absent one — see item 132.
    """
    r = _git("rev-parse", "--is-shallow-repository", repo=repo)
    return r.stdout.strip() == "true"


def read_scope_note(change: "Change | None", repo: Path | None = None) -> str:
    """One line naming exactly which commits a failing check just read.

    Item 132: every check below reads `commit_messages`, which is `git log
    base..HEAD` — commit messages ONLY. The pull request's own description
    box is never read by anything in this file. Before this note existed,
    a shallow or badly-based checkout produced an empty or wrong commit
    range and the resulting failure gave no way to tell "you forgot the
    trailer" from "the gate could not see the commit that has it" — costing
    real hours (see docs/WORK.md item 132). This is printed whenever there
    is at least one problem to report, so an author can tell which case
    they are in without opening this script.
    """
    if change is None:
        return ("no base commit could be resolved — every check below is "
                "silent, not passing; nothing was read")
    head = _git("rev-parse", "HEAD", repo=repo)
    head_sha = head.stdout.strip()[:12] if head.returncode == 0 else "HEAD"
    base_sha = change.base[:12] if change.base else change.base
    note = (f"read commit messages only, range {base_sha}..{head_sha} "
            f"(`git log {base_sha}..{head_sha}`) — the pull request "
            f"DESCRIPTION box is never read by this gate, only commit "
            f"messages count")
    if is_shallow(repo):
        note += ("; this checkout is SHALLOW, so an earlier commit on a "
                 "multi-commit branch may be missing from that range even "
                 "though the base above resolved — see item 132 and make "
                 "sure the workflow's checkout step uses `fetch-depth: 0`")
    return note


def file_at(ref: str, path: str, repo: Path | None = None) -> str | None:
    """A file's contents at `ref`, or None if it did not exist there."""
    r = _git("show", f"{ref}:{path}", repo=repo)
    return r.stdout if r.returncode == 0 else None


def changed_paths(ref: str, repo: Path | None = None) -> set[str]:
    r = _git("diff", "--name-only", f"{ref}...HEAD", repo=repo)
    return {p for p in r.stdout.split("\n") if p}


#: `@@ -12,3 +40,7 @@` — the post-change side of a unified-diff hunk.
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_line_ranges(ref: str, path: str,
                        repo: Path | None = None) -> list[tuple[int, int]]:
    """Line ranges this change touched in `path`, in POST-change numbering.

    Needed because "the file was touched" is not "the site was touched",
    and the difference is the whole reason the 2026-09-04 session-count fix
    left a leftover. `src/pipeline.py` is over eleven thousand lines; that
    change edited it, switched ONE of the sites that read the new counter,
    and a file-level test would have called every other site in the same
    file accounted for. Replaying the gate against commit cd7aec09 with
    file-level accounting reported no problem at all — which is how this
    was found before shipping rather than after.

    `-U0` so a hunk covers only the lines that actually differ. A pure
    deletion (`+N,0`) is recorded as the single line it collapsed to, so an
    enclosing function whose body was deleted still counts as touched.
    """
    r = _git("diff", "-U0", f"{ref}...HEAD", "--", path, repo=repo)
    out: list[tuple[int, int]] = []
    for line in r.stdout.splitlines():
        match = HUNK.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        out.append((start, start + max(count, 1) - 1))
    return out


#: A trailer line, for the unwrap below: `Key: value` at the start of a line.
#: Deliberately matches ANY key, not just this module's own — a continuation
#: line must never swallow the next trailer, whoever owns it.
_ANY_TRAILER_START = re.compile(r"^[ \t]*[A-Za-z][A-Za-z0-9-]*[ \t]*:", re.I)


def unwrap_trailers(messages: str) -> str:
    """Join a trailer's continuation lines onto the trailer itself.

    Every check in this module captures a trailer's value to END OF LINE, so a
    trailer written across several physical lines was silently truncated to its
    first line: an `Acceptance-observable:` wrapped at column 72 arrived as
    eight words with no path and failed, and because the gate reads EVERY
    occurrence in `git log base..HEAD`, no later commit could correct it. With
    force-push blocked, the only remedy was re-cutting the branch — which cost
    four pull requests on 2026-09-26 alone, none of them for anything wrong
    with the work.

    Wrapping is a typographic accident, not a claim about content, so it is
    normalised here rather than policed. The rule is git's own trailer
    convention: a line that is INDENTED and is not itself `Key:` continues the
    trailer above it. A blank line, an unindented line, or the start of another
    trailer ends it. Nothing else in the message is touched, and a message with
    no wrapped trailers comes back byte-identical.
    """
    out: list[str] = []
    in_trailer = False
    for line in (messages or "").splitlines():
        if _ANY_TRAILER_START.match(line):
            out.append(line)
            in_trailer = True
            continue
        if in_trailer and line.strip() and line[:1] in (" ", "\t"):
            out[-1] = out[-1].rstrip() + " " + line.strip()
            continue
        out.append(line)
        in_trailer = False
    return "\n".join(out) + ("\n" if (messages or "").endswith("\n") else "")


def commit_messages(ref: str, repo: Path | None = None) -> str:
    """Every commit message on this change, as one blob.

    A merge commit's own message is included. The declarations this module
    reads are trailers, and which of a change's commits carries them is not
    something a gate should have an opinion about. Wrapped trailers are joined
    by `unwrap_trailers` before any check sees them.
    """
    r = _git("log", "--format=%B%n", f"{ref}..HEAD", repo=repo)
    return unwrap_trailers(r.stdout)


@dataclass
class Change:
    """Everything the checks are allowed to look at, in one object.

    Constructed once from git so the checks are pure functions over it and
    can be exercised against a synthetic repository in tests.
    """

    base: str
    paths: set[str] = field(default_factory=set)
    messages: str = ""
    work_md_before: str | None = None
    work_md_after: str | None = None
    tree: Path = REPO_ROOT

    @classmethod
    def from_git(cls, repo: Path | None = None) -> "Change | None":
        repo = repo or REPO_ROOT
        base = base_ref(repo)
        if not base:
            return None
        after = (repo / WORK_MD)
        return cls(
            base=base,
            paths=changed_paths(base, repo),
            messages=commit_messages(base, repo),
            work_md_before=file_at(base, WORK_MD, repo),
            work_md_after=after.read_text() if after.exists() else None,
            tree=repo,
        )


# ---------------------------------------------------------------------------
# the board's own shapes
# ---------------------------------------------------------------------------

#: `**39(a). Title — status.**` — the heading form every item in
#: `docs/WORK.md` already uses. Verified against the file, not its prose.
ITEM_HEADING = re.compile(r"^\*\*(\d+)\.\s", re.M)

#: The line that retires an item. Editing it IS a closure whatever the
#: description says — the same signal `scripts/work_queue.py` uses.
RETIRED_LINE_PREFIX = "**Retired item numbers"

#: ONLY the leading comma-separated run of numbers on that line counts.
#: The rest of the line is prose, and the prose on the live line contains
#: a SECOND numbering scheme ("1, 2, 3 ... in the PM test gate" — the line
#: says in so many words that "the two schemes are separate, 3 is live in
#: this queue while retired in the gate") and half a dozen dates. A loose
#: `\b\d+\b` sweep over the whole line reads `2026`, `09` and `16` out of
#: "closed 2026-09-16" as retired item numbers, and any prose edit that
#: mentions a new number as a closure. Both were caught by
#: `tests/test_definition_of_done.py::
#: test_the_real_board_parses_into_items_and_retired_numbers` before this
#: shipped. Anchoring to the leading run is what keeps the false-positive
#: rate at zero.
RETIRED_RUN = re.compile(
    r"^\*\*Retired item numbers[^*]*\*\*\s*((?:\d+\s*,\s*)*\d+)\b")

#: A filed item's own completion criteria. One label, then one bullet per
#: criterion, checkbox-style so "met" is a one-character edit and a diff
#: shows it. The identifier is the bullet's ordinal within its item.
DONE_WHEN = re.compile(r"^\s*DONE WHEN:\s*$", re.M)
CRITERION = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*(.+?)\s*$", re.M)

TRAILER = r"^[ \t]*{key}[ \t]*:[ \t]*(.+?)[ \t]*$"


def trailer(messages: str, key: str) -> list[str]:
    """Every value given for a trailer key across this change's commits."""
    pattern = re.compile(TRAILER.format(key=re.escape(key)), re.I | re.M)
    return [m.group(1) for m in pattern.finditer(messages or "")]


def item_blocks(work_md: str | None) -> dict[str, str]:
    """Item number -> the text from its heading to the next item's heading."""
    if not work_md:
        return {}
    marks = [(m.group(1), m.start()) for m in ITEM_HEADING.finditer(work_md)]
    out: dict[str, str] = {}
    for i, (number, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(work_md)
        out[number] = work_md[start:end]
    return out


def criteria(block: str) -> list[tuple[int, bool, str]]:
    """`(ordinal, met, text)` for each criterion under this item's DONE WHEN.

    Reads the bullets that follow the label and stops at the first line that
    is neither a criterion bullet nor blank, so ordinary item prose below the
    criteria is not swept in.
    """
    match = DONE_WHEN.search(block)
    if not match:
        return []
    out: list[tuple[int, bool, str]] = []
    ordinal = 0
    for line in block[match.end():].splitlines():
        if not line.strip():
            continue
        bullet = CRITERION.match(line)
        if not bullet:
            break
        ordinal += 1
        out.append((ordinal, bullet.group(1).lower() == "x", bullet.group(2)))
    return out


def retired_numbers(work_md: str | None) -> set[str]:
    if not work_md:
        return set()
    line = next((l for l in work_md.splitlines()
                 if l.startswith(RETIRED_LINE_PREFIX)), "")
    match = RETIRED_RUN.match(line)
    if not match:
        return set()
    return {n.strip() for n in match.group(1).split(",") if n.strip()}


def items_closed(change: Change) -> set[str]:
    """Items this change retires: numbers newly on the retired line."""
    return (retired_numbers(change.work_md_after)
            - retired_numbers(change.work_md_before))


def items_filed(change: Change) -> set[str]:
    """Items this change files: headings that did not exist at the base."""
    return (set(item_blocks(change.work_md_after))
            - set(item_blocks(change.work_md_before)))


# ---------------------------------------------------------------------------
# CHECK 1 — declared halves
# ---------------------------------------------------------------------------

#: `Done-criteria-deferred: 18/2 -> item 63 (2026-09-18)`. A deferred
#: criterion must name where it went and when it was deferred, because an
#: obligation with no date is the silent limbo this check exists to remove.
DEFERRAL = re.compile(
    r"(\d+)\s*/\s*(\d+)\s*(?:->|→)\s*items?\s*#?(\d+)"
    r".*?(\d{4}-\d{2}-\d{2})", re.I)
MET = re.compile(r"(\d+)\s*/\s*(\d+)", re.I)


def declared_criteria_problems(change: Change) -> list[str]:
    """Items filed without criteria, and closures that drop one.

    Filing: a new item must carry a `DONE WHEN:` label and at least one
    criterion bullet. An item whose whole content is a question for the
    owner is exempt via `NO CRITERIA:` and a reason, because "he rules or he
    does not" has no half to leave behind — and the exemption is itself in
    the diff, which is the point.

    Closing: every criterion the item carried AT THE BASE COMMIT must appear
    in this change's `Done-criteria-met` or `Done-criteria-deferred`
    trailers. A deferral must name a target item that exists in the board
    after this change and a date. An item that carried no criteria at the
    base is not held to this — that is the grandfathering, and it is why
    this check has almost no bite on today's board.
    """
    problems: list[str] = []
    after = item_blocks(change.work_md_after)
    before = item_blocks(change.work_md_before)

    for number in sorted(items_filed(change), key=int):
        block = after[number]
        if re.search(r"^\s*NO CRITERIA:\s*\S.{15,}", block, re.M):
            continue
        if not criteria(block):
            problems.append(
                f"board item {number} is filed by this change with no "
                f"completion criteria. Add a `DONE WHEN:` line to its block "
                f"in {WORK_MD} followed by one `- [ ] ...` bullet per half "
                f"of the work, so closing it later has something to verify "
                f"against. If the item is a question only the owner can "
                f"answer, say so with a `NO CRITERIA: <reason>` line instead."
            )

    met = {(m.group(1), m.group(2)) for v in trailer(change.messages, "Done-criteria-met")
           for m in MET.finditer(v)}
    deferred = {(m.group(1), m.group(2)): (m.group(3), m.group(4))
                for v in trailer(change.messages, "Done-criteria-deferred")
                for m in DEFERRAL.finditer(v)}

    for number in sorted(items_closed(change), key=int):
        declared = criteria(before.get(number, ""))
        if not declared:
            continue
        for ordinal, _was_met, text in declared:
            key = (number, str(ordinal))
            if key in met:
                continue
            if key in deferred:
                target, _date = deferred[key]
                if target not in after:
                    problems.append(
                        f"board item {number} criterion {ordinal} "
                        f"({text[:60]!r}) is deferred onto item {target}, "
                        f"which does not exist in {WORK_MD} after this "
                        f"change. File the item, or account for the "
                        f"criterion as met."
                    )
                continue
            problems.append(
                f"board item {number} is retired by this change but "
                f"criterion {ordinal} ({text[:60]!r}), declared when the "
                f"item was filed, is accounted for neither way. Add "
                f"`Done-criteria-met: {number}/{ordinal}` to a commit "
                f"message if it shipped, or "
                f"`Done-criteria-deferred: {number}/{ordinal} -> item N "
                f"(YYYY-MM-DD)` naming the item that now carries it."
            )
    return problems


# ---------------------------------------------------------------------------
# CHECK 2 — consumer completeness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RivalShape:
    """A site that computes the quantity the WRONG way, by arithmetic shape.

    WHY THIS EXISTS, and it is the most important paragraph in this file.
    Walking the AST for a symbol finds every site that CALLS the sanctioned
    definition. It cannot find a site that computes the same quantity by
    hand and never calls anything — and that is exactly the measured
    defect. Replayed against cd7aec09, the 2026-09-04 commit that added
    `trading_sessions_held`, a symbol-only version of this check found one
    consumer, the one the commit had just written, and reported nothing.
    The three sites left on `(today - entry_date).days` were invisible
    because they did not reference the new function at all. A check that
    only finds callers would have blessed the commit that created the
    leftover it exists to prevent.

    So a registered quantity may also carry the shape of its own wrong
    version. `tests/test_one_definition_guard.py` is the precedent and says
    why the AST and not grep: *"What they have in common is the
    ARITHMETIC — and arithmetic is exactly what an AST can see."*

    Kept narrow on purpose. `target` is a regex the ASSIGNMENT TARGET's
    name must match, and `attribute` must appear in the assigned value.
    `(event_date - today).days` in `src/data/event_calendar.py` is a
    perfectly good calendar-day count of a different quantity, and a
    matcher that flagged it would be the false positive that gets this
    gate deleted.
    """

    target: str
    attribute: str
    describe: str


@dataclass(frozen=True)
class SharedQuantity:
    """A quantity more than one site reads, and where it is defined.

    `symbol` is what an AST walk looks for: a function name, a
    module-level constant, or a settings key read through `.get("...")`.
    `defined_in` is the file that owns it — a change to any other file is
    not a change to the definition and does not arm this check.
    `rival`, when present, is the shape of the same quantity computed by
    hand; see `RivalShape`.
    """

    name: str
    symbol: str
    defined_in: str
    why: str
    rival: RivalShape | None = None


#: Registered shared quantities. Each one is here because a change to it
#: has more than one reader and a partial switch-over is silent.
#:
#: DELIBERATELY SHORT. The registry is what keeps the false-positive rate
#: at zero across the last fifty commits on main, and it is equally the
#: coverage limit — an unregistered quantity is not checked at all. Adding
#: an entry is one line and is expected; the honest trade is that somebody
#: has to add it.
SHARED_QUANTITIES: tuple[SharedQuantity, ...] = (
    SharedQuantity(
        name="holding time, in trading sessions",
        symbol="trading_sessions_held",
        defined_in="src/trading_calendar.py",
        why=("added 2026-09-04 to replace a calendar-day count; exactly one "
             "of its consumers was switched over and `src/pipeline.py` still "
             "divides a calendar-day `days_held` by a session horizon"),
        rival=RivalShape(
            target=r"^days_held$",
            attribute="days",
            describe="a holding time assigned from a calendar-day "
                     "`(a - b).days` subtraction instead of the "
                     "weekend-aware session counter",
        ),
    ),
    SharedQuantity(
        name="unrealised P&L percent",
        symbol="unrealized_pnl_pct",
        defined_in="src/risk/metrics.py",
        why=("one definition that returns None when unknowable; the guard it "
             "replaced silently returned None for every short, so a reader "
             "left on the old shape shows an empty short book"),
    ),
    SharedQuantity(
        name="evidence-coverage verdict",
        symbol="evaluate",
        defined_in="src/evidence_gate.py",
        why=("the refusal that stops the desk deciding on evidence that "
             "never arrived; a seat status added for one reader and not the "
             "others is how the #428 split failed to reach Risk"),
    ),
)


def _module_name(path: str) -> str:
    return Path(path).stem


def derive_consumers(quantity: SharedQuantity,
                     tree: Path) -> dict[str, tuple[int, int]]:
    """Every site in `src/` that reads this quantity, by AST walk.

    Maps `file.py:enclosing_function` to the enclosing definition's line
    range, so a change can be tested against the SITE rather than against
    the file it happens to live in. The definition's own file is excluded —
    a definition is not a consumer of itself.

    DERIVED, NEVER DECLARED. The whole point of this check is that the
    author does not get to supply the list of things they should have
    looked at; a hand-written enumeration is another artefact that goes
    stale, and going stale silently is the defect class. The cost is that a
    dynamic read (`getattr(mod, name)`) is invisible, so this is a lower
    bound on the true consumer set and the check can under-fire. It cannot
    over-fire on a name that is never read.
    """
    out: dict[str, tuple[int, int]] = {}
    for py in sorted((tree / "src").rglob("*.py")):
        rel = py.relative_to(tree).as_posix()
        if rel == quantity.defined_in:
            continue
        try:
            source = py.read_text(encoding="utf-8")
            module = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        total = len(source.splitlines()) or 1
        stack: list[tuple[str, int, int]] = []

        def visit(node: ast.AST) -> None:
            named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef))
            if named:
                stack.append((
                    node.name,  # type: ignore[attr-defined]
                    node.lineno,  # type: ignore[attr-defined]
                    getattr(node, "end_lineno", None) or total,
                ))
            if isinstance(node, (ast.Name, ast.Attribute)):
                found = (getattr(node, "id", None)
                         or getattr(node, "attr", None))
                if found == quantity.symbol:
                    if stack:
                        name, start, end = stack[-1]
                    else:
                        # A module-level read: the whole module is the site,
                        # because there is no narrower thing to point at.
                        name, start, end = "<module>", 1, total
                    out[f"{rel}:{name}"] = (start, end)
            for child in ast.iter_child_nodes(node):
                visit(child)
            if named:
                stack.pop()

        visit(module)
    return out


def derive_rivals(quantity: SharedQuantity,
                  tree: Path) -> dict[str, tuple[int, int]]:
    """Sites in `src/` computing this quantity by hand, by arithmetic shape.

    Same return shape as `derive_consumers` — site to enclosing line range —
    so the two sets are accounted for identically. Empty when the quantity
    registers no rival shape.
    """
    if quantity.rival is None:
        return {}
    target = re.compile(quantity.rival.target)
    out: dict[str, tuple[int, int]] = {}
    for py in sorted((tree / "src").rglob("*.py")):
        rel = py.relative_to(tree).as_posix()
        if rel == quantity.defined_in:
            continue
        try:
            source = py.read_text(encoding="utf-8")
            module = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        total = len(source.splitlines()) or 1
        stack: list[tuple[str, int, int]] = []

        def visit(node: ast.AST) -> None:
            named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef))
            if named:
                stack.append((
                    node.name,  # type: ignore[attr-defined]
                    node.lineno,  # type: ignore[attr-defined]
                    getattr(node, "end_lineno", None) or total,
                ))
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                names = {getattr(t, "id", None) or getattr(t, "attr", None)
                         for t in targets} - {None}
                value = getattr(node, "value", None)
                if value is not None and any(
                        target.match(n) for n in names):  # type: ignore[arg-type]
                    attrs = {a.attr for a in ast.walk(value)
                             if isinstance(a, ast.Attribute)}
                    if quantity.rival.attribute in attrs:
                        if stack:
                            name, start, end = stack[-1]
                        else:
                            name, start, end = "<module>", 1, total
                        out[f"{rel}:{name}"] = (start, end)
            for child in ast.iter_child_nodes(node):
                visit(child)
            if named:
                stack.pop()

        visit(module)
    return out


def _touches_definition(change: Change, quantity: SharedQuantity) -> bool:
    """Did this change edit the definition of `symbol`, not just its file?

    A file-level test would arm the check on every unrelated edit to
    `src/pipeline.py`-sized modules, which is how a gate earns a
    false-positive rate and then gets routed around. So this compares the
    symbol's own source segment before and after.
    """
    if quantity.defined_in not in change.paths:
        return False
    before = file_at(change.base, quantity.defined_in, change.tree)
    after_path = change.tree / quantity.defined_in
    after = after_path.read_text(encoding="utf-8") if after_path.exists() else None
    return _segment(before, quantity.symbol) != _segment(after, quantity.symbol)


def _segment(source: str | None, symbol: str) -> str | None:
    """The source text of `symbol`'s definition, or None."""
    if not source:
        return None
    try:
        module = ast.parse(source)
    except SyntaxError:
        return source
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.name == symbol:
            return ast.get_source_segment(source, node)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            for target in targets:
                if getattr(target, "id", None) == symbol:
                    return ast.get_source_segment(source, node)
    return None


def consumer_completeness_problems(change: Change) -> list[str]:
    """A registered quantity changed without every reader accounted for.

    Arms only when the change edits the definition itself. Then each
    derived consumer must be either touched by this change AT THE SITE —
    the diff's own line ranges overlapping the enclosing function, not
    merely the file — or listed in a `Consumers-unchanged:` trailer with a
    reason. The list is checked in BOTH directions: an entry that is not in
    the derived set fails too, because a list that does not match the tree
    is the artefact this check exists to stop anybody relying on.

    Site granularity is not a refinement, it is the check. Replayed
    against cd7aec09 — the 2026-09-04 commit that added
    `trading_sessions_held` and switched one of its readers — a
    file-granularity version of this reported nothing, because the
    untouched readers were in `src/pipeline.py` and so was the edit.
    """
    problems: list[str] = []
    declared = [v for v in trailer(change.messages, "Consumers-unchanged")]
    declared_sites = {v.split("—")[0].split("--")[0].strip().rstrip(",")
                      for v in declared}
    declared_sites = {s for s in declared_sites if s}

    def _touched_at_site(site: str, span: tuple[int, int]) -> bool:
        path = site.split(":")[0]
        if path not in change.paths:
            return False
        start, end = span
        return any(hunk_start <= end and hunk_end >= start
                   for hunk_start, hunk_end
                   in changed_line_ranges(change.base, path, change.tree))

    for quantity in SHARED_QUANTITIES:
        if not _touches_definition(change, quantity):
            continue
        consumers = dict(derive_consumers(quantity, change.tree))
        rivals = derive_rivals(quantity, change.tree)
        consumers.update(rivals)
        unaccounted = sorted(
            site for site, span in consumers.items()
            if not _touched_at_site(site, span)
            and site not in declared_sites
        )
        if unaccounted:
            rival_note = ""
            hand_rolled = [s for s in unaccounted if s in rivals]
            if hand_rolled and quantity.rival is not None:
                rival_note = (
                    f" Of those, {', '.join(hand_rolled)} do not call it at "
                    f"all — they are {quantity.rival.describe}, which is the "
                    f"same quantity computed by hand and is what a "
                    f"caller-only search cannot see."
                )
            problems.append(
                f"this change edits {quantity.symbol} "
                f"({quantity.name}) in {quantity.defined_in}, and "
                f"{len(unaccounted)} site(s) that read it are neither "
                f"changed nor accounted for: {', '.join(unaccounted)}.{rival_note} "
                f"Either update them in this change, or add one "
                f"`Consumers-unchanged: <site> — <why it is correct "
                f"unchanged>` trailer per site. The list is derived from "
                f"the tree, so it cannot be satisfied by writing a shorter "
                f"one. Why this quantity is registered: {quantity.why}."
            )
        stale = sorted(
            site for site in declared_sites
            if ":" in site and site not in consumers
            and site.split(":")[0] in {c.split(":")[0] for c in consumers}
        )
        if stale:
            problems.append(
                f"`Consumers-unchanged` names {', '.join(stale)}, which no "
                f"longer reads {quantity.symbol}. The declaration is stale "
                f"or was never true; the derived set is "
                f"{', '.join(sorted(consumers)) or '(empty)'}."
            )
    return problems


# ---------------------------------------------------------------------------
# CHECK 3 — a falsifiable adversary record
# ---------------------------------------------------------------------------

OBJECTION = re.compile(r"^[ \t]*Objection-(\d+)[ \t]*:[ \t]*(.+?)[ \t]*$",
                       re.I | re.M)
RESPONSE = re.compile(
    r"^[ \t]*Response-(\d+)[ \t]*:[ \t]*(CHANGED|REJECTED)\b[ \t]*(.*?)[ \t]*$",
    re.I | re.M)

#: An objection shorter than this is a label, not an argument. Low on
#: purpose — the check below is about structure and falsifiability, and a
#: word count high enough to judge an argument would be met with padding.
MIN_OBJECTION_WORDS = 8

#: A rejection carries the whole weight of the decision not to act, so it
#: is held to more than an objection is.
MIN_REJECTION_WORDS = 12

#: At least this many objections on a closure. One objection is what an
#: author writes when the exercise is a formality.
MIN_OBJECTIONS = 2

PATH_IN_TEXT = re.compile(r"\b((?:src|scripts|tests|docs|config)/[\w./-]+)")


def _words(text: str) -> int:
    return len([w for w in re.split(r"\s+", text or "") if w])


def adversary_record_problems(change: Change) -> list[str]:
    """A closure whose adversary record cannot be falsified by a reader.

    Required on any change that retires a board item:

        Objection-1: <at least 8 words of actual argument>
        Response-1: CHANGED src/pipeline.py — <what moved>
        Objection-2: ...
        Response-2: REJECTED — <at least 12 words of reasoning>

    WHAT A READER CAN FALSIFY, and therefore what this enforces:
      * that objections are enumerated at all, at least two of them;
      * that each has exactly one disposition, and each disposition an
        objection;
      * that no two objections are the same sentence, which is how a count
        gets padded;
      * that a `CHANGED` disposition cites a path THIS DIFF ACTUALLY
        TOUCHES. That is the load-bearing one. "I changed something in
        response" is the claim most worth making falsely and the only one a
        machine can catch red-handed.

    WHAT IT CANNOT: whether an adversary was consulted, whether these were
    the strongest objections available, or whether a `REJECTED` reason is
    any good. A determined author can still write two plausible objections
    and reject both at length. This check makes that a deliberate
    fabrication with a specific shape rather than the six words that pass
    today.
    """
    closed = items_closed(change)
    if not closed:
        return []
    numbers = ", ".join(sorted(closed, key=int))
    problems: list[str] = []
    objections = {m.group(1): m.group(2) for m in OBJECTION.finditer(change.messages)}
    responses = {m.group(1): (m.group(2).upper(), m.group(3))
                 for m in RESPONSE.finditer(change.messages)}

    if len(objections) < MIN_OBJECTIONS:
        problems.append(
            f"this change retires board item(s) {numbers} but records "
            f"{len(objections)} adversary objection(s); at least "
            f"{MIN_OBJECTIONS} are required, each as "
            f"`Objection-N: <argument>` with a matching "
            f"`Response-N: CHANGED <path> — ...` or "
            f"`Response-N: REJECTED — ...` in a commit message."
        )

    for ordinal, text in sorted(objections.items()):
        if _words(text) < MIN_OBJECTION_WORDS:
            problems.append(
                f"Objection-{ordinal} is {_words(text)} words "
                f"({text[:40]!r}); an objection under "
                f"{MIN_OBJECTION_WORDS} words is a label, not an argument."
            )
        if ordinal not in responses:
            problems.append(
                f"Objection-{ordinal} has no `Response-{ordinal}:`. Every "
                f"objection is answered by what changed or by a reasoned "
                f"rejection; an unanswered one is the half that gets lost."
            )
    for ordinal in sorted(set(responses) - set(objections)):
        problems.append(
            f"`Response-{ordinal}` answers an objection that was never "
            f"stated. Add `Objection-{ordinal}:`."
        )

    seen: dict[str, str] = {}
    for ordinal, text in sorted(objections.items()):
        key = re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
        if key in seen:
            problems.append(
                f"Objection-{ordinal} repeats Objection-{seen[key]} "
                f"verbatim. Two copies of one objection is one objection."
            )
        seen[key] = ordinal

    for ordinal, (disposition, detail) in sorted(responses.items()):
        if disposition == "CHANGED":
            cited = set(PATH_IN_TEXT.findall(detail))
            if not cited:
                problems.append(
                    f"Response-{ordinal} says CHANGED but cites no path. "
                    f"Name the file this objection moved, so a reader can "
                    f"check the diff against the claim."
                )
            elif not (cited & change.paths):
                problems.append(
                    f"Response-{ordinal} says CHANGED and cites "
                    f"{', '.join(sorted(cited))}, but this change touches "
                    f"none of those files. Either the claim is wrong or the "
                    f"path is; the diff touches "
                    f"{len(change.paths)} file(s)."
                )
        elif disposition == "REJECTED" and _words(detail) < MIN_REJECTION_WORDS:
            problems.append(
                f"Response-{ordinal} rejects an objection in "
                f"{_words(detail)} words. A rejection carries the whole "
                f"decision not to act; give it at least "
                f"{MIN_REJECTION_WORDS}."
            )
    return problems


# ---------------------------------------------------------------------------
# CHECK 4 — deployment verified, not claimed (the half CI can see)
# ---------------------------------------------------------------------------

MIN_OBSERVABLE_WORDS = 10


def acceptance_observable_problems(change: Change) -> list[str]:
    """A closure with nothing a human can confirm after a live session.

    A prompt or behaviour change cannot be validated offline on this desk —
    the rehearsal rig is code, not prompt, and says so. So a closure names
    an ACCEPTANCE OBSERVABLE: a specific thing somebody can look at after a
    real session and say yes or no to.

        Acceptance-observable: the next evening report shows a sessions-held
        figure for TSLA that does not jump by 2 over a weekend — see
        src/pipeline.py

    Two things are mechanically checkable and are all that is enforced:
    it is at least ten words, and it cites a path that EXISTS in the tree
    after this change, so the observable is anchored to something real
    rather than to a surface nobody can find. Whether it is the RIGHT
    observable is not checkable and is not claimed.

    The other half of this check — is the fixing commit actually in the
    production checkout at `/home/qamc/quant-agent` — cannot run here. CI
    has no access to the box. It is `scripts/check_item_deployment.py`,
    which runs there alongside the two existing drift checks and answers
    the per-item question neither of them does.
    """
    closed = items_closed(change)
    if not closed:
        return []
    values = trailer(change.messages, "Acceptance-observable")
    numbers = ", ".join(sorted(closed, key=int))
    if not values:
        return [
            f"this change retires board item(s) {numbers} with no "
            f"`Acceptance-observable:` trailer. Name one specific thing "
            f"somebody can confirm after a real live session, citing a path "
            f"in this repository. Nothing on this desk validates a "
            f"behaviour change offline."
        ]
    problems: list[str] = []
    for value in values:
        if _words(value) < MIN_OBSERVABLE_WORDS:
            problems.append(
                f"`Acceptance-observable: {value[:50]}` is {_words(value)} "
                f"words. Under {MIN_OBSERVABLE_WORDS} it names a feeling, "
                f"not an observation."
            )
        cited = [p for p in PATH_IN_TEXT.findall(value)]
        if not cited:
            problems.append(
                f"`Acceptance-observable: {value[:50]}` cites no path. Point "
                f"at the code or document that produces the thing to be "
                f"observed."
            )
        else:
            missing = [p for p in cited if not (change.tree / p).exists()]
            if missing:
                problems.append(
                    f"`Acceptance-observable` cites "
                    f"{', '.join(missing)}, which does not exist after this "
                    f"change."
                )
    return problems


# ---------------------------------------------------------------------------
# all four, and the CLI
# ---------------------------------------------------------------------------

CHECKS = {
    "declared_criteria": declared_criteria_problems,
    "consumer_completeness": consumer_completeness_problems,
    "adversary_record": adversary_record_problems,
    "acceptance_observable": acceptance_observable_problems,
}


def run_all(change: Change | None) -> dict[str, list[str]]:
    """Every check. An unreadable base is no problems, never a failure."""
    if change is None:
        return {name: [] for name in CHECKS}
    return {name: fn(change) for name, fn in CHECKS.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--consumers", metavar="SYMBOL",
                        help="print the derived consumer set for a "
                             "registered quantity and exit")
    args = parser.parse_args(argv)

    if args.consumers:
        match = next((q for q in SHARED_QUANTITIES
                      if q.symbol == args.consumers), None)
        if match is None:
            print(f"{args.consumers} is not a registered shared quantity. "
                  f"Registered: "
                  f"{', '.join(q.symbol for q in SHARED_QUANTITIES)}",
                  file=sys.stderr)
            return 1
        for site in sorted(derive_consumers(match, REPO_ROOT)):
            print(site)
        return 0

    change = Change.from_git()
    results = run_all(change)
    failing = any(results.values())
    if args.json:
        payload = {"base": change.base if change else None,
                   "problems": results}
        if failing:
            payload["read_scope"] = read_scope_note(change)
        print(json.dumps(payload, indent=2))
    else:
        if change is None:
            print("no base commit to measure against; every check is silent")
        for name, problems in results.items():
            print(f"{name}: {'OK' if not problems else f'{len(problems)} problem(s)'}")
            for problem in problems:
                print(f"  - {problem}")
        if failing:
            print(f"\n{read_scope_note(change)}")
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
