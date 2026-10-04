"""Small pure helpers lifted out of pipeline_stages (re-exported there)."""
from __future__ import annotations

import logging

from src.sentinel.guarded_site import record_site

logger = logging.getLogger("src.pipeline_stages")


def record_stage(owner, where: str, exc: BaseException | None = None) -> None:
    """One counted pass through a stage catch-all (traceback + row); never raises."""
    record_site(owner, where, exc, log=logger, scope="stages")


def _macro_regime(macro_analysis) -> str | None:
    """The regime string, from either a MacroAnalysis or a carried-forward dict."""
    if macro_analysis is None:
        return None
    if isinstance(macro_analysis, dict):
        value = macro_analysis.get("regime")
    else:
        value = getattr(macro_analysis, "regime", None)
    return str(value) if value else None


def _live_stops_from_heat(ctx) -> dict[str, float] | None:
    """{symbol: live broker stop} from the PM facts' heat roll-up, or None.

    The stops behind `_book_risk_inputs`' per-symbol risk, so a trim sized
    from them is sized against the very risk figure the PM was shown.
    """
    heat = getattr(getattr(ctx, "facts", None), "heat", None)
    if heat is None:
        return None
    try:
        return {
            row.symbol.upper(): row.stop
            for row in heat.per_position if row.protected and row.stop
        }
    except Exception as e:  # noqa: BLE001 — never fail the session on this
        record_stage(ctx, "live_stop_map", e)
        return None
