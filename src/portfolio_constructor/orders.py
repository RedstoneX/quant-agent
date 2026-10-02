"""Portfolio Constructor — turns PM target-state into concrete orders.

Phase 2 of the architecture work. Previously the LLM (Portfolio Manager)
emitted TradeDecision objects directly, including entry_price / stop_loss /
take_profit. That put the LLM dangerously close to the execution layer:
- fat-finger-protection patches
- vol-adjusted sizing patches
- stop-limit buffer patches
- sub-penny quantize patches
...were all band-aids for "LLM output an execution detail it shouldn't own."

Now PM emits TargetPosition (target_weight_pct, conviction, thesis,
invalid_if) and this module derives the actual orders from:
- Target state
- Current positions (broker truth)
- TA's ATR + suggested stop (for stop distance)
- Broker's live price (for entry price)
- Total equity + cash (for sizing)

The constructor is deterministic and unit-testable. LLM creativity is
confined to intent; math is code.
"""

from __future__ import annotations

import json
import math
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.data.levels import (
    describe_stop_level_basis,
    COVERAGE_UNKNOWN,
    FAULT_NO_ANALYSIS,
    FAULT_NO_ENTRY,
    FAULT_NO_PRICE,
    TargetDerivation,
    derive_structural_target,
    level_zone_halfwidth,
    stop_rests_on_level,
    touch_probability,
)
from src.data.technical import LONGEST_INDICATOR_WINDOW
from src.models import (
    Position, TargetPosition, TechAnalysisResult, TradeDecision,
    reward_to_risk, stated_soft_exit,
)
from src.risk.constants import (
    REWARD_RISK_PARITY,
    gap_adjusted_risk_per_share,
    reward_risk_floor_applies,
    risk_budget_allocation_pct,
    reward_risk_parity_refuses,
)

from src.portfolio_constructor.config import logger  # the package logger, named as before the split
from src.portfolio_constructor.config import (
    _named_reduction_trigger,
    STOP_REFUSAL_SIZED_TO_ZERO,
    STOP_REFUSAL_TARGET_NOT_ABOVE_ENTRY,
    STOP_REFUSAL_TARGET_NOT_BELOW_ENTRY,
    RiskPlan,
)


class _OrderBuildMixin:
    """Per-leg order builders for `PortfolioConstructor`.

    Bodies lifted VERBATIM into the `src/portfolio_constructor/order_build/`
    package, one standalone piece per leg: `LongEntryBuilder._build_buy`
    (long_entry.py), `ShortEntryBuilder._build_short` (short_entry.py) and the
    collaborator-free `ExitOrderBuilders` (`_build_sell`, `_build_cover`,
    `_hold_decision`; exits.py). Each entry shim builds its object PER CALL so
    a collaborator swapped on the host after construction is what the body
    sees. No collaborator passed below is itself a lifted method, so the shim
    cannot call back into itself.
    """

    _ORDER_BUILDER_COLLABORATORS = (
        ("cfg", "cfg"),
        ("derive_target", "_derive_target"),
        ("resolve_entry_and_stop", "_resolve_entry_and_stop"),
        ("apply_sector_dial", "_apply_sector_dial"),
        ("note_refusal", "_note_refusal"),
        ("shipped_stop_rule", "shipped_stop_rule"),
        ("shipped_stop_level_basis", "shipped_stop_level_basis"),
        ("target_note", "_target_note"),
    )

    def _order_builder_collaborators(self) -> dict:
        return {param: getattr(self, attr) for param, attr in self._ORDER_BUILDER_COLLABORATORS}

    def _long_entry_builder(self):
        """Build the standalone LongEntryBuilder from this host's collaborators."""
        from src.portfolio_constructor.order_build.long_entry import LongEntryBuilder
        return LongEntryBuilder(**self._order_builder_collaborators())

    def _short_entry_builder(self):
        """Build the standalone ShortEntryBuilder from this host's collaborators."""
        from src.portfolio_constructor.order_build.short_entry import ShortEntryBuilder
        return ShortEntryBuilder(**self._order_builder_collaborators())

    @staticmethod
    def _hold_decision(*args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/order_build/exits.py."""
        from src.portfolio_constructor.order_build.exits import ExitOrderBuilders
        return ExitOrderBuilders._hold_decision(*args, **kwargs)

    @staticmethod
    def _build_sell(*args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/order_build/exits.py."""
        from src.portfolio_constructor.order_build.exits import ExitOrderBuilders
        return ExitOrderBuilders._build_sell(*args, **kwargs)

    @staticmethod
    def _build_cover(*args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/order_build/exits.py."""
        from src.portfolio_constructor.order_build.exits import ExitOrderBuilders
        return ExitOrderBuilders._build_cover(*args, **kwargs)

    def _build_buy(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/order_build/long_entry.py."""
        return self._long_entry_builder()._build_buy(*args, **kwargs)

    def _build_short(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/order_build/short_entry.py."""
        return self._short_entry_builder()._build_short(*args, **kwargs)
