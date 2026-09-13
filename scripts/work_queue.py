#!/usr/bin/env python3
"""What is actually mine to do next, decided by code rather than by memory.

Deterministic and read-only. No model call, no daemon, no new alert path.

The problem this closes
-----------------------
The orchestration failure the owner named on 2026-09-12: work agreed in
conversation was not started, a change sat 35.5 hours because the newest
thing was always picked up instead of the oldest, and three background
agents died overnight without anyone noticing. Every one of those is a
lapse of ATTENTION, and his ruling was that the fix must be code, not
another model asked to be more careful: *"I was thinking that code is the
solution, not another LM"* — a second model goes to sleep exactly the way
the first one did.

His second constraint is the harder one, and it is what most of the
machinery below is for: *"I don't want the code pushing you into an endless
loop... burning all my session or tokens."* A checker that refuses to let
the session end is worse than no checker, because it converts a missed item
into an unbounded bill. So this halts by default and blocks only under
conditions it can prove.

What it does
------------
  1. Reads the backlog (`docs/WORK.md`) through the SAME parser the status
     board uses — `scripts/status_board.py`. One parser, one set of
     buckets; a board and a queue that disagree about what is open would
     be worse than either alone.
  2. Sorts every item into exactly one of four answers to the only
     question that matters at the end of a turn — *is there something I
     could be doing right now?*

       ACTIONABLE         nobody is blocked, nothing is waiting, it is mine
       WAITING_EXTERNAL   real work exists but the next move is not mine
                          (a change is in review, tests are running)
       BLOCKED_ON_OWNER   it needs a decision only he can give
       UNREADABLE         the backlog's shape changed and this could not
                          read it — reported loudly, never as "all clear"

  3. Checks two things about the turn that is ending, which the backlog
     cannot see:

       * a CLOSURE NOBODY ARGUED AGAINST. The owner's rule is that no board
         item closes until the adversary agent has argued against closing
         it; it was skipped on five closures in a row on 2026-09-13. An open
         pull request that closes an item — it edits the backlog's "Retired
         item numbers" line, its title names an item, or its description
         says in words that it closes one — must carry a line
         beginning `Adversary:` with at least a sentence behind it. GitHub
         is read credential-free through `src/inflight.py`; a read that
         FAILS produces nothing, because not being able to look is not
         evidence that a review is missing.
       * an UNKEPT PROMISE. If this turn made no tool call at all and its
         closing line says it is doing something, the promise died with the
         turn. See `scripts/turn_promises.py` for what it deliberately does
         not flag and why.

  4. In `--stop-hook` mode, answers one question with an exit code: may
     this session stop? It says no ONLY when all of the following hold,
     and yes in every other case including every case it is unsure about.

Why it errs toward stopping
---------------------------
A Stop hook that blocks while a background agent is still running re-sends
the whole transcript on every firing — measured at $1.59 per firing
(anthropics/claude-code#93745). That is the exact failure his constraint
names, so "something might still be running" resolves to STOP, never to
block. The same applies to an unreadable backlog: a parser that cannot
read the file has no business insisting there is work in it.

Any of the three checks can block, and the same two escape hatches apply to
all of them: nothing blocks while a background agent looks alive, and
nothing blocks on a read that failed. For the backlog check specifically,
all three of these must hold:

  * ACTIONABLE is non-empty. Waiting-external and blocked-on-owner work
    is not a reason to keep a session alive; neither is a backlog that
    could not be parsed.
  * No background agent looks alive. See `running_agents` — this reads
    the harness's own subagent transcripts and treats recent write
    activity as alive, because being wrong in that direction costs a stop
    that should have blocked, and being wrong the other way costs money.
  * This same item has not already been handed back `--max-handbacks`
    times (default 2). If two attempts have not moved an item, a third
    inside the same session is a loop, not diligence. The count lives in
    a state file keyed by session, so it survives the turns it has to.

The oldest-first rule
---------------------
Within ACTIONABLE the order is the backlog's own ranking, lowest number
first, because the backlog is ranked by measured cost and the owner's
instruction is explicit: *"Do not reorder it from intuition."* The 35.5
hour change happened under the opposite habit.

What this does NOT enforce
--------------------------
Stated plainly, because a checker that is believed to cover more than it
does is worse than one nobody trusts:

  * It cannot see a MERGED closure. Both new checks read OPEN pull requests,
    so an item closed and merged inside one turn is past them.
  * It does not judge the adversary's argument, only that one is written
    down. A dishonest `Adversary:` line passes.
  * It reads only the CLOSING line of the final message, and only when the
    turn called no tool at all. A promise made mid-message, or in a turn
    that did some other work, is not flagged — deliberately; see
    `scripts/turn_promises.py`.
  * It never blocks while a background agent looks alive, so a promise made
    in that state is missed rather than caught late.
  * It only runs where it is installed. The wrapper is safe to install at
    user level, and until it is, a session started outside this repository
    is not checked at all — the reason this had never fired once by
    2026-09-13.

Usage
-----
    scripts/work_queue.py                  # the four lists, for a human
    scripts/work_queue.py --next           # one line: the next thing
    scripts/work_queue.py --json           # the same, machine-readable
    scripts/work_queue.py --stop-hook      # hook mode, reads JSON on stdin

Exit codes
----------
    0  nothing to do, or (in hook mode) the session may stop
    2  hook mode only: STOP BLOCKED. stderr says which of the three checks
       spoke — an unkept promise from this turn, a closure nobody argued
       against, or the next actionable backlog item. 2 is the harness's own
       and ONLY blocking code; this used to return 1, which the harness
       treats as a hook error and lets the session end.
    3  the backlog could not be read at all (report mode)

Switches
--------
    WORK_QUEUE_PROMISE_CHECK=0     silence the unkept-promise check
    WORK_QUEUE_ADVERSARY_CHECK=0   silence the adversarial-review check
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.status_board import (  # noqa: E402
    PendingDecision,
    QueueItem,
    load_board_notes,
    load_funnel_queue,
    load_pending_decisions,
    load_pm_gate,
)
from scripts.turn_promises import unkept_promise  # noqa: E402
from src.inflight import (  # noqa: E402
    RETIRED_LINE,
    WORK_MD,
    OpenPR,
    read_open_pull_requests,
)

#: How long after its last write a subagent transcript still counts as a
#: live agent. Generous on purpose: the cost of calling a finished agent
#: "alive" is one stop that should have blocked; the cost of calling a live
#: agent "finished" is a blocking hook that re-sends the transcript at
#: $1.59 a firing. The asymmetry is the whole reason for the default.
AGENT_IDLE_SECONDS = 180

#: A third attempt at the same item inside one session is a loop.
MAX_HANDBACKS = 2

#: Where the per-session hand-back counts live. Under the harness's own
#: scratch root when it is present, so the file dies with the session
#: rather than accumulating in the repo.
STATE_DIR_ENV = "CLAUDE_PROJECT_SCRATCH"
STATE_FILENAME = "work_queue_handbacks.json"

#: Buckets `scripts/status_board.py` produces, mapped to who the next move
#: belongs to. Kept as a table rather than a chain of `if`s so that a bucket
#: added to the board and forgotten here fails loudly in `classify` instead
#: of silently landing in whichever branch happened to be last.
BUCKET_OWNERSHIP = {
    "open": "ACTIONABLE",
    # Finished by its own account but never struck through. The write-up
    # IS the work — the owner's "déjà vu" complaint is caused by exactly
    # these sitting unmarked, so they are mine, not noise.
    "finished_unmarked": "ACTIONABLE",
    # Done, review outstanding. The review is a real task and it is mine.
    "review_owed": "ACTIONABLE",
    # He has ruled or somebody is building it. Neither is my move to make.
    "in_hand": "WAITING_EXTERNAL",
    # Parked by an explicit decision. Re-raising a parked item is the
    # behaviour he has corrected more than once.
    "paused": "BLOCKED_ON_OWNER",
    "no_action": "NONE",
    "resolved": "NONE",
}


@dataclass
class Queue:
    """Everything the backlog says, sorted by whose move it is."""

    actionable: list[QueueItem] = field(default_factory=list)
    waiting_external: list[QueueItem] = field(default_factory=list)
    blocked_on_owner: list[QueueItem] = field(default_factory=list)
    #: Plain-English sentences, one per section that could not be read.
    unreadable: list[str] = field(default_factory=list)
    #: Decisions the owner owes an answer on. Never actionable by me, and
    #: listed separately because their handle is a date, not a number.
    decisions: list[PendingDecision] = field(default_factory=list)

    @property
    def next_item(self) -> QueueItem | None:
        """The lowest-numbered actionable item — the backlog's own order."""
        return self.actionable[0] if self.actionable else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "actionable": [{"ref": i.ref, "title": i.title} for i in self.actionable],
            "waiting_external": [{"ref": i.ref, "title": i.title}
                                 for i in self.waiting_external],
            "blocked_on_owner": [{"ref": i.ref, "title": i.title}
                                 for i in self.blocked_on_owner],
            "unreadable": list(self.unreadable),
            "decisions": [{"ref": d.ref, "question": d.question,
                           "days_left": d.days_left} for d in self.decisions],
        }


