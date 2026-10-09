"""The one thing the holding-discipline claim check needs from the PM agent.

A risk guard must not depend on the agent whose decisions it checks, so the
claim check takes the parser as an argument typed by this Protocol and never
imports an agent. The concrete parser is supplied by the pipeline layer
(`src/exits/pm_claim_check.py`).
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

__all__ = ["StateChangeParser"]


class StateChangeParser(Protocol):
    """Parse the rendered active-state-change block into
    `{iso_date: {SYMBOL: {direction, ...}}}`."""

    def __call__(
        self,
        active_state_changes: str,
        asof: date | None = None,
    ) -> dict[str, dict[str, set[str]]]: ...
