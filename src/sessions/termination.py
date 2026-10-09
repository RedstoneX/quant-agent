"""The SIGTERM-unwind exception, in a leaf module so a session can catch it.

It lived in `src/pipeline.py`; a session that must react to a kill (repair
stops before settling) cannot import the pipeline, which imports the
sessions. `src.pipeline` re-exports it, so `from src.pipeline import
SessionTerminated` keeps working.
"""

from __future__ import annotations


class SessionTerminated(BaseException):
    """The wrapper's `timeout` sent SIGTERM; unwind so `finally` blocks run.

    `scripts/run_if_et_window.sh` runs each mode under
    `timeout --kill-after=30 1200`, so a hung session gets SIGTERM and then,
    thirty seconds later, SIGKILL. Python's default SIGTERM handling ends the
    process on the spot and NO `finally` runs — which is how a morning tick
    killed at the PM→RM boundary (the observed death mode: 61/61 BUY-proposal
    days during the 6/30-7/15 relay outage) could leave the deferred §11.2
    gross ceiling unenforced.

    Raised from a handler installed only for the duration of the morning
    and intraday bodies, it converts that silent death into an ordinary unwind that spends
    part of the thirty-second grace window paying the session's outstanding
    safety debts. A `BaseException` on purpose: the body is full of
    `except Exception` guards that would otherwise swallow it and carry on
    inside a process that is about to be killed.
    """
