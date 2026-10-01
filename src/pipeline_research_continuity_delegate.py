"""Thin delegating mixin: keeps every `TradingPipeline._<name>` research-continuity
entry point alive while the logic lives in the standalone `ResearchContinuity`.

Conversion step 9. Each method (a module-level helper, so a plain namespace works as `self`) builds a `ResearchContinuity` from the pipeline's
CURRENT collaborators (tests and the pipeline reassign `db`, `macro_store`,
`_load_earnings_analyses` after construction, so they are read per call, never
snapshotted) and forwards. The wire-peek cache `_last_news_peek_items` is the one
piece of state; it is carried onto and back off the pipeline around each call.

External callers that still reach in through the pipeline (remove an entry here
when its caller takes a `ResearchContinuity` instead): `src/pipeline_intraday.py`
(`_carry_forward_*`, `_heal_lost_research_seats`), `src/pipeline_exits.py`
(`_carry_forward_macro`, `_record_heal`), `src/pipeline_stages.py` (`_record_heal`),
`src/agents/portfolio_manager.py` (`_carry_forward_macro/_news`),
`src/risk/exit_guard.py` (`_carry_forward_macro`), `src/seat_heal.py`
(`_persist_heal_call`, `_try_one_paid_research_retry`), `src/evidence_gate.py`
(`_heal_lost_research_seats`), `src/storage/db.py` (`_try_one_paid_research_retry`),
`src/data/congressional_trading.py` (`_alert_form4_backlog_before_open`).
"""

from src.pipeline_research_continuity import ResearchContinuity
from src.storage.event_journal import DatabaseEventJournal


def _build(owner) -> ResearchContinuity:
    """Built per call from the owner's CURRENT attributes; `owner` may be any
    stand-in, because tests call these methods unbound on a plain namespace."""
    g = lambda name: getattr(owner, name, None)  # noqa: E731
    return ResearchContinuity(
        config=g("config"), journal=DatabaseEventJournal(g("db")), db=g("db"),
        macro_store=g("macro_store"), news_store=g("news_store"),
        macro=g("macro"), news_provider=g("news_provider"),
        earnings_provider=g("earnings_provider"),
        smart_money_provider=g("smart_money_provider"),
        macro_analyst=g("macro_analyst"), news_analyst=g("news_analyst"),
        tech_analyst=g("tech_analyst"),
        load_earnings_analyses=g("_load_earnings_analyses"),
        require_paid_analysis=g("_require_paid_analysis"),
    )


def _call(owner, name, args, kwargs):
    rc = _build(owner)
    # A method patched or reassigned on the owner must still be the one the
    # moved bodies reach when they call each other.
    for other, own in vars(ResearchContinuityMixin).items():
        bound = getattr(owner, other, None) if callable(own) and other != name else None
        if bound is not None and getattr(bound, "__func__", None) is not own:
            setattr(rc, other, bound)
    state = getattr(owner, "__dict__", None)  # a bare object() has none
    if state is not None and "_last_news_peek_items" in state:
        rc._last_news_peek_items = state["_last_news_peek_items"]
    try:
        return getattr(rc, name)(*args, **kwargs)
    finally:
        if state is not None:
            owner._last_news_peek_items = rc._last_news_peek_items


class ResearchContinuityMixin:
    def _macro_regime_or_print_changed(self, *args, **kwargs):
        return _call(self, "_macro_regime_or_print_changed", args, kwargs)

    def _macro_history_regime_changed(self, *args, **kwargs):
        return _call(self, "_macro_history_regime_changed", args, kwargs)

    def _live_macro_series_prints(self, *args, **kwargs):
        return _call(self, "_live_macro_series_prints", args, kwargs)

    def _macro_series_prints_changed(self, *args, **kwargs):
        return _call(self, "_macro_series_prints_changed", args, kwargs)

    def _watched_research_symbols(self, *args, **kwargs):
        return _call(self, "_watched_research_symbols", args, kwargs)

    def _peek_news_headlines(self, *args, **kwargs):
        return _call(self, "_peek_news_headlines", args, kwargs)

    def _peeked_news_wire_text(self, *args, **kwargs):
        return _call(self, "_peeked_news_wire_text", args, kwargs)

    def _news_has_newer_material_wire(self, *args, **kwargs):
        return _call(self, "_news_has_newer_material_wire", args, kwargs)

    def _record_form4_backlog(self, *args, **kwargs):
        return _call(self, "_record_form4_backlog", args, kwargs)

    def _record_congressional_refresh(self, *args, **kwargs):
        return _call(self, "_record_congressional_refresh", args, kwargs)

    def _alert_form4_backlog_before_open(self, *args, **kwargs):
        return _call(self, "_alert_form4_backlog_before_open", args, kwargs)

    def _form4_freshness(self, *args, **kwargs):
        return _call(self, "_form4_freshness", args, kwargs)

    def _form4_known_accessions(self, *args, **kwargs):
        return _call(self, "_form4_known_accessions", args, kwargs)

    def _findings_from_specialist_evidence(self, *args, **kwargs):
        return _call(self, "_findings_from_specialist_evidence", args, kwargs)

    def _load_remembered_insider_findings(self, *args, **kwargs):
        return _call(self, "_load_remembered_insider_findings", args, kwargs)

    def _carry_forward_macro(self, *args, **kwargs):
        return _call(self, "_carry_forward_macro", args, kwargs)

    def _latest_news_read_today(self, *args, **kwargs):
        return _call(self, "_latest_news_read_today", args, kwargs)

    def _carry_forward_news(self, *args, **kwargs):
        return _call(self, "_carry_forward_news", args, kwargs)

    def _carry_forward_earnings(self, *args, **kwargs):
        return _call(self, "_carry_forward_earnings", args, kwargs)

    def _specialist_insider_as_of(self, *args, **kwargs):
        return _call(self, "_specialist_insider_as_of", args, kwargs)

    def _insider_same_session(self, *args, **kwargs):
        return _call(self, "_insider_same_session", args, kwargs)

    def _carry_forward_insider(self, *args, **kwargs):
        return _call(self, "_carry_forward_insider", args, kwargs)

    def _record_heal(self, *args, **kwargs):
        return _call(self, "_record_heal", args, kwargs)

    def _persist_heal_call(self, *args, **kwargs):
        return _call(self, "_persist_heal_call", args, kwargs)

    def _persist_healed_macro_store(self, *args, **kwargs):
        return _call(self, "_persist_healed_macro_store", args, kwargs)

    def _cover_healed_news_wire(self, *args, **kwargs):
        return _call(self, "_cover_healed_news_wire", args, kwargs)

    def _try_one_paid_research_retry(self, *args, **kwargs):
        return _call(self, "_try_one_paid_research_retry", args, kwargs)

    def _heal_lost_research_seats(self, *args, **kwargs):
        return _call(self, "_heal_lost_research_seats", args, kwargs)
