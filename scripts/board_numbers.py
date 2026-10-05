#!/usr/bin/env python3
"""Everywhere a board item NUMBER is taken from, and everywhere it must never
collide. One module, reused by a blocking test, an advisory GitHub check, and
the script an agent runs to claim a number, so the three can never quietly
disagree about what "already taken" means.

THE PROBLEM THIS CLOSES. Item numbers in `docs/WORK.md` were allocated by an
agent reading the file, taking the next unused number, and writing it. With
several agents building in parallel, THREE collisions happened in about an
hour on 2026-09-29/30 — item 188 was claimed by two branches, then item 189
by two more. Each agent had correctly checked the live board, the
retired-numbers line, and open pull requests; the check simply could not win
a race, because a number is claimed on a branch long before it reaches this
file. The retired-numbers line itself carried a standing warning that this
was happening and that warnings do not fix it. This module is the mechanical
fix: read the SAME three sources everywhere a number is checked or claimed.

What can and cannot be caught, and why
---------------------------------------
Two of the three sources this module reads are on disk in THIS checkout and
are therefore always current as of whatever commit is being tested:

  * the live item headings in `docs/WORK.md` (`**N. ...**`), via the exact
    regex `scripts/status_board.py` uses to parse the board — one parser, so
    a heading this module counts is a heading the board itself would show;
  * the retired-numbers line, parsed via `scripts/resolve_doc_conflict.py
    ::parse_retired` — the same function the merge driver uses, so "retired"
    here can never mean something different than what a merge just unioned.

A pull request's own CI run always sees BOTH of those exactly as they stand
in whatever `docs/WORK.md` that run is testing. For a `pull_request`-
triggered run, GitHub tests an ephemeral merge of the PR branch onto the
base branch's tip as of when the run started — so a duplicate heading or a
retired-number reuse that is already true against current `main` at RUN TIME
is always caught, loudly, by a plain read of the file already checked out.
No network call is needed for either of these, and neither can be flaky.

The third source — every OTHER currently open pull request's own claimed
numbers — cannot be read this way. It requires asking GitHub what else is
open right now, which is a network call: it can rate-limit, time out, or
simply be stale the instant after it returns, because another PR can merge
between the read and the moment THIS one merges. A required check that
depends on that read would turn an ordinary GitHub hiccup into a blocked
merge queue, and a stale-the-instant-it-returns advisory into a false sense
of safety if it were trusted as gospel. So this half is ADVISORY ONLY: it is
read best-effort (see `src.inflight.read_open_pull_requests`, the same
credential-free path `scripts/work_queue.py` already uses) and a failed read
degrades to "could not check", never to "nothing else is claimed".

Combining all three is exactly right for the POSITIVE half — the script an
agent runs to pick a number wants the best guess available, open-PR claims
included, because being wrong there just means a wasted re-run of the
script, not a false pass on a required check.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: The exact heading shape `scripts/status_board.py` parses EVERY numbered
#: item with (`_ITEM_OPEN_RE`, the parser behind `_parse_numbered_items` —
#: the board's real, general item parser) — imported, never re-typed, so a
#: heading counted here is a heading the board itself would show as one
#: item. Deliberately NOT `_QUEUE_ITEM_RE`: that stricter sibling requires
#: the whole heading (bold span and all trailing prose) to fit on one
#: physical line and end the line at the closing `**`, which most real
#: items do not — their body text continues on the same line right after
#: the bold title. `_ITEM_OPEN_RE` only requires the number to open the
#: line, which is the actual, universal convention.
def _load_queue_item_re():
    from scripts.status_board import _ITEM_OPEN_RE
    return _ITEM_OPEN_RE


#: The retired-numbers bullet-line parser the merge driver itself uses
#: (`scripts/resolve_doc_conflict.py::parse_retired_lines`) — imported, never
#: re-typed, so "retired" here can never disagree with what a merge unions.
#: As of 2026-09-30 the retired list is APPEND-ONLY: one `- retired <scheme>:
#: N, N, ...` line per closure (or batch of closures in one change), never an
#: edit to an existing line — see that module's docstring for why (every
#: closure used to edit the SAME shared line, which collided on every merge,
#: including on GitHub's own squash-merge, which never runs the local merge
#: driver at all).
def _load_parse_retired():
    from scripts.resolve_doc_conflict import parse_retired_lines, Refusal
    return parse_retired_lines, Refusal


def _load_pm_gate_markers():
    """The PM test gate's own heading and close marker
    (`scripts/status_board.py`'s `_PM_GATE_HEADING` / `_PM_GATE_STOP`),
    imported rather than re-typed. The gate numbers 1-8 and the funnel-queue
    numbers are two DELIBERATELY SEPARATE schemes (the retired-numbers
    line's own prose says so: "3 is retired in BOTH"), so a live gate item
    must never be compared against a live queue item for a number
    collision. The gate is empty as of 2026-09-14, but this stays correct if
    it is ever reopened."""
    from scripts.status_board import _PM_GATE_HEADING, _PM_GATE_STOP
    return _PM_GATE_HEADING, _PM_GATE_STOP


def _drop_pm_gate_section(work_md_text: str) -> str:
    heading, stop = _load_pm_gate_markers()
    start = work_md_text.find(heading)
    if start == -1:
        return work_md_text
    end = work_md_text.find(stop, start)
    if end == -1:
        return work_md_text
    end += len(stop)
    return work_md_text[:start] + work_md_text[end:]


_RETIRED_LINE_PREFIX = "**Retired item numbers"
#: The append-only bullet-line shape (`scripts/resolve_doc_conflict.py
#: ::_RETIRED_BULLET_RE`), duplicated here ONLY as a cheap pre-filter for
#: "does this file have any retired lines at all" — the actual parsing of
#: matched lines always goes through the imported `parse_retired_lines`.
_RETIRED_BULLET_PREFIX_RE = re.compile(r"^-\s+retired\s+(?:queue|gate):", re.I)


def live_item_numbers(work_md_text: str) -> list[int]:
    """Every number currently heading a `**N. ...**` item in the funnel
    queue (the PM test gate's own separate numbering scheme is excluded —
    see `_load_pm_gate_markers`), in the order they appear. A number
    appearing twice is returned twice — callers that care about duplicates
    check that themselves; silently deduping here would hide the exact
    defect this module exists to catch.
    """
    item_open_re = _load_queue_item_re()
    text = _drop_pm_gate_section(work_md_text)
    out = []
    for line in text.splitlines():
        m = item_open_re.match(line.strip())
        if m:
            out.append(int(m.group(1)))
    return out


@dataclass
class RetiredNumbers:
    queue: list[int] = field(default_factory=list)
    gate: list[int] = field(default_factory=list)
    error: str | None = None

    @property
    def all_queue(self) -> set[int]:
        return set(self.queue)


def retired_item_numbers(work_md_text: str) -> RetiredNumbers:
    """The two retired-number lists, read the same way the merge driver
    reads them — every `- retired queue: ...` / `- retired gate: ...` bullet
    line anywhere in the file, unioned. `error` is set (lists empty) when
    the explanatory header line is missing entirely, or when a line that
    looks like a bullet cannot be parsed — a caller must treat that as
    "unknown", never as "nothing is retired".
    """
    has_header = any(l.startswith(_RETIRED_LINE_PREFIX)
                      for l in work_md_text.splitlines())
    if not has_header:
        return RetiredNumbers(error=f"no '{_RETIRED_LINE_PREFIX}' line found")
    bullet_lines = [l for l in work_md_text.splitlines()
                    if _RETIRED_BULLET_PREFIX_RE.match(l.strip())]
    if not bullet_lines:
        return RetiredNumbers(
            error="the retired-numbers header is present but no "
                  "'- retired queue: ...' / '- retired gate: ...' line "
                  "follows it")
    parse_retired_lines, Refusal = _load_parse_retired()
    try:
        queue, gate = parse_retired_lines(bullet_lines)
    except Refusal as exc:
        return RetiredNumbers(error=str(exc))
    return RetiredNumbers(queue=queue, gate=gate)


def duplicate_live_numbers(work_md_text: str) -> list[int]:
    """Numbers that head more than one item on the board right now — the
    exact shape of the 2026-09-29/30 collisions once two branches both land.
    Sorted, each once, regardless of how many times it repeats."""
    nums = live_item_numbers(work_md_text)
    seen: set[int] = set()
    dupes: set[int] = set()
    for n in nums:
        if n in seen:
            dupes.add(n)
        seen.add(n)
    return sorted(dupes)


def live_numbers_that_are_retired(work_md_text: str) -> list[int]:
    """Live item numbers that also appear on the queue side of the
    retired-numbers line — a reused number, whether reused on purpose or by
    a race. Returns [] (never a false positive) when the retired line could
    not be parsed; that failure is reported separately by the caller."""
    retired = retired_item_numbers(work_md_text)
    if retired.error:
        return []
    live = set(live_item_numbers(work_md_text))
    return sorted(live & retired.all_queue)


# ---------------------------------------------------------------------------
# The advisory half: what other open pull requests claim right now
# ---------------------------------------------------------------------------

#: Matches an ADDED heading line in a unified diff patch, e.g.
#: `+**188. Some new item.**` — deliberately reuses the same numeral+dot
#: shape as the board's own heading regex rather than a hand-rolled one.
_PATCH_ADDED_ITEM_RE = re.compile(r"^\+\*\*(?:~~)?(\d+)\.\s+")


def claims_from_patch(patch: str) -> set[int]:
    """Item numbers a unified diff patch of `docs/WORK.md` ADDS. Only `+`
    lines count: a PR that merely touches an existing item's body without
    adding a new heading claims nothing."""
    return {int(m.group(1)) for line in patch.splitlines()
            if (m := _PATCH_ADDED_ITEM_RE.match(line))}


@dataclass
class OpenPrClaims:
    #: pr number -> set of item numbers that PR's diff adds.
    by_pr: dict[int, set[int]] = field(default_factory=dict)
    #: Set when the read failed outright (no PRs could be listed at all).
    #: None means the read succeeded, even if some individual PRs' file
    #: diffs could not be fetched (those are simply absent from `by_pr`).
    problem: str | None = None

    @property
    def all_claimed(self) -> set[int]:
        out: set[int] = set()
        for nums in self.by_pr.values():
            out |= nums
        return out


def read_open_pr_claims(fetch=None, repo: str | None = None) -> OpenPrClaims:
    """Every open pull request's newly-added `docs/WORK.md` item numbers,
    read credential-free off GitHub via `src.inflight` — the same path
    `scripts/work_queue.py` already uses for the adversary-line check.
    Never raises; a failed read comes back as `problem`, never as an empty
    (and therefore falsely reassuring) claim map."""
    kwargs = {}
    if fetch is not None:
        kwargs["fetch"] = fetch
    if repo is not None:
        kwargs["repo"] = repo
    try:
        # Imported inside the try: a missing dependency must come back as a
        # `problem`, not as a traceback (the docstring's "never raises").
        from src.inflight import read_open_pull_requests

        prs, problem = read_open_pull_requests(**kwargs)
    except Exception as exc:  # noqa: BLE001 - this must never raise into a caller
        return OpenPrClaims(problem=f"GitHub could not be read ({exc!r})")
    if problem:
        return OpenPrClaims(problem=problem)
    by_pr: dict[int, set[int]] = {}
    for pr in prs:
        if pr.work_md_patch:
            claimed = claims_from_patch(pr.work_md_patch)
            if claimed:
                by_pr[pr.number] = claimed
    return OpenPrClaims(by_pr=by_pr)


# ---------------------------------------------------------------------------
# The positive half: the next genuinely free number
# ---------------------------------------------------------------------------

@dataclass
class NextNumberResult:
    next_number: int
    highest_known: int
    checked_open_prs: bool
    open_pr_problem: str | None = None


@dataclass
class RefBoard:
    """`docs/WORK.md` as the SHARED copy of the board has it, or why not.

    THE FAILURE THIS CLOSES. The board is shared state: every agent on this
    box allocates item numbers out of one sequence. The working tree is NOT
    shared state — the main development checkout is shared between sessions
    and nobody pulls it, so it sits arbitrarily far behind. On 2026-10-01 it
    was 120 commits behind and two agents working in parallel were both
    handed 222, for unrelated defects, because the number was already taken
    on `main` before either of them asked and nothing in the check could see
    it. The open-pull-request half of the check could not help: that half
    looks FORWARD at numbers claimed on branches, and this number had
    already landed.

    So the authoritative copy of a shared file is the shared ref, not the
    local one. Deliberately `git show` and nothing else: NO `git fetch`, so
    this stays cheap and never reaches the network. A stale `origin/main`
    is still enormously better than a stale working tree, because it is
    advanced by every `git fetch` anything on the box runs, while the
    working tree only moves when somebody pulls it on purpose.

    It fails SOFT, unlike the open-PR half. A worktree with no `origin`
    remote, or an `origin/main` that was never fetched, is a normal
    situation; blocking there would stop an agent filing its item at all,
    which is a worse outcome than the local read the tool already did
    before this existed. The reason is always stated out loud.
    """
    text: str | None = None
    ref: str = "origin/main"
    problem: str | None = None


def read_ref_work_md(work_md: Path, ref: str = "origin/main",
                      run=None) -> RefBoard:
    """Read `work_md` as `ref` has it. Never raises, never fetches.

    `work_md` is resolved to its path INSIDE its own repository, so a
    `--work-md` pointing at a fixture outside any checkout simply reports
    that it is not in a repository and the caller falls back.
    """
    import subprocess

    if run is None:
        def run(args):
            return subprocess.run(args, capture_output=True, text=True,
                                  timeout=30)

    work_md = Path(work_md)
    directory = str(work_md.resolve().parent)
    try:
        prefix = run(["git", "-C", directory, "rev-parse", "--show-prefix"])
    except Exception as exc:  # pragma: no cover - git missing entirely
        return RefBoard(ref=ref, problem=f"could not run git: {exc}")
    if prefix.returncode != 0:
        detail = (prefix.stderr or "").strip().splitlines()
        return RefBoard(ref=ref, problem=(
            detail[0] if detail else f"{work_md} is not inside a git repository"))

    relpath = prefix.stdout.strip() + work_md.name
    try:
        shown = run(["git", "-C", directory, "show", f"{ref}:{relpath}"])
    except Exception as exc:  # pragma: no cover - git missing entirely
        return RefBoard(ref=ref, problem=f"could not run git: {exc}")
    if shown.returncode != 0:
        detail = (shown.stderr or "").strip().splitlines()
        return RefBoard(ref=ref, problem=(
            detail[0] if detail else f"could not read {ref}:{relpath}"))
    return RefBoard(text=shown.stdout, ref=ref)


def next_free_number(work_md_text: str, pr_claims: OpenPrClaims | None = None,
                      extra_texts: list[str] | None = None,
                      ) -> NextNumberResult:
    """The lowest number that is higher than every number this module knows
    about anywhere: live on the board, retired (either scheme — a number
    retired in the PM test gate is not reused either), or claimed by an open
    pull request's own diff. Sequential, matching the board's own existing
    convention (the retired line's own prose picks the next integer after
    the highest allocated, not the lowest unused gap).
    """
    known: set[int] = set()
    # Every copy of the board counts, and a number is taken if ANY of them
    # has it: the shared ref carries what has already landed, the local
    # copy carries an item this agent has written but not yet pushed.
    for text in [work_md_text, *(extra_texts or [])]:
        retired = retired_item_numbers(text)
        known |= set(live_item_numbers(text))
        known |= retired.all_queue | set(retired.gate)
    checked_open_prs = pr_claims is not None and pr_claims.problem is None
    if pr_claims is not None:
        known |= pr_claims.all_claimed
    highest = max(known) if known else 0
    return NextNumberResult(
        next_number=highest + 1,
        highest_known=highest,
        checked_open_prs=checked_open_prs,
        open_pr_problem=pr_claims.problem if pr_claims is not None else None,
    )
