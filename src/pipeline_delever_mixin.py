"""DeleverMixin — the thin delegating face `TradingPipeline` still composes.

Conversion step 11. Every body lives in `src.pipeline_delever.DeleverService`;
nothing here decides anything. The twelve names stay on the pipeline because
the session bodies in `src/pipeline.py` and the tests call them there, and
because tests replace them on the instance (`pipeline._force_delever =
MagicMock(...)`, `pipeline._resolve_gross_ceiling = lambda ctx: ...`), which an
instance attribute still wins over. Any such instance override of a delever
name is forwarded onto the service so an inner call (`_enforce_gross_ceiling`
-> `_resolve_gross_ceiling`) still reaches the test's stand-in, exactly as it
did when the bodies were inherited.

The service is resolved on EVERY call from the pipeline's live collaborators:
`TradingPipeline.__init__` builds one (`self.delever`), but tests build the
pipeline via `__new__` and then assign `broker`, `db`, `config`,
`_submit_protected_sell` or `cash_sweeper` afterwards, so a service frozen at
construction would silently read stale collaborators. Building one is nine
attribute assignments — no I/O, no state.
"""
from __future__ import annotations

from src.pipeline_delever import DeleverService

_NAMES = (
    "_live_delever_price", "_force_delever",
    "_alert_owner_force_delever_incomplete", "_resolve_gross_ceiling",
    "_enforce_gross_ceiling", "_is_margin_floor_breach", "_conviction_cut_order",
    "_enforce_gross_ceiling_by_conviction", "_discharge_deferred_gross_ceiling",
    "_submit_gross_ceiling_trims", "_record_delever_shortfall",
    "_alert_owner_delever_incomplete",
)


class DeleverMixin:
    """Delegates the twelve delever names to a `DeleverService`; see module docstring."""

    @property
    def _delever(self) -> DeleverService:
        service = DeleverService(
            config=getattr(self, "config", None),
            broker=getattr(self, "broker", None),
            db=getattr(self, "db", None),
            protection=self,
            sweeper=self._sweeper,
            sweep_symbol=self._sweep_symbol,
            full_sell_qty=self._full_sell_qty,
            format_qty=self._format_qty,
            compute_deployable_cash=self._compute_deployable_cash,
        )
        for name in _NAMES:
            override = self.__dict__.get(name)
            if override is not None:
                setattr(service, name, override)
        return service

    def _live_delever_price(self, *args, **kwargs):
        return self._delever._live_delever_price(*args, **kwargs)

    def _force_delever(self, *args, **kwargs):
        return self._delever._force_delever(*args, **kwargs)

    def _alert_owner_force_delever_incomplete(self, *args, **kwargs):
        return self._delever._alert_owner_force_delever_incomplete(*args, **kwargs)

    def _resolve_gross_ceiling(self, *args, **kwargs):
        return self._delever._resolve_gross_ceiling(*args, **kwargs)

    def _enforce_gross_ceiling(self, *args, **kwargs):
        return self._delever._enforce_gross_ceiling(*args, **kwargs)

    def _is_margin_floor_breach(self, *args, **kwargs):
        return self._delever._is_margin_floor_breach(*args, **kwargs)

    def _conviction_cut_order(self, *args, **kwargs):
        return self._delever._conviction_cut_order(*args, **kwargs)

    def _enforce_gross_ceiling_by_conviction(self, *args, **kwargs):
        return self._delever._enforce_gross_ceiling_by_conviction(*args, **kwargs)

    def _discharge_deferred_gross_ceiling(self, *args, **kwargs):
        return self._delever._discharge_deferred_gross_ceiling(*args, **kwargs)

    def _submit_gross_ceiling_trims(self, *args, **kwargs):
        return self._delever._submit_gross_ceiling_trims(*args, **kwargs)

    def _record_delever_shortfall(self, *args, **kwargs):
        return self._delever._record_delever_shortfall(*args, **kwargs)

    def _alert_owner_delever_incomplete(self, *args, **kwargs):
        return self._delever._alert_owner_delever_incomplete(*args, **kwargs)
