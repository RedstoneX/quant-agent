#!/usr/bin/env python3
"""Did this turn end on a promise it did not keep?

The failure this closes
-----------------------
The owner, 2026-09-13: *"I want to know the absolute root cause of saying
something but doing nothing."* The mechanism is not forgetfulness. A stated
intention has no weight in the next turn — nothing re-reads it, nothing acts
on it — and an intention written as the LAST line of a message dies with the
turn that wrote it, because the turn is over the moment that text is sent.
It happened three times in one session, all three closing sentences.

So the check is narrow and structural: look at the FINAL assistant message
of the turn. If it carries no tool call, that turn did nothing after that
sentence, by definition. If that same message contains a first-person,
immediate, active commitment, the promise died there.

Why precision is the whole design
---------------------------------
This fires on ordinary English written by a model that writes a lot of it.
A check that cries wolf gets muted, and a muted check is worse than none —
it costs the appearance of enforcement. So every ambiguous shape is a MISS
on purpose. Not flagged, deliberately:

  * a question ("Want me to dispatch that?") — asking is not promising
  * a conditional or a dependency on something else finishing ("once it
    lands", "if it fails", "the moment they are") — the turn genuinely
    cannot act yet, so the next turn is the right place
  * a promise about a future session, a scheduled job, or a standing habit
    ("going forward", "from now on", "tomorrow")
  * work described as an AGENT's or someone else's, not the writer's
  * a promise to COMMUNICATE ("I'll confirm", "I'll report back") — there
    is no tool call that could have discharged it inside this turn
  * anything past tense ("Dispatched.") — that is a claim about a tool call,
    and a false one is a different defect than this one
  * a negation ("I won't touch WORK.md")

What is left is the shape that actually failed: "Merging now.",
"Dispatching the fix now.", "I'll rebase #304 and merge it", written with no
tool call behind them.

Calibration
-----------
Measured against this repository's own session transcript rather than
invented sentences — see `tests/test_work_queue.py`, whose negative cases
are real sentences lifted from it.
"""
from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path

#: Fenced code, inline code and anything in double quotes are quoted
#: material, not the writer speaking. Without the last one this check fires
#: on a message ABOUT the check ('"Dispatching now" and "dispatched" look
#: identical in my own transcript' — a real sentence from this session).
_FENCE = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`]*`")
_QUOTED = re.compile(r"[\u201c\"][^\u201d\"\n]*[\u201d\"]")

#: A sentence ends at . ! ? or a newline. Bullets count as sentences: the
#: three real failures were bullet lines as often as prose.
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

#: First person, active, about to happen. Each alternative was taken from a
#: sentence that really occurred; none is speculative.
_COMMITMENT = re.compile(
    r"(?:^|(?<=[\s(\-*—]))(?:"
    r"i'?ll\b"
    r"|i\s+will\b"
    r"|i'?m\s+going\s+to\b"
    r"|i'?m\s+about\s+to\b"
    r"|next\s+i\b"
    r"|starting\s+on\b"
    r")", re.I)

#: "Merging now." — a bare gerund opening the sentence, with an explicit
#: "now". The "now" is required: "Running." on its own is a status report,
#: and the difference between the two is exactly this word.
_GERUND_NOW = re.compile(
    r"^\W*(?:dispatching|running|building|fixing|checking|merging|rebasing"
    r"|pushing|committing|verifying|investigating|resolving|updating|writing"
    r"|reading|searching|adding|creating|opening|closing|proceeding\s+to"
    r"|doing|starting|kicking\s+off)\b[^.!?]*\bnow\b", re.I)

#: Anything here disqualifies the whole sentence. Coverage traded for
#: precision, deliberately and in that direction.
_CONDITIONAL = re.compile(
    r"\b(?:if|unless|once|when|whenever|after|until|as\s+soon\s+as"
    r"|the\s+moment|in\s+case|provided|assuming|pending|while)\b", re.I)

_FUTURE_SESSION = re.compile(
    r"\b(?:going\s+forward|from\s+now\s+on|in\s+future|next\s+session"
    r"|tomorrow|tonight|overnight|later|each\s+(?:morning|day|time)"
    r"|every\s+time|scheduled|cron|nightly|weekly|daily)\b", re.I)

_SOMEONE_ELSE = re.compile(
    r"\b(?:agent|agents|subagent|subagents|session|sessions|github|ci"
    r"|auto-?merge|they|it)\s+(?:will|'ll|is|are)\b|\bthe\s+agent\b"
    r"|\banother\s+session\b", re.I)

#: A promise to SAY something. No tool call discharges it, so its absence
#: is not evidence of anything.
_JUST_TALKING = re.compile(
    r"\b(?:tell|confirm|report|let\s+you\s+know|come\s+back|bring\s+back"
    r"|update\s+you|flag|mention|note|bring\s+you|say|explain|show\s+you"
    r"|answer|keep\s+you|label|call\s+it|describe|own)\b", re.I)

