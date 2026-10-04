"""Loud accessors for the values a settlement recording pins at entry.

WHY THIS EXISTS. A settlement recording is built to settle a number that
governs money: doctrine says when a number will not derive in two honest
attempts, build the recording that would settle it. The recurring way that
fails is not the column and not the INSERT -- it is the one expression that
hands the value to the INSERT.

``getattr(obj, "name", None)`` is that expression. It cannot fail. When the
attribute is misspelled, lives on a different object, or is renamed later,
the call quietly yields the default and the column fills with NULL for the
entire life of the feature, while every test that asserts "the writer passes
entry_atr" still passes and the board item reads as handled. That is exactly
how ``trades.entry_atr`` stood empty from the day it shipped until
``d9a853e7`` -- the accessor read ATR(14) off ``TradeDecision``, which has no
such field, and the default answered every time.

``pinned_evidence`` is the same read made LOUD. A missing attribute raises,
so a rename breaks a test instead of silently emptying a recording, while a
genuinely absent SOURCE OBJECT (the resume and sweep lanes carry no analysis)
still records nothing rather than something reconstructed -- doctrine bars
backfilling a recording with a computed value.
"""

from __future__ import annotations

from typing import Any

__all__ = ["pinned_evidence"]


def pinned_evidence(source: Any, attribute: str) -> Any:
    """Read `attribute` off `source` for a settlement recording.

    `None` when `source` itself is absent -- the lanes that carry no analysis
    object record no evidence, which is the honest answer. `AttributeError`
    when the object IS there and does not carry the attribute, because that
    is a defect in the recording, not a datum about the trade.
    """
    if source is None:
        return None
    try:
        return getattr(source, attribute)
    except AttributeError as exc:  # pragma: no cover - re-raised with context
        raise AttributeError(
            f"settlement recording reads {attribute!r} off "
            f"{type(source).__name__}, which has no such attribute; the "
            f"recording would have stored NULL forever"
        ) from exc
