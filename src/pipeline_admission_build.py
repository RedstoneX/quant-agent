"""Build an `AdmissionService` from plain collaborators -- no pipeline anywhere.

Conversion step 8b. `AdmissionMixin` (deleted with this change) composed the
service onto `TradingPipeline` and wrapped every collaborator in a `_Late`
shell that held a lambda bound to the pipeline: the service could reach the
whole pipeline through it, so the boundary was a name, not a wall.

This module takes the four collaborators and the live constructor by VALUE and
returns a service that can reach nothing else. It imports no pipeline module,
so it can be called -- and tested -- with fakes and nothing else.

`db` may be None (tests drive a bare pipeline): the journal resolves the
writer at write time, so a service that never writes never needs one.
"""
from __future__ import annotations

from src.pipeline_admission import AdmissionService
from src.ports.event_journal import EventJournal


class DatabaseJournal(EventJournal):
    """A journal over a database handle, resolved at write time.

    The handle is a value, not a pipeline: resolving the WRITER late is what
    keeps a service built before the database opens usable for every method
    that never writes, which is what the deleted `_Late` shell provided.
    """

    def __init__(self, db: object) -> None:
        self.db = db

    def _journal(self) -> EventJournal:
        from src.storage.event_journal import DatabaseEventJournal

        return DatabaseEventJournal(self.db)

    def persist_evidence(self, **kwargs) -> None:
        self._journal().persist_evidence(**kwargs)

    def record_pipeline_event(self, **kwargs) -> None:
        self._journal().record_pipeline_event(**kwargs)


def build_admission_service(
    *, config, broker, market, db, sec_form4_provider=None, portfolio_constructor=None,
) -> AdmissionService:
    """The admission service for these collaborators, holding no caller.

    `portfolio_constructor` is read ONCE, here, into the constructor-config
    callable the service already takes: the universe screen's volatility
    ceiling must agree with the widest stop the live constructor can place
    (board item 185), and `None` leaves the service on its `config.risk`
    fallback.
    """
    from src.risk.constants import live_constructor_cfg_or_none

    return AdmissionService(
        config=config,
        broker=broker,
        market=market,
        journal=DatabaseJournal(db),
        sec_form4_provider=sec_form4_provider,
        constructor_cfg_fn=lambda: live_constructor_cfg_or_none(portfolio_constructor),
    )


#: What `build_admission_service` itself puts on the service. Everything else
#: in its `__dict__` was put there by a caller (tests replace a method on the
#: service) and must survive a rebuild.
_BUILT_FIELDS = frozenset(
    {"config", "broker", "market", "journal", "sec_form4_provider", "_constructor_cfg_fn"}
)


def carry_over_stand_ins(old: AdmissionService, new: AdmissionService) -> AdmissionService:
    """Move anything a CALLER put on `old` onto `new`, and return `new`.

    A service is rebuilt when the collaborators it was built from are replaced
    (a bare test pipeline assigns them one at a time). Without this, a stand-in
    put on the service before that assignment would vanish silently -- the
    worst kind of failure, because the real body then runs unnoticed.
    """
    for name, value in old.__dict__.items():
        if name not in _BUILT_FIELDS:
            setattr(new, name, value)
    return new
