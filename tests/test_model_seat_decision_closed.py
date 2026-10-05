"""The seat-model question is CLOSED and may not be refiled as pending.

Owner ruling 2026-10-02, verbatim: "We already ran multiple, multiple tests on
all of the LLMs. And that's the conclusion we came to. Close the door and move
on. There's plenty of work to do." The trade-decision seat runs
`openai/gpt-5.5`, chosen over 148 graded trials.

Why a guard and not a note. `docs/WORK.md` kept the question as
``- [ ] DECIDE BY 2026-10-31 — Which model should run the desk's actual
trade-decision seat?`` for two days after he answered it. The board's own
deadline test (`test_no_pending_decision_is_overdue`) is built to make a
FORGOTTEN decision loud; it is silent about an ANSWERED one still sitting on
the board, and a pending line reads as unfinished work to every session that
opens the file. Two agent runs plus an adversary pass were spent re-deriving
the answer before anyone noticed. This is the mechanism that stops the third.

This guard computes from the board text at check time and stores nothing: no
recorded baseline, no pinned count, no cached list of offending lines. It
refuses the question, not a particular wording of it, and it does not care
where on the board the line is written.

Closing a question is the one legal way to go green here. If a model choice
genuinely has to be reopened, the owner reopens it and this file is deleted in
the same change that records him doing so -- the mechanism that would make the
old rule incorrect is a NEW dated ruling from him, never a session's
convenience.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The board's pending-decision shape, the same one
#: `test_no_pending_decision_is_overdue` parses.
_PENDING = re.compile(r"^- \[ \] DECIDE BY (\d{4}-\d{2}-\d{2}) [-—] (.+)$")

#: A question is about seat model selection when it names a model/LLM AND the
#: thing it would be selected for. Both halves are required so that an honest
#: pending decision which merely mentions a model in passing is not refused.
_MODEL_WORDS = ("model", "llm")
_SEAT_WORDS = ("seat", "portfolio_manager", "portfolio manager", "trade-decision",
               "trade decision", "decision seat", "risk manager", "agent route",
               "routing")
_CHOICE_WORDS = ("which", "should run", "run the", "switch", "swap", "choose",
                 "choice", "select", "benchmark", "compare", "re-run", "rerun")


def _board() -> Path:
    return Path(__file__).resolve().parents[1] / "docs" / "WORK.md"


def _pending_questions() -> list[tuple[str, str]]:
    board = _board()
    if not board.exists():  # nothing to police
        return []
    found: list[tuple[str, str]] = []
    for line in board.read_text(encoding="utf-8").splitlines():
        match = _PENDING.match(line.strip())
        if match:
            found.append((match.group(1), match.group(2)))
    return found


def test_no_pending_decision_reopens_the_seat_model_choice() -> None:
    offenders = []
    for due, question in _pending_questions():
        lowered = question.lower()
        if (
            any(word in lowered for word in _MODEL_WORDS)
            and any(word in lowered for word in _SEAT_WORDS)
            and any(word in lowered for word in _CHOICE_WORDS)
        ):
            offenders.append(f"DECIDE BY {due} - {question[:110]}")

    assert not offenders, (
        "docs/WORK.md files the seat-model choice as a pending decision:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe owner closed this on 2026-10-02 after 148 graded trials: the "
          "trade-decision seat runs openai/gpt-5.5 and the question is not to be "
          "reopened, benchmarked or routed to him. Record the model fact where it "
          "belongs and remove the pending line; do not loosen this guard."
    )


def test_the_guard_recognises_the_question_it_was_written_for() -> None:
    """The exact line that sat on the board must be caught.

    Without this, a later edit could narrow the matching above until the guard
    passes on the very wording it exists to refuse.
    """
    line = ("- [ ] DECIDE BY 2026-10-31 — Which model should run the desk's "
            "actual trade-decision seat?")
    match = _PENDING.match(line)
    assert match is not None
    question = match.group(2).lower()
    assert any(word in question for word in _MODEL_WORDS)
    assert any(word in question for word in _SEAT_WORDS)
    assert any(word in question for word in _CHOICE_WORDS)
