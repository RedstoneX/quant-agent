"""The price target's ONE VOTE toward the alignment exit.

Owner ruling, 2026-10-08: *"one vote toward the alignment exit. Can tip a
close when other signals agree. But never sells alone."*

`src.risk.alignment_exit` exits when a chart mark is lost AND the close
sits more than the give-back tolerance past the LAST lost mark (the lowest
for a long, the highest for a short). This module supplies the vote: once
a completed close has reached the CURRENT target on or after the date it
took effect, the give-back is read from the FIRST lost mark instead. With
no lost mark there is nothing to vote on, so the position holds; with one
lost mark the vote changes nothing; with two or more it can tip a close
that would otherwise hold. Target not reached, or no target supplied, is
the plain rule — and the vote text says which applied, with the target's
value, version and effective date, so the record never goes silent.

A leaf: it imports nothing from execution and introduces no number.
"""

from __future__ import annotations

from src.risk.chart_averages import _finite

__all__ = ["reference_mark", "target_reached", "target_vote"]


def target_reached(
    closes: list[float], bar_dates: list, target: float | None,
    effective_date, *, is_short: bool = False,
) -> bool | None:
    """Has a COMPLETED close reached the CURRENT target since it took effect?

    `closes` and `bar_dates` are parallel, ascending. Reached means a close
    at or beyond `target` (>= long, <= short) on a session dated on or
    after `effective_date` (position entry, or the latest applied target
    revision). Close only, no intraday touch, no tolerance, and nothing
    before the effective date counts — a revision resets the question.
    None when there is no target or no dates to judge it against.
    """
    t = _finite(target)
    if t is None or not bar_dates or not effective_date:
        return None
    eff = str(effective_date)[:10]
    return any(
        str(d)[:10] >= eff and (c <= t if is_short else c >= t)
        for d, c in zip(bar_dates, closes) if _finite(c) is not None
    )


def target_vote(
    closes: list[float], bar_dates: list | None, target: float | None,
    effective_date, version: str, *, is_short: bool = False,
) -> tuple[bool | None, str]:
    """(reached, plain-words account of the vote) for one verdict.

    `reached` is `target_reached`'s answer. The text names the target, its
    version (which record set it) and effective date when there is one,
    and otherwise says LOUDLY that there was no target to read and why
    (`version` carries the caller's reason in that case).
    """
    reached = target_reached(
        closes, bar_dates or [], target, effective_date, is_short=is_short,
    )
    if reached is None:
        why = (
            (version or "no current target supplied") if _finite(target) is None
            else "no dates to judge the target against"
        )
        return None, (
            f"target vote: NOT APPLIED, no target to read ({why}) — today's "
            f"rule, measured from the last lost mark"
        )
    return reached, (
        f"target vote: {'APPLIED' if reached else 'not applied'} — target "
        f"{_finite(target):.4f} ({version or 'current record'}, in force from "
        f"{str(effective_date)[:10]}) {'reached' if reached else 'not reached'} "
        f"by a completed close since then"
    )


def reference_mark(breached: list, reached: bool | None, *, is_short: bool):
    """The lost mark the give-back is measured from.

    Plain rule: the LAST mark given up — the lowest of the lost marks for a
    long, the highest for a short. With the target's vote (`reached`), the
    FIRST mark given up instead: the highest for a long, the lowest for a
    short. `breached` holds `ChartMark`s and must not be empty.
    """
    pick_highest = is_short != bool(reached)
    return (max if pick_highest else min)(breached, key=lambda m: m.price)
