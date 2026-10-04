"""Research continuity: may yesterday's paid research be reused, and can a lost seat be healed?

Step 7 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210), clusters S + T + U + V.
Moved VERBATIM out of `src/pipeline.py` as a mixin, so `TradingPipeline` keeps
every one of these as its own attribute and every test that calls, patches or
reads them through the class is untouched.

One question is answered here: whether the desk may stand on evidence it has
already paid for. The change detectors (macro regime, FRED prints, a newer
material news wire) decide whether a stored answer is still true; the
carry-forward readers hand the morning's macro, news, earnings and insider
payloads to an intraday tick with an honest `data_status` rather than a
silent reuse; the Form-4 backlog alert and the congressional refresh say when
an evidence lane is behind; and the seat healing path spends at most one paid
retry to recover a seat that was lost, recording what it did either way.

`CarryForward` travels with it: the dataclass is read and constructed only by
the moved bodies, and this module may not import `src.pipeline`. It is
re-exported from `src.pipeline`, so `from src.pipeline import CarryForward`
keeps working.

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import logging

from src.cost_circuit.parts.shim_guard import _is_class_shim
from src.pipeline_stages import _persist_evidence
from src.research_continuity.carry_forward import CarryForward, CarryForwardReaders
from src.research_continuity.change_detectors import ResearchChangeDetectors
from src.research_continuity.form4_backlog import Form4BacklogRecorder
from src.research_continuity.heal_records import HealRecords
from src.research_continuity.insider_memory import InsiderMemory
from src.research_continuity.seat_heal_path import SeatHealer

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


def _lifted_collab(host, name: str):
    """The host's replacement for a lifted body, or None when the host only has
    the mixin's own shim for it (passing that back would recurse)."""
    fn = getattr(host, name, None)
    if fn is None or _is_class_shim(fn, name, ResearchContinuityMixin):
        return None
    return fn


def _change_detectors(host) -> ResearchChangeDetectors:
    """Standalone change detectors over the host's CURRENT collaborators (bodies
    moved to src/research_continuity/change_detectors.py). Built per call so a
    collaborator rebound after construction is what the body sees; a module
    function rather than a method so a test that binds one shim onto a bare
    object with `__get__` still reaches the moved code. The peek-items slot is
    read and written on the host, where `_peeked_news_wire_text` always found it."""
    return ResearchChangeDetectors(
        macro_store=getattr(host, "macro_store", None),
        macro=getattr(host, "macro", None),
        news_provider=getattr(host, "news_provider", None),
        news_store=getattr(host, "news_store", None),
        config=getattr(host, "config", None),
        peek_items_get=lambda: getattr(host, "_last_news_peek_items", None),
        peek_items_set=lambda items: setattr(host, "_last_news_peek_items", items),
        macro_history_regime_changed=_lifted_collab(host, "_macro_history_regime_changed"),
        macro_series_prints_changed=_lifted_collab(host, "_macro_series_prints_changed"),
        live_macro_series_prints=_lifted_collab(host, "_live_macro_series_prints"),
        watched_research_symbols=_lifted_collab(host, "_watched_research_symbols"),
        peek_news_headlines=_lifted_collab(host, "_peek_news_headlines"),
    )


class _HostJournal:
    """The evidence journal a part writes through: `_persist_evidence` over the
    host's CURRENT `db`, resolved per write so a rebound store or a patched
    `_persist_evidence` is what the moved body reaches."""

    def __init__(self, host) -> None:
        self._host = host

    def persist_evidence(self, **kwargs) -> None:
        _persist_evidence(getattr(self._host, "db", None), **kwargs)


def _form4_backlog(host) -> Form4BacklogRecorder:
    """Standalone Form 4 backlog recorder writing the host's journal (bodies
    moved to src/research_continuity/form4_backlog.py)."""
    return Form4BacklogRecorder(journal=_HostJournal(host))


