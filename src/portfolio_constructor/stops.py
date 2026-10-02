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
    STOP_RULE_LEVEL_HONOURED,
    STOP_RULE_ABSOLUTE_FLOOR,
    STOP_RULE_ATR_BAND,
    STOP_RULE_OUTSIDE_BAND,
    STOP_REFUSAL_WRONG_SIDE,
    STOP_RULE_SIGNAL_BAR,
    STOP_RULE_STRUCTURAL_NO_ATR,
    STOP_RULE_PRIOR_BAR_NO_ATR,
    STOP_REFUSAL_STOP_NOT_FINITE,
    STOP_REFUSAL_ENTRY_NOT_FINITE,
    STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY,
    STOP_REFUSAL_NO_VALID_STOP,
    STOP_REFUSAL_NO_STRUCTURAL_TARGET,
    STOP_REFUSAL_REWARD_BELOW_RISK,
)


from src.portfolio_constructor.entry_stop.resolver import EntryStopResolver
from src.portfolio_constructor.shim_guard import _is_class_shim


class _StopMixin:
    """Stop and reward-to-risk resolution for `PortfolioConstructor` (moved verbatim)."""

    _STOP_GEOMETRY_COLLABORATORS = (
        ("cfg", "cfg"),
    )

    def _stop_geometry(self):
        """Build the standalone StopGeometry from this host's collaborators."""
        from src.portfolio_constructor.stop_geometry import StopGeometry
        return StopGeometry(**{
            param: getattr(self, attr)
            for param, attr in self._STOP_GEOMETRY_COLLABORATORS
        })

    def _entry_stop_resolver(self) -> EntryStopResolver:
        """Thin shim: builds the standalone object per call from this host's collaborators
        (bodies moved to src/portfolio_constructor/entry_stop/resolver.py)."""
        return EntryStopResolver(
            cfg=self.cfg,
            derive_target=self._derive_target,
            note_data_fault=self._note_data_fault,
            note_parity_standdown=self._note_parity_standdown,
            note_refusal=self._note_refusal,
            parity_verdict=self._parity_verdict,
            record_parity_refusal=self._record_parity_refusal,
            resolve_stop=self._resolve_stop,
            unpriceable_symbols=self._unpriceable_symbols,
            reward_risk_at=self._reward_risk_at,
            derive_structural_stop_no_atr=self._derive_structural_stop_no_atr,
            level_backing_stop=self._level_backing_stop,
            stop_atr_multiple=self._stop_atr_multiple,
            # A moved body passed back in would overwrite the resolver's own method
            # with a call back into it: pass one ONLY when it is not this mixin's shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (("widen_stop_past_noise", "_widen_stop_past_noise"),)
                if not _is_class_shim(getattr(self, attr), attr, _StopMixin)
            },
        )

    def _resolve_entry_and_stop(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/entry_stop/resolver.py."""
        return self._entry_stop_resolver()._resolve_entry_and_stop(*args, **kwargs)

    def _stop_atr_multiple(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/stop_geometry.py."""
        return self._stop_geometry()._stop_atr_multiple(*args, **kwargs)

    def _level_backing_stop(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/stop_geometry.py."""
        return self._stop_geometry()._level_backing_stop(*args, **kwargs)

    def _derive_structural_stop_no_atr(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/stop_geometry.py."""
        return self._stop_geometry()._derive_structural_stop_no_atr(*args, **kwargs)

    def _reward_risk_at(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/stop_geometry.py."""
        return self._stop_geometry()._reward_risk_at(*args, **kwargs)

    def real_reward_risk_preview(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/entry_stop/resolver.py."""
        return self._entry_stop_resolver().real_reward_risk_preview(*args, **kwargs)

    def _widen_stop_past_noise(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/entry_stop/resolver.py."""
        return self._entry_stop_resolver()._widen_stop_past_noise(*args, **kwargs)

    def shipped_stop_rule(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/stop_geometry.py."""
        return self._stop_geometry().shipped_stop_rule(*args, **kwargs)

    def shipped_stop_level_basis(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/stop_geometry.py."""
        return self._stop_geometry().shipped_stop_level_basis(*args, **kwargs)

    def _resolve_stop(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/stop_geometry.py."""
        return self._stop_geometry()._resolve_stop(*args, **kwargs)
