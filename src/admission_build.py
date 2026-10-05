"""`build_admission_service` — an `AdmissionService` from values, not a pipeline.

Conversion step 8, finished. The old `AdmissionMixin`
(`src/pipeline_admission_shell.py`, deleted 2026-10-05) forwarded eleven names
off `TradingPipeline` into an `AdmissionService` it rebuilt per call from
`self`. That is the cosmetic split: the service could not be built without a
pipeline in hand. This builder takes the four collaborators and the one
callable BY VALUE, so admission can be constructed, driven and tested with no
pipeline anywhere — see `tests/test_admission_service.py`, which proves in a
subprocess that reaching the service never imports `src.pipeline`.

`db` may be `None`: `DatabaseEventJournal` only stores it, and every write on
the money path is already best-effort, so a caller with no database still gets
a service whose read-only gates work.
"""
from __future__ import annotations

from collections.abc import Callable

from src.pipeline_admission import AdmissionService
from src.risk.constants import live_constructor_cfg_or_none
from src.storage.event_journal import DatabaseEventJournal


def build_admission_service(
    *, config, broker=None, market=None, db=None, sec_form4_provider=None,
    constructor_cfg_fn: Callable[[], object | None] | None = None,
    portfolio_constructor=None,
) -> AdmissionService:
    """The universe-admission service, from plain collaborators.

    `constructor_cfg_fn` wins when given; otherwise the live
    `ConstructorConfig` is read off `portfolio_constructor`, which is where
    the deleted shell read it from (board item 185: the screen's volatility
    ceiling is 1 / the widest stop that object can produce).
    """
    if constructor_cfg_fn is None:
        def constructor_cfg_fn() -> object | None:
            return live_constructor_cfg_or_none(portfolio_constructor)
    return AdmissionService(
        config=config, broker=broker, market=market,
        journal=DatabaseEventJournal(db),
        sec_form4_provider=sec_form4_provider,
        constructor_cfg_fn=constructor_cfg_fn,
    )


class AdmissionSlot:
    """Expose admission without adding methods to ``TradingPipeline``.

    Until a caller explicitly assigns a service, every read is built from the
    holder's current collaborators. That preserves the deleted shell's late
    binding for tests that replace collaborators after construction.
    """

    def __set_name__(self, owner, name) -> None:
        self._key = "_" + name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        if self._key in obj.__dict__:
            return obj.__dict__[self._key]
        return build_admission_service(
            config=getattr(obj, "config", None),
            broker=getattr(obj, "broker", None),
            market=getattr(obj, "market", None),
            db=getattr(obj, "db", None),
            sec_form4_provider=getattr(obj, "sec_form4_provider", None),
            portfolio_constructor=getattr(obj, "portfolio_constructor", None),
        )

    def __set__(self, obj, service) -> None:
        obj.__dict__[self._key] = service