def classify(items: list[QueueItem]) -> dict[str, list[QueueItem]]:
    """Sort parsed backlog items by whose move is next.

    An unknown bucket is an ACTIONABLE-side error on purpose: a board that
    grows a new bucket should surface here as something to look at, not be
    silently dropped into "nothing to do".
    """
    out: dict[str, list[QueueItem]] = {
        "ACTIONABLE": [], "WAITING_EXTERNAL": [], "BLOCKED_ON_OWNER": [],
    }
    for item in items:
        owner = BUCKET_OWNERSHIP.get(item.bucket, "ACTIONABLE")
        if owner == "NONE":
            continue
        out[owner].append(item)
    for group in out.values():
        group.sort(key=lambda i: i.rank)
    return out


def build_queue(work_md: Path | None = None,
                board_notes: Path | None = None) -> Queue:
    """Read the backlog and sort it. Never raises on a malformed backlog.

    Every read is defensive for the reason the board's own loader gives:
    this file is hand-edited constantly by several sessions at once, and a
    broken edit must produce "I could not read it", never a stack trace and
    never a silently empty queue that reads as "all done".
    """
    work_md = work_md or (REPO_ROOT / "docs" / "WORK.md")
    board_notes = board_notes or (REPO_ROOT / "docs" / "BOARD_NOTES.md")
    queue = Queue()

    try:
        notes = load_board_notes(board_notes)
    except Exception:  # noqa: BLE001 - prose is decoration here, never a blocker
        notes = {}

    for loader, label in ((load_funnel_queue, "the running order"),
                          (load_pm_gate, "the model-test gate")):
        try:
            items, problem = loader(work_md, notes=notes)
        except Exception as exc:  # noqa: BLE001
            queue.unreadable.append(f"{label} could not be read ({exc}).")
            continue
        if problem:
            queue.unreadable.append(problem)
            continue
        sorted_items = classify(items)
        queue.actionable.extend(sorted_items["ACTIONABLE"])
        queue.waiting_external.extend(sorted_items["WAITING_EXTERNAL"])
        queue.blocked_on_owner.extend(sorted_items["BLOCKED_ON_OWNER"])

    try:
        queue.decisions = load_pending_decisions(work_md, notes=notes)
    except Exception:  # noqa: BLE001
        queue.unreadable.append("The pending decisions could not be read.")

    queue.actionable.sort(key=lambda i: (i.source, i.rank))
    return queue


