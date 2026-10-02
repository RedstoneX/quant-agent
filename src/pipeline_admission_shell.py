"""The thin shell `TradingPipeline` keeps around `AdmissionService` (step 8).

Every `_admit_*`, `_filter_supported_symbols` and gate name stays a method of
`TradingPipeline`, so `src/stage_risk.py`, the three bound methods handed to
the morning stage, and every test that calls or replaces one are untouched.
Nothing here may import `src.pipeline`.
"""

from collections.abc import Callable

from src.pipeline_admission import AdmissionService
from src.ports.event_journal import EventJournal


class _Late:
    """Resolve a pipeline attribute at first USE, not at construction.

    A bare test pipeline may lack `broker`/`market`; the old mixin only failed
    if a method actually touched the missing one, and this keeps that.
    """

    def __init__(self, get: Callable[[], object]) -> None:
        self._get = get

    def __getattr__(self, name: str):
        return getattr(self._get(), name)


class _PipelineDatabaseJournal(EventJournal):
    """The pipeline's CURRENT `db` as a journal, resolved at write time.

    Resolving late keeps a bare test pipeline with no `db` working for every
    method that never writes, exactly as `self.db` did before the move.
    """

    def __init__(self, get_db: Callable[[], object]) -> None:
        self._get_db = get_db

    def _journal(self) -> EventJournal:
        from src.storage.event_journal import DatabaseEventJournal

        return DatabaseEventJournal(self._get_db())

    def persist_evidence(self, **kwargs) -> None:
        self._journal().persist_evidence(**kwargs)

    def record_pipeline_event(self, **kwargs) -> None:
        self._journal().record_pipeline_event(**kwargs)

    def insert_agent_log(self, **fields) -> None:
        self._journal().insert_agent_log(**fields)


class AdmissionMixin:
    """Thin delegating shell: `TradingPipeline` keeps every name it always had.

    A fresh `AdmissionService` is built per call from the pipeline's CURRENT
    collaborators, so a test that rebinds `pipeline.broker`/`market`/`config`
    after construction still reaches the moved code.
    """

    def _admission_service(self) -> AdmissionService:
        return AdmissionService(
            config=_Late(lambda: self.config), broker=_Late(lambda: self.broker),
            market=_Late(lambda: self.market),
            journal=_PipelineDatabaseJournal(lambda: self.db),
            sec_form4_provider=getattr(self, "sec_form4_provider", None),
            constructor_cfg_fn=self._constructor_cfg_or_none,
        )

    def _filter_supported_symbols(self, *args, **kwargs):
        return self._admission_service()._filter_supported_symbols(*args, **kwargs)

    def _evaluate_external_admission_gates(self, *args, **kwargs):
        return self._admission_service()._evaluate_external_admission_gates(*args, **kwargs)

    def _universe_screen_enabled(self, *args, **kwargs):
        return self._admission_service()._universe_screen_enabled(*args, **kwargs)

    def _universe_screen_sources(self, *args, **kwargs):
        return self._admission_service()._universe_screen_sources(*args, **kwargs)

    def _evaluate_screened_admission(self, *args, **kwargs):
        return self._admission_service()._evaluate_screened_admission(*args, **kwargs)

    def _form4_admission_is_current(self, *args, **kwargs):
        return self._admission_service()._form4_admission_is_current(*args, **kwargs)

    def _admit_screened_universe_symbols(self, *args, **kwargs):
        return self._admission_service()._admit_screened_universe_symbols(*args, **kwargs)

    def _run_universe_screen(self, *args, **kwargs):
        return self._admission_service()._run_universe_screen(*args, **kwargs)

    def _attach_universe_changes(self, *args, **kwargs):
        return self._admission_service()._attach_universe_changes(*args, **kwargs)

    def _admit_nominated_external_symbols(self, *args, **kwargs):
        return self._admission_service()._admit_nominated_external_symbols(*args, **kwargs)

    def _admit_transient_smart_money_symbols(self, *args, **kwargs):
        return self._admission_service()._admit_transient_smart_money_symbols(*args, **kwargs)
