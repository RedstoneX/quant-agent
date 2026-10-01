"""RiskGateMixin — the thin delegating face `TradingPipeline` still composes.

Conversion step 7. Every body lives in `src.pipeline_risk_gate.RiskGate`;
nothing here decides anything. The seven names stay on the pipeline because
`src/stage_risk.py` and the tests call them there, and because tests replace
them on the instance (`pipeline._apply_risk_modifications = MagicMock(...)`),
which an instance attribute still wins over.

The gate is resolved on EVERY call from the pipeline's live collaborators:
`TradingPipeline.__init__` builds one (`self.risk_gate`), but ~58 tests build
the pipeline via `__new__` and then assign `risk_engine`, `db`, `config` or `_sweeper`
afterwards, so a gate frozen at construction would silently read stale
collaborators. The cached gate is reused only while its collaborators are
the very objects the pipeline holds now; otherwise a fresh one is built
(three attribute assignments — no I/O, no state).
"""
from __future__ import annotations

from src.pipeline_risk_gate import RiskGate


class RiskGateMixin:
    """Delegates the seven risk-gate names to a `RiskGate`; see module docstring."""

    @property
    def _risk_gate(self) -> RiskGate:
        risk_engine = getattr(self, "risk_engine", None)
        db = getattr(self, "db", None)
        config = getattr(self, "config", None)
        sweeper = self._sweeper
        gate = self.__dict__.get("risk_gate")
        if (
            isinstance(gate, RiskGate)
            and gate.risk_engine is risk_engine
            and gate.db is db
            and gate.config is config
            and gate._sweeper == sweeper
        ):
            return gate
        return RiskGate(risk_engine=risk_engine, db=db, sweeper=sweeper, config=config)

    def _filter_hard_risk_decisions(self, *args, **kwargs):
        return self._risk_gate._filter_hard_risk_decisions(*args, **kwargs)

    def _persist_hard_risk_block(self, *args, **kwargs):
        return self._risk_gate._persist_hard_risk_block(*args, **kwargs)

    def _apply_risk_modifications(self, *args, **kwargs):
        return self._risk_gate._apply_risk_modifications(*args, **kwargs)

    def _risk_mod_floor_breach(self, *args, **kwargs):
        return self._risk_gate._risk_mod_floor_breach(*args, **kwargs)

    @staticmethod
    def _reconcile_size_to_risk_budget(*args, **kwargs):
        return RiskGate._reconcile_size_to_risk_budget(*args, **kwargs)

    @staticmethod
    def _has_actionable_signal_fn(*args, **kwargs):
        return RiskGate._has_actionable_signal_fn(*args, **kwargs)

    @staticmethod
    def _refuse_queued_earnings_buys(*args, **kwargs):
        return RiskGate._refuse_queued_earnings_buys(*args, **kwargs)