# ---------------------------------------------------------------------------
# Is anything still running?
# ---------------------------------------------------------------------------

def running_agents(session_id: str | None,
                   idle_seconds: int = AGENT_IDLE_SECONDS,
                   now: float | None = None,
                   projects_root: Path | None = None) -> list[str]:
    """Subagent transcripts written to recently enough to look alive.

    There is no terminal record at the end of a subagent's transcript to
    read, so this uses the one signal that is actually there: how long ago
    the file was last appended to. That is a heuristic and it is stated as
    one — but it is a heuristic whose error is in the safe direction. A
    finished agent mistaken for a live one costs a stop that should have
    blocked. A live agent mistaken for a finished one costs a blocking hook
    firing against a growing transcript, which is the $1.59-per-firing trap
    this whole design exists to avoid.

    Returns the agent identifiers that look alive, so the caller can SAY
    what it is waiting for rather than just declining to act.
    """
    if not session_id:
        return []
    root = projects_root or (Path.home() / ".claude" / "projects")
    now = now if now is not None else time.time()
    alive: list[str] = []
    try:
        candidates = list(root.glob(f"*/{session_id}/subagents/agent-*.jsonl"))
    except OSError:
        return []
    for path in candidates:
        try:
            if now - path.stat().st_mtime <= idle_seconds:
                alive.append(path.stem.removeprefix("agent-"))
        except OSError:
            continue
    return sorted(alive)


# ---------------------------------------------------------------------------
# Was the closure argued against?
# ---------------------------------------------------------------------------