def _insider_memory(host) -> InsiderMemory:
    """Standalone insider-seat memory over the host's CURRENT collaborators
    (bodies moved to src/research_continuity/insider_memory.py). The watched
    symbols come from the host's own change-detector entry point so an
    instance-level double still lands; a lifted body is passed in only when
    the host replaced it, never as the mixin's own shim."""
    return InsiderMemory(
        db=getattr(host, "db", None),
        smart_money_provider=getattr(host, "smart_money_provider", None),
        watched_research_symbols=getattr(host, "_watched_research_symbols", None),
        form4_freshness=_lifted_collab(host, "_form4_freshness"),
        form4_known_accessions=_lifted_collab(host, "_form4_known_accessions"),
        findings_from_specialist_evidence=_lifted_collab(host, "_findings_from_specialist_evidence"),
        load_remembered_insider_findings=_lifted_collab(host, "_load_remembered_insider_findings"),
        specialist_insider_as_of=_lifted_collab(host, "_specialist_insider_as_of"),
        insider_same_session=_lifted_collab(host, "_insider_same_session"),
        carry_forward_insider=_lifted_collab(host, "_carry_forward_insider"),
    )


def _carry_forward_readers(host) -> CarryForwardReaders:
    """Standalone carry-forward readers over the host's CURRENT collaborators
    (bodies moved to src/research_continuity/carry_forward.py); same rules as
    `_insider_memory`."""
    return CarryForwardReaders(
        db=getattr(host, "db", None),
        macro_store=getattr(host, "macro_store", None),
        news_store=getattr(host, "news_store", None),
        earnings_provider=getattr(host, "earnings_provider", None),
        load_earnings_analyses=getattr(host, "_load_earnings_analyses", None),
        macro_regime_or_print_changed=getattr(host, "_macro_regime_or_print_changed", None),
        news_has_newer_material_wire=getattr(host, "_news_has_newer_material_wire", None),
        peeked_news_wire_text=getattr(host, "_peeked_news_wire_text", None),
        carry_forward_macro=_lifted_collab(host, "_carry_forward_macro"),
        latest_news_read_today=_lifted_collab(host, "_latest_news_read_today"),
        carry_forward_news=_lifted_collab(host, "_carry_forward_news"),
        carry_forward_earnings=_lifted_collab(host, "_carry_forward_earnings"),
    )


def _heal_records(host) -> HealRecords:
    """Standalone heal records over the host's CURRENT storage and journal
    (bodies moved to src/research_continuity/heal_records.py). The peek-items
    slot is read on the host, where the expiry peek writes it."""
    return HealRecords(
        db=getattr(host, "db", None),
        journal=_HostJournal(host),
        macro_store=getattr(host, "macro_store", None),
        macro=getattr(host, "macro", None),
        news_store=getattr(host, "news_store", None),
        peek_items_get=lambda: getattr(host, "_last_news_peek_items", None),
        record_heal=_lifted_collab(host, "_record_heal"),
        persist_heal_call=_lifted_collab(host, "_persist_heal_call"),
        persist_healed_macro_store=_lifted_collab(host, "_persist_healed_macro_store"),
        cover_healed_news_wire=_lifted_collab(host, "_cover_healed_news_wire"),
    )


def _seat_healer(host) -> SeatHealer:
    """Standalone seat healer over the host's CURRENT collaborators (bodies
    moved to src/research_continuity/seat_heal_path.py). The analyst seats and
    the paid-analysis gate are looked up on the host per call, which is where
    the intraday tick and its tests install them; the four record callables
    are the host's entry points (a double on the host still lands)."""
    return SeatHealer(
        db=getattr(host, "db", None),
        record_heal=lambda *a, **k: host._record_heal(*a, **k),
        persist_heal_call=lambda *a, **k: host._persist_heal_call(*a, **k),
        persist_healed_macro_store=lambda *a, **k: host._persist_healed_macro_store(*a, **k),
        cover_healed_news_wire=lambda *a, **k: host._cover_healed_news_wire(*a, **k),
        require_paid_analysis=getattr(host, "_require_paid_analysis", None),
        agent_for=lambda name: getattr(host, name, None),
        try_one_paid_research_retry=_lifted_collab(host, "_try_one_paid_research_retry"),
        heal_lost_research_seats=_lifted_collab(host, "_heal_lost_research_seats"),
    )


