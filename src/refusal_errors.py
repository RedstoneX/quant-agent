"""Exceptions for "I could not find out", kept distinct from "the answer is no".

A money-path read that FAILS must not come back looking like a normal answer.
A failed sizing-price read used to return the same `None` as a measured "this
name has no today print", so afterwards nobody could tell the two apart. These
types make the unknown state representable; every raise site has a caller that
catches the specific type and turns it into a recorded, named refusal.
"""


class StateUnknown(Exception):
    """Base: a read that was supposed to answer a question did not."""


class SizingPriceUnavailable(StateUnknown):
    """The sizing-price read itself failed.

    NOT the same as "this name has no today print", which is a measured answer
    and stays a `None` return. Raised by `src.pipeline_stages._today_sizing_price`;
    caught by `src.sizing_refusal.sizing_price_or_refusal`, which refuses the
    entry under the reason `sizing_price_unreadable`.
    """