#: The owner's rule: no board item is closed until the adversary agent
#: (`.claude/agents/qamc-adversary.md`) has argued against closing it. It was
#: skipped on five closures in a row on 2026-09-13, and the reason it keeps
#: being skipped is that work coming back green produces no signal that it
#: was never challenged. Nothing about "remember to run the adversary" has
#: held; this is the mechanical version.
#:
#: The evidence is a line in the pull request description beginning
#: `Adversary:` and carrying at least a sentence. A bare `Adversary: yes` is
#: not evidence of an argument and does not count.
ADVERSARY_LINE = re.compile(r"^\s*Adversary\s*:\s*(.+)$", re.I | re.M)

#: What counts as "at least a sentence" after the label. Deliberately low —
#: this is a presence check, not a quality one; judging the argument is the
#: reader's job, and a threshold high enough to judge would be gamed by
#: padding anyway.
MIN_ADVERSARY_WORDS = 6

#: "item 12", "items 30/31/32" — the convention this repository's pull
#: request titles already use for the item they close.
ITEM_REF = re.compile(r"\bitems?\s+#?(\d+)\b", re.I)

#: In the BODY the same words are usually a citation, not a claim: PR 351
#: ("docs: make README match the desk that actually exists") mentions
#: "item 44" only to explain where a deleted rule came from, and demanding
#: an adversary line for that is the cried wolf. So a body reference counts
#: only when a closing verb is attached to it.
ITEM_CLOSED = re.compile(
    r"\b(?:closes?|closing|closed|resolves?|resolved|retires?|retired"
    r"|completes?|completed|finishes?|finished|fixes)\b[^.\n]{0,40}?"
    r"\bitems?\s+#?(\d+)\b", re.I)


def closes_a_board_item(pr: OpenPR) -> str | None:
    """Why this change looks like a board closure, or None.

    Two independent signals, either of which is enough:

      * it edits the backlog's "Retired item numbers" line — that line is
        only ever touched to retire an item, so editing it IS a closure
        whatever the description says; and
      * its TITLE names a board item by number — this repository's own
        convention for the item a change closes — or its description says
        in words that it closes one.

    A change whose files could not be read still gets the second test. It
    never gets the first, because a read that failed is not evidence.
    """
    patch = pr.work_md_patch
    if patch:
        for line in patch.splitlines():
            if line[:1] in "+-" and line[1:2] != line[:1] and RETIRED_LINE in line:
                return f"it edits the {WORK_MD} line that retires board items"
    match = ITEM_REF.search(pr.title or "")
    if match:
        return f"its title names board item {match.group(1)}"
    match = ITEM_CLOSED.search(pr.body or "")
    if match:
        return f"its description says it closes board item {match.group(1)}"
    return None


def has_adversary_evidence(body: str) -> bool:
    """Does this description carry a real `Adversary:` line?"""
    for match in ADVERSARY_LINE.finditer(body or ""):
        argument = match.group(1).strip()
        if len(argument.split()) >= MIN_ADVERSARY_WORDS:
            return True
    return False


def unreviewed_closures(prs: list[OpenPR]) -> list[str]:
    """One plain sentence per open change that closes an item unchallenged."""
    out: list[str] = []
    for pr in prs:
        why = closes_a_board_item(pr)
        if why and not has_adversary_evidence(pr.body):
            out.append(f"PR {pr.number} ({pr.title or 'untitled'}) closes a "
                       f"board item — {why} — but its description carries no "
                       f"'Adversary:' line, so nothing argued against closing "
                       f"it.")
    return out


def adversary_gaps(fetch: Any = None) -> list[str]:
    """The unreviewed closures, or an empty list if GitHub could not be read.

    A failed read must NEVER manufacture work. "I could not see the pull
    requests" is not evidence that a review is missing, and a hook that
    treats it as such would hold sessions open every time GitHub rate-limits
    this address — which for an unauthenticated reader is routine.
    """
    try:
        prs, problem = (read_open_pull_requests(fetch) if fetch
                        else read_open_pull_requests())
    except Exception:  # noqa: BLE001 - this check never breaks a session
        return []
    if problem:
        return []
    return unreviewed_closures(prs)


# ---------------------------------------------------------------------------
# Hand-back accounting — the loop brake
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    scratch = os.environ.get(STATE_DIR_ENV)
    base = Path(scratch) if scratch else Path("/tmp")
    return base / STATE_FILENAME


