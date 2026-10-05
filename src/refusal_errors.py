"""Exceptions for "I could not find out", kept distinct from "the answer is no".

Three money-path reads used to turn a failed lookup into a normal-looking
answer: a failed exchange-calendar read became "not a trading day", a
crashing evidence gate became "no gate ran, proceed", and a failed price
read became "there is no price". Each fabricated answer is indistinguishable
afterwards from the genuine one it imitates, which is the whole defect: the
desk cannot report a true state it never held.

These exceptions exist so the unknown state is REPRESENTABLE. They are not
thrown into the void — every raise site in this repo has a caller that
catches the specific type and converts it into a recorded, deliberate
refusal (no order, no fabricated measurement, a distinct reason string).
"""


class StateUnknown(Exception):
    """Base: a read that was supposed to answer a question did not."""


class TradingCalendarUnavailable(StateUnknown):
    """The exchange calendar could not be read.

    NOT the same as "the exchange is shut". Raised by
    `AlpacaBroker.is_trading_day` and by `Pipeline._is_trading_day`; caught
    by the scheduler's `_run_safe` and by `main.py`'s session wrappers,
    both of which record a failed session and notify the owner instead of
    skipping the day in silence.
    """


class SizingPriceUnavailable(StateUnknown):
    """The sizing-price read itself failed.

    NOT the same as "this name has no today print" (which is a real,
    measured answer and stays a `None` return). Raised by
    `_today_sizing_price`; caught at all three of its call sites, each of
    which refuses the name under its own distinct skip reason.
    """