#: A stance, a habit, or a manner — "I'll keep it simple", "I'll slow down",
#: "I'll be looking hard at that". Real sentences, all of them, and none of
#: them nameable as a tool call, so their absence proves nothing.
_STANCE = re.compile(
    r"\bi'?(?:ll|m)\s+(?:be\s+\w+ing|keep|stay|slow|stop|go\s+quiet|hold"
    r"|remain|treat|watch|avoid|stick|carry|leave|make\s+sure|ensure|try"
    r"|remember|only|also|still|never|always|need|want|have\s+to)\b", re.I)

#: "- **Reading** — taking the number from what's in front of you" is a
#: glossary line, not an intention, and its bolded gerund otherwise reads as
#: one. Another real sentence from this session.
_DEFINITION = re.compile(r"^[-*+]\s*\*\*[^*]+\*\*\s*[\u2014:-]")

_NEGATED = re.compile(r"\b(?:won'?t|will\s+not|not\s+going\s+to|never)\b", re.I)


def _sentences(text: str) -> list[str]:
    text = _QUOTED.sub(" ", _INLINE.sub(" ", _FENCE.sub(" ", text)))
    return [s.strip() for s in _SPLIT.split(text) if s.strip()]


def _last_line(text: str) -> str:
    """The closing line of the message, which is where this failure lives.

    Not a simplification for its own sake. The owner's three cases were all
    CLOSING sentences, and the mechanism is specific to that position: an
    intention stated mid-message is usually discharged by the rest of the
    message, while one written last has nothing after it. Scanning the whole
    message instead roughly triples the firing rate on this session's own
    transcript, almost entirely on sentences that were in fact acted on.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def offending_sentence(text: str) -> str | None:
    """The first unkept-looking commitment in `text`, or None.

    `text` must be the whole final assistant message; the caller has already
    established that no tool call followed it.
    """
    closing = _last_line(text)
    if _DEFINITION.match(closing.strip()):
        return None
    for raw in _sentences(closing):
        s = raw.strip("*_# ").strip()
        if not s or s.endswith("?"):
            continue
        if _STANCE.search(s):
            continue
        if _CONDITIONAL.search(s) or _FUTURE_SESSION.search(s):
            continue
        if _SOMEONE_ELSE.search(s) or _JUST_TALKING.search(s):
            continue
        if _NEGATED.search(s):
            continue
        if _COMMITMENT.search(s) or _GERUND_NOW.search(s):
            return s
    return None


def _is_real_user_turn(entry: dict) -> bool:
    """A person typing, as opposed to a tool handing a result back.

    Both are `"type": "user"` in the transcript. The turn boundary is the
    first kind; mistaking the second for it would cut every turn that used a
    tool down to nothing.
    """
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                   for b in content)


def final_turn_message(transcript_path: str | Path,
                       tail_lines: int = 600) -> tuple[str, bool] | None:
    """The last assistant message of the turn, and whether the TURN used a
    tool at all.

    Returns `(text, turn_used_a_tool)`, or None when there is nothing to
    judge — an unreadable path, no assistant text at the end, or a turn
    boundary that cannot be found in the tail that was read. None always
    means "do not fire": this check must never manufacture a finding out of
    a failed read.

    In this transcript format an assistant entry holds text OR tool calls,
    never both, so a turn is several entries and "did the turn act" is a
    question about all of them, not about the last one.

    Only the tail of the file is read. These transcripts reach hundreds of
    megabytes and a Stop hook that loads one into memory is its own outage.
    """
    try:
        path = Path(transcript_path)
        with path.open("r", errors="replace") as fh:
            tail = deque(fh, maxlen=tail_lines)
    except (OSError, TypeError, ValueError):
        return None

    final_text: str | None = None
    used_tool = False
    for line in reversed(tail):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        kind = entry.get("type")
        if kind == "user":
            if _is_real_user_turn(entry):
                break  # start of the turn
            continue  # a tool result: still inside the turn
        if kind != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        if any(isinstance(b, dict) and b.get("type") == "tool_use"
               for b in content):
            used_tool = True
            if final_text is None:
                # The turn's last act was a tool call, not a sentence.
                final_text = ""
            continue
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
        if final_text is None and text.strip():
            final_text = text
    if final_text is None:
        return None
    return (final_text, used_tool)


def unkept_promise(transcript_path: str | Path | None) -> str | None:
    """The sentence this turn promised and did not act on, or None.

    A turn that called ANY tool does not fire. That is the last precision
    guard and it earns its place: "Running now:" written under a list of
    agents that the same turn actually dispatched is a status report, and
    treating it as a broken promise is exactly the cried wolf that would get
    this check switched off. What is left is the real shape — a turn that
    did nothing at all and signed off saying it was doing something.
    """
    if not transcript_path:
        return None
    found = final_turn_message(transcript_path)
    if found is None:
        return None
    text, used_tool = found
    if used_tool:
        return None
    return offending_sentence(text)