class ResearchContinuityMixin:
    """Change detection, carry-forward and seat healing for TradingPipeline."""

    # --- lifted to src/research_continuity/change_detectors.py (ResearchChangeDetectors); thin shims follow ---
    def _macro_regime_or_print_changed(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/change_detectors.py)."""
        return _change_detectors(self)._macro_regime_or_print_changed(*args, **kwargs)

    def _macro_history_regime_changed(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/change_detectors.py)."""
        return _change_detectors(self)._macro_history_regime_changed(*args, **kwargs)

    def _live_macro_series_prints(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/change_detectors.py)."""
        return _change_detectors(self)._live_macro_series_prints(*args, **kwargs)

    def _macro_series_prints_changed(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/change_detectors.py)."""
        return _change_detectors(self)._macro_series_prints_changed(*args, **kwargs)

    def _watched_research_symbols(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/change_detectors.py)."""
        return _change_detectors(self)._watched_research_symbols(*args, **kwargs)

    def _peek_news_headlines(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/change_detectors.py)."""
        return _change_detectors(self)._peek_news_headlines(*args, **kwargs)

    def _peeked_news_wire_text(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/change_detectors.py)."""
        return _change_detectors(self)._peeked_news_wire_text(*args, **kwargs)

    def _news_has_newer_material_wire(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/change_detectors.py)."""
        return _change_detectors(self)._news_has_newer_material_wire(*args, **kwargs)

    # --- lifted to src/research_continuity/form4_backlog.py (Form4BacklogRecorder); thin shims follow ---
    def _record_form4_backlog(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/form4_backlog.py)."""
        return _form4_backlog(self)._record_form4_backlog(*args, **kwargs)

    def _record_congressional_refresh(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/form4_backlog.py)."""
        return _form4_backlog(self)._record_congressional_refresh(*args, **kwargs)

    def _alert_form4_backlog_before_open(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/form4_backlog.py)."""
        return _form4_backlog(self)._alert_form4_backlog_before_open(*args, **kwargs)

    # --- lifted to src/research_continuity/insider_memory.py (InsiderMemory); thin shims follow ---
    def _form4_freshness(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/insider_memory.py)."""
        return _insider_memory(self)._form4_freshness(*args, **kwargs)

    def _form4_known_accessions(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/insider_memory.py)."""
        return _insider_memory(self)._form4_known_accessions(*args, **kwargs)

    def _findings_from_specialist_evidence(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/insider_memory.py)."""
        return _insider_memory(self)._findings_from_specialist_evidence(*args, **kwargs)

    def _load_remembered_insider_findings(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/insider_memory.py)."""
        return _insider_memory(self)._load_remembered_insider_findings(*args, **kwargs)

    def _specialist_insider_as_of(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/insider_memory.py)."""
        return _insider_memory(self)._specialist_insider_as_of(*args, **kwargs)

    def _insider_same_session(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/insider_memory.py)."""
        return _insider_memory(self)._insider_same_session(*args, **kwargs)

    def _carry_forward_insider(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/insider_memory.py)."""
        return _insider_memory(self)._carry_forward_insider(*args, **kwargs)

    # --- lifted to src/research_continuity/carry_forward.py (CarryForwardReaders); thin shims follow ---
    def _carry_forward_macro(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/carry_forward.py)."""
        return _carry_forward_readers(self)._carry_forward_macro(*args, **kwargs)

    def _latest_news_read_today(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/carry_forward.py)."""
        return _carry_forward_readers(self)._latest_news_read_today(*args, **kwargs)

    def _carry_forward_news(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/carry_forward.py)."""
        return _carry_forward_readers(self)._carry_forward_news(*args, **kwargs)

    def _carry_forward_earnings(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/carry_forward.py)."""
        return _carry_forward_readers(self)._carry_forward_earnings(*args, **kwargs)

    # --- lifted to src/research_continuity/heal_records.py (HealRecords); thin shims follow ---
    def _record_heal(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/heal_records.py)."""
        return _heal_records(self)._record_heal(*args, **kwargs)

    def _persist_heal_call(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/heal_records.py)."""
        return _heal_records(self)._persist_heal_call(*args, **kwargs)

    def _persist_healed_macro_store(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/heal_records.py)."""
        return _heal_records(self)._persist_healed_macro_store(*args, **kwargs)

    def _cover_healed_news_wire(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/heal_records.py)."""
        return _heal_records(self)._cover_healed_news_wire(*args, **kwargs)

    # --- lifted to src/research_continuity/seat_heal_path.py (SeatHealer); thin shims follow ---
    def _try_one_paid_research_retry(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/seat_heal_path.py)."""
        return _seat_healer(self)._try_one_paid_research_retry(*args, **kwargs)

    def _heal_lost_research_seats(self, *args, **kwargs):
        """Thin shim (body moved to src/research_continuity/seat_heal_path.py)."""
        return _seat_healer(self)._heal_lost_research_seats(*args, **kwargs)
