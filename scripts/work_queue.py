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

  3. In `--stop-hook` mode, answers one question with an exit code: may
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

The three conditions to block, all required:

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

Usage
-----
    scripts/work_queue.py                  # the four lists, for a human
    scripts/work_queue.py --next           # one line: the next thing
    scripts/work_queue.py --json           # the same, machine-readable
    scripts/work_queue.py --stop-hook      # hook mode, reads JSON on stdin

Exit codes
----------
    0  nothing to do, or (in hook mode) the session may stop
    1  hook mode only: there IS actionable work; stderr says which
    3  the backlog could not be read at all
"""
from __future__ import annotations

import argparse
import json
import os
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


def decide(queue: Queue, session_id: str | None, stop_hook_active: bool,
           max_handbacks: int = MAX_HANDBACKS,
           idle_seconds: int = AGENT_IDLE_SECONDS,
           now: float | None = None,
           projects_root: Path | None = None,
           state_path: Path | None = None) -> HookDecision:
    """The whole policy, in one readable function, in the order it is
    checked. Every branch that is not certain resolves to "stop"."""
    if not queue.actionable:
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


def run_hook(raw: str, **kw: Any) -> int:
    """Read the harness's hook JSON, print the decision, return an exit code.

    Exit 0 lets the session stop. Exit 1 with a reason on stderr is how this
    hands the item back. Malformed input stops — an unparseable hook payload
    is not evidence of outstanding work.
    """
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        print("work_queue: could not read the hook payload; allowing stop",
              file=sys.stderr)
        return 0
    queue = build_queue()
    decision = decide(queue, payload.get("session_id"),
                      bool(payload.get("stop_hook_active")), **kw)
    if not decision.block:
        return 0
    print(f"Next in the backlog, oldest first: {decision.reason}",
          file=sys.stderr)
    return 1


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
