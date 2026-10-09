"""Order a session's SELLs so a ranked-margin rotation's close goes last (lifted from pipeline_rotation_exec)."""

from __future__ import annotations


def _rotation_sell_last(sell_decisions: list, ctx) -> list:
    """This session's SELLs with a RANKED-MARGIN rotation's close moved to
    the END, and every other order preserved.

    Board item 39. The rotation's close is the only exit whose paired BUY
    can be refused for lack of the room the close frees, so it is the only
    one that must not be submitted until that question is answered — and
    the question is easiest to answer once every OTHER exit has a terminal
    status and the account has been re-read. Going last is what turns the
    other exits from something to project into something to measure.

    A no-op on every session without a ranked-margin rotation, which is
    every session while `execution.rotation_ranked_margin_enabled` is off.
    """
    rotation = getattr(ctx, "rotation", None)
    if not isinstance(rotation, dict) or rotation.get("tier") != "ranked_margin":
        return sell_decisions
    held = str(rotation.get("held_symbol") or "").strip().upper()
    if not held:
        return sell_decisions
    others = [d for d in sell_decisions if str(getattr(d, "symbol", "") or "").strip().upper() != held]
    rotation_legs = [d for d in sell_decisions if str(getattr(d, "symbol", "") or "").strip().upper() == held]
    return others + rotation_legs
