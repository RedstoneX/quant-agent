"""Desk-set fields on a technical verdict that make a re-read skippable
(board item 177). Kept off `TechAnalystAnswerItem`, so the response schema the
model is asked to fill is unchanged."""

from pydantic import BaseModel


class TechRereadFields(BaseModel):
    #: PYTHON-SET. A hash over every input this verdict is a function of: the
    #: submitted bars and indicators, the prior-rating context, the valuation
    #: line, the live/forming-session block and the macro strings. None means
    #: the inputs could not be pinned down, which forces a fresh call.
    input_fingerprint: str | None = None
    #: PYTHON-SET. "refreshed_this_session" when this run asked the seat,
    #: "carried_forward" when the verdict was reused because no input moved
    #: (board item 227's per-seat stamp must never read a reuse as fresh).
    read_state: str = "refreshed_this_session"