def _load_state(path: Path) -> dict[str, dict[str, int]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def handback_count(session_id: str, ref: str, path: Path | None = None) -> int:
    """How many times this session has already been handed this same item."""
    state = _load_state(path or _state_path())
    return int(state.get(session_id, {}).get(ref, 0))


def record_handback(session_id: str, ref: str, path: Path | None = None) -> int:
    """Count one hand-back and return the new total. Best effort: a state
    file that cannot be written degrades to no brake at all, which the
    harness's own consecutive-block cap (8) still bounds."""
    path = path or _state_path()
    state = _load_state(path)
    session = state.setdefault(session_id, {})
    session[ref] = int(session.get(ref, 0)) + 1
    try:
        path.write_text(json.dumps(state))
    except OSError:
        pass
    return session[ref]


# ---------------------------------------------------------------------------
# Hook mode
# ---------------------------------------------------------------------------

@dataclass
class HookDecision:
    """May the session stop, and the one sentence explaining why."""

    block: bool
    reason: str
    #: Which check spoke. Drives the exit code, so the caller — and the
    #: reader of a hook log — can tell a dropped backlog item from a broken
    #: promise without parsing English.
    kind: str = "queue"


def decide(queue: Queue, session_id: str | None, stop_hook_active: bool,
           max_handbacks: int = MAX_HANDBACKS,
           idle_seconds: int = AGENT_IDLE_SECONDS,
           now: float | None = None,
           projects_root: Path | None = None,
           state_path: Path | None = None,
           promise: str | None = None,
           gaps: list[str] | None = None) -> HookDecision:
    """The whole policy, in one readable function, in the order it is
    checked. Every branch that is not certain resolves to "stop".

    `promise` is the unkept commitment this turn ended on, and `gaps` the
    unreviewed closures, both already gathered by the caller — this function
    stays free of I/O so the policy can be read and tested on its own.

    Order matters, and it is not the order of importance:

      1. Nothing to say at all → stop. The cheap exit, taken first.
      2. Something to say, but an agent is alive → stop anyway. The cost
         trap outranks every finding below it; blocking while a subagent
         writes re-sends the whole transcript at $1.59 a firing.
      3. An unkept promise → block. First because it is about THIS turn and
         costs one sentence to fix, where the others are about other work.
      4. A closure nobody argued against → block.
      5. An actionable backlog item → block, subject to the hand-back brake.
    """
    gaps = gaps or []
    if not queue.actionable and not promise and not gaps:
        if queue.unreadable:
            # Loud in the report, permissive here. A parser that cannot read
            # the backlog has no grounds to insist there is work in it.
            return HookDecision(False, "the backlog could not be read; "
                                       "not holding the session open on a guess")
        return HookDecision(False, "nothing in the backlog is mine to act on")

    alive = running_agents(session_id, idle_seconds=idle_seconds, now=now,
                           projects_root=projects_root)
    if alive:
        return HookDecision(False, f"{len(alive)} background agent(s) still "
                                   "writing; blocking now re-sends the whole "
                                   "transcript on every firing")

    if promise:
        return HookDecision(
            True,
            f"“{promise}” — and the turn made no tool call at all. "
            "Do it now, or say plainly that it is not being done.",
            kind="promise")

    if gaps:
        first = gaps[0]
        more = (f" ({len(gaps) - 1} more like it.)" if len(gaps) > 1 else "")
        return HookDecision(
            True,
            f"{first} Run the adversary against it and put its argument in "
            f"the description before this closes.{more}",
            kind="adversary")

    item = queue.next_item
    assert item is not None  # non-empty actionable, checked above
    if session_id:
        already = handback_count(session_id, item.ref, state_path)
        if already >= max_handbacks:
            return HookDecision(False, f"{item.ref} has already been handed "
                                       f"back {already} times this session; "
                                       "a third pass is a loop, not diligence")
        record_handback(session_id, item.ref, state_path)

    return HookDecision(True, f"{item.ref} is actionable and nothing is "
                              f"waiting on anyone else: {item.title}")


#: The ONLY exit code that stops a Stop hook from stopping. Verified against
#: the harness's own documentation on 2026-09-13, not remembered: exit 0 ends
#: the conversation, exit 2 blocks it and feeds stderr back, and every other
#: non-zero code is reported as a hook ERROR and ends the conversation
#: anyway. This file previously returned 1 to hand work back, which means
#: that even in a session where it loaded it could never have blocked.
BLOCK_EXIT = 2

#: Set to "0"/"off"/"false" to silence the unkept-promise check alone. It is
#: ON by default: measured against this session's own transcript it fired on
#: 6 of 169 turns that ended without a single tool call, and every one of
#: those six was the real thing. The switch exists because a check that
#: fires on English deserves an off button that does not require an edit.
PROMISE_CHECK_ENV = "WORK_QUEUE_PROMISE_CHECK"

#: Same, for the adversarial-review check.
ADVERSARY_CHECK_ENV = "WORK_QUEUE_ADVERSARY_CHECK"


def _enabled(env_name: str) -> bool:
    return os.environ.get(env_name, "1").strip().lower() not in (
        "0", "off", "false", "no")


def run_hook(raw: str, **kw: Any) -> int:
    """Read the harness's hook JSON, print the decision, return an exit code.

    Exit 0 lets the session stop; exit 2 blocks it and hands the reason back.
    Malformed input stops — an unparseable hook payload is not evidence of
    outstanding work, and neither is a check that threw.
    """
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        print("work_queue: could not read the hook payload; allowing stop",
              file=sys.stderr)
        return 0
    if not isinstance(payload, dict):
        print("work_queue: the hook payload was not an object; allowing stop",
              file=sys.stderr)
        return 0

    queue = build_queue()

    promise = None
    if _enabled(PROMISE_CHECK_ENV):
        try:
            promise = unkept_promise(payload.get("transcript_path"))
        except Exception:  # noqa: BLE001 - never break a session over this
            promise = None

    gaps: list[str] = []
    if _enabled(ADVERSARY_CHECK_ENV):
        gaps = adversary_gaps()

    decision = decide(queue, payload.get("session_id"),
                      bool(payload.get("stop_hook_active")),
                      promise=promise, gaps=gaps, **kw)
    if not decision.block:
        return 0
    lead = {
        "promise": "You said you were doing this and the turn did nothing",
        "adversary": "A closure has not been argued against",
    }.get(decision.kind, "Next in the backlog, oldest first")
    print(f"{lead}: {decision.reason}", file=sys.stderr)
    return BLOCK_EXIT


# ---------------------------------------------------------------------------
# Human report
# ---------------------------------------------------------------------------

def render(queue: Queue) -> str:
    lines: list[str] = []
    if queue.unreadable:
        lines.append("COULD NOT READ")
        lines.extend(f"  {p}" for p in queue.unreadable)
        lines.append("")
    lines.append(f"ACTIONABLE ({len(queue.actionable)}) — oldest first")
    for item in queue.actionable:
        lines.append(f"  {item.ref}: {item.title}")
    if queue.waiting_external:
        lines.append("")
        lines.append(f"WAITING ON SOMEONE ELSE ({len(queue.waiting_external)})")
        for item in queue.waiting_external:
            lines.append(f"  {item.ref}: {item.title}")
    if queue.blocked_on_owner:
        lines.append("")
        lines.append(f"PARKED BY A DECISION ({len(queue.blocked_on_owner)})")
        for item in queue.blocked_on_owner:
            lines.append(f"  {item.ref}: {item.title}")
    if queue.decisions:
        lines.append("")
        lines.append(f"NEEDS THE OWNER ({len(queue.decisions)})")
        for dec in queue.decisions:
            when = ("OVERDUE" if dec.overdue
                    else f"{dec.days_left} day(s) left")
            lines.append(f"  {dec.ref} [{when}]: {dec.question}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stop-hook", action="store_true",
                    help="hook mode: read the harness JSON on stdin")
    ap.add_argument("--next", action="store_true",
                    help="print only the next actionable item")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    ap.add_argument("--work-md", default=None)
    ap.add_argument("--max-handbacks", type=int, default=MAX_HANDBACKS)
    ap.add_argument("--agent-idle-seconds", type=int, default=AGENT_IDLE_SECONDS)
    args = ap.parse_args(argv)

    if args.stop_hook:
        return run_hook(sys.stdin.read(),
                        max_handbacks=args.max_handbacks,
                        idle_seconds=args.agent_idle_seconds)

    queue = build_queue(Path(args.work_md) if args.work_md else None)
    if args.json:
        print(json.dumps(queue.as_dict(), indent=2))
    elif args.next:
        item = queue.next_item
        print(f"{item.ref}: {item.title}" if item else "nothing actionable")
    else:
        print(render(queue))
    if queue.unreadable and not queue.actionable:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
