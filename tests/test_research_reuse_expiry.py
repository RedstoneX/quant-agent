"""Research-reuse expiry must actually run.

The getattr peeks were never defined, so news/Form 4 expiry never fired.
Macro expiry compared the regime label only. An undated snapshot was
treated as same-session. These pins keep that from regressing.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from src.evidence_kind import (
    STATUS_CARRIED_FROM_MORNING,
    STATUS_REMEMBERED,
    same_session_from_date,
)
from src.pipeline import TradingPipeline
from src.trading_calendar import et_today


MACRO = {
    "reasoning_chain": {
        "volatility_analysis": "vix ok",
        "yield_curve_analysis": "curve ok",
        "monetary_policy_analysis": "fed ok",
        "inflation_labor_credit": "cpi ok",
        "cross_signal_synthesis": "together ok",
        "sector_implications": "tech ow",
    },
    "regime": "risk-on",
    "confidence": "medium",
    "equity_outlook": "bullish",
    "position_guidance": {
        "target_invested_pct": 70,
        "cash_recommendation_pct": 30,
        "reasoning": "stay invested",
    },
    "summary": "risk on",
    "sector_guidance": [{"sector": "Technology", "stance": "overweight", "reason": ""}],
}


class _MacroStore:
    def __init__(self, state, history=None):
        self._state = state
        self._history = history if history is not None else ([state] if state else [])

    def load_last_state(self):
        return self._state

    def load_history(self, days=2):
        return list(self._history or [])[-days:]


class _NewsStore:
    def __init__(self, report=None, raw_headlines=None):
        self._report = report
        self._raw = raw_headlines or []

    def load_daily_report(self, session=None):
        return self._report

    def load_raw_headlines(self, session_date=None):
        return list(self._raw)


def _bind_reuse(obj):
    obj._carry_forward_macro = TradingPipeline._carry_forward_macro.__get__(obj)
    obj._carry_forward_news = TradingPipeline._carry_forward_news.__get__(obj)
    obj._carry_forward_insider = TradingPipeline._carry_forward_insider.__get__(obj)
    obj._macro_regime_or_print_changed = (
        TradingPipeline._macro_regime_or_print_changed.__get__(obj)
    )
    obj._macro_history_regime_changed = (
        TradingPipeline._macro_history_regime_changed.__get__(obj)
    )
    obj._macro_series_prints_changed = (
        TradingPipeline._macro_series_prints_changed.__get__(obj)
    )
    obj._live_macro_series_prints = (
        TradingPipeline._live_macro_series_prints.__get__(obj)
    )
    obj._news_has_newer_material_wire = (
        TradingPipeline._news_has_newer_material_wire.__get__(obj)
    )
    obj._watched_research_symbols = (
        TradingPipeline._watched_research_symbols.__get__(obj)
    )
    obj._peek_news_headlines = TradingPipeline._peek_news_headlines.__get__(obj)
    obj._peek_new_form4_accessions = (
        TradingPipeline._peek_new_form4_accessions.__get__(obj)
    )
    obj._insider_same_session = TradingPipeline._insider_same_session.__get__(obj)
    obj._load_remembered_insider_findings = (
        TradingPipeline._load_remembered_insider_findings.__get__(obj)
    )
    obj._form4_known_accessions = TradingPipeline._form4_known_accessions.__get__(obj)
    obj._findings_from_specialist_evidence = (
        TradingPipeline._findings_from_specialist_evidence.__get__(obj)
    )
    return obj


def test_research_reuse_peeks_exist_on_the_pipeline():
    """The getattr call sites used to resolve to None. They must be real."""
    assert callable(getattr(TradingPipeline, "_peek_news_headlines", None))
    assert callable(getattr(TradingPipeline, "_peek_new_form4_accessions", None))
    assert callable(getattr(TradingPipeline, "_load_remembered_insider_findings", None))


def test_same_session_requires_a_trustworthy_date():
    today = str(et_today())
    assert same_session_from_date(today) is True
    assert same_session_from_date("") is False
    assert same_session_from_date(None) is False
    assert same_session_from_date("not-a-date") is False
    assert same_session_from_date(str(et_today() - timedelta(days=1))) is False


def test_dated_same_session_macro_still_reuses():
    state = dict(MACRO, date=str(et_today()), series_prints={
        "values": {"inflation.core_cpi_yoy": 3.1},
        "observations": {"CPILFESL": "2026-08-01"},
    })
    obj = SimpleNamespace(
        macro_store=_MacroStore(state),
        news_store=_NewsStore(),
        macro=SimpleNamespace(get_macro_summary=lambda: {
            "inflation": {"core_cpi_yoy": 3.1},
        }, _run_freshness={
            "CPILFESL": SimpleNamespace(latest_observation=__import__("datetime").date(2026, 8, 1)),
        }),
    )
    _bind_reuse(obj)
    carried = obj._carry_forward_macro()
    assert carried.status == STATUS_CARRIED_FROM_MORNING
    assert carried.same_session is True
    assert carried.payload["regime"] == "risk-on"


def test_undated_macro_snapshot_is_rejected_as_same_session():
    obj = SimpleNamespace(macro_store=_MacroStore(dict(MACRO)), news_store=_NewsStore())
    _bind_reuse(obj)
    carried = obj._carry_forward_macro()
    assert carried.payload is not None
    assert carried.same_session is False
    assert carried.status == STATUS_REMEMBERED


def test_macro_expiry_fires_on_print_change_with_same_regime_label():
    """CPI printed a new number; the analyst still called risk-on. Expire."""
    stored_prints = {
        "values": {"inflation.core_cpi_yoy": 3.1},
        "observations": {"CPILFESL": "2026-08-01"},
    }
    live_prints_summary = {
        "inflation": {"core_cpi_yoy": 3.4},
        "vix": {"current": 22.0},
    }
    state = dict(MACRO, date=str(et_today()), series_prints=stored_prints)
    obj = SimpleNamespace(
        macro_store=_MacroStore(state, history=[state]),
        news_store=_NewsStore(),
        macro=SimpleNamespace(
            get_macro_summary=lambda: live_prints_summary,
            _run_freshness={
                "CPILFESL": SimpleNamespace(latest_observation=__import__("datetime").date(2026, 9, 1)),
            },
        ),
    )
    _bind_reuse(obj)
    carried = obj._carry_forward_macro()
    assert carried.status == "expired"
    assert carried.payload is None


def test_unchanged_prints_do_not_invent_churn():
    stored_prints = {
        "values": {"inflation.core_cpi_yoy": 3.1},
        "observations": {"CPILFESL": "2026-08-01"},
    }
    state = dict(MACRO, date=str(et_today()), series_prints=stored_prints)
    obj = SimpleNamespace(
        macro_store=_MacroStore(state, history=[state]),
        news_store=_NewsStore(),
        macro=SimpleNamespace(
            get_macro_summary=lambda: {"inflation": {"core_cpi_yoy": 3.1}},
            _run_freshness={
                "CPILFESL": SimpleNamespace(latest_observation=__import__("datetime").date(2026, 8, 1)),
            },
        ),
    )
    _bind_reuse(obj)
    carried = obj._carry_forward_macro()
    assert carried.status == STATUS_CARRIED_FROM_MORNING
    assert carried.payload is not None


def test_failed_print_fetch_is_not_a_change():
    state = dict(MACRO, date=str(et_today()), series_prints={
        "values": {"inflation.core_cpi_yoy": 3.1},
        "observations": {},
    })

    def _boom():
        raise RuntimeError("FRED down")

    obj = SimpleNamespace(
        macro_store=_MacroStore(state),
        news_store=_NewsStore(),
        macro=SimpleNamespace(get_macro_summary=_boom, _run_freshness={}),
    )
    _bind_reuse(obj)
    carried = obj._carry_forward_macro()
    assert carried.status == STATUS_CARRIED_FROM_MORNING


def test_news_peek_expires_on_a_new_headline_and_reuses_when_unchanged():
    from src.models import MacroNarrative, NewsIntelligenceReport

    stored = NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-09-17", era_themes=["AI capex"],
            current_regime="risk-on",
        ),
        state_changes=[],
        stock_news={"AAPL": [{
            "headline": "Apple beats",
            "sentiment": "bullish",
            "conviction": "high",
            "impact_summary": "beat",
        }]},
        pm_briefing="ok", market_sentiment="bullish", confidence="medium",
    ).model_dump()

    class _Provider:
        def __init__(self, titles):
            self.titles = titles

        def fetch_news(self, symbols=None):
            return [SimpleNamespace(title=t) for t in self.titles], None

    obj = SimpleNamespace(
        macro_store=_MacroStore(None),
        news_store=_NewsStore(stored, raw_headlines=[{"title": "Apple beats"}]),
        news_provider=_Provider(["Apple beats"]),
    )
    _bind_reuse(obj)
    kept = obj._carry_forward_news()
    assert kept.status == STATUS_CARRIED_FROM_MORNING
    assert kept.payload is not None

    obj.news_provider = _Provider(["Apple beats", "AAPL guidance cut after close"])
    expired = obj._carry_forward_news()
    assert expired.status == "expired"
    assert expired.payload is None

    obj.news_provider = _Provider(["Apple beats", "Fed holds rates after the close"])
    ignored = obj._carry_forward_news()
    assert ignored.status == STATUS_CARRIED_FROM_MORNING
    assert ignored.payload is not None


def test_insider_peek_expires_on_a_new_form4_accession():
    class _Form4:
        def __init__(self, known, peek):
            self._known = set(known)
            self._peek = set(peek)

        def known_accessions(self):
            return set(self._known)

        def peek_accessions(self):
            return set(self._peek)

    ctx = SimpleNamespace(smart_money_findings=[
        {"symbol": "FTK", "observations": [{"accession_number": "0001-26-000001"}]},
    ])
    obj = SimpleNamespace(
        macro_store=_MacroStore(None),
        news_store=_NewsStore(),
        smart_money_provider=SimpleNamespace(
            providers=[_Form4(["0001-26-000001"], ["0001-26-000001"])],
            peek_form4_accessions=lambda: {"0001-26-000001"},
        ),
        db=None,
    )
    _bind_reuse(obj)
    kept = obj._carry_forward_insider(ctx)
    assert kept.status in (STATUS_CARRIED_FROM_MORNING, "chose_not_to_refetch") or kept.payload

    obj.smart_money_provider.peek_form4_accessions = lambda: {
        "0001-26-000001", "0001-26-000002",
    }
    expired = obj._carry_forward_insider(ctx)
    assert expired.status == "expired"


def test_insider_loader_uses_processed_accessions_not_just_findings():
    """A filing already in the cache is not 'new' just because it was not material."""
    class _Form4:
        def known_accessions(self):
            return {"0001-26-000001", "0001-26-000099"}

        def peek_accessions(self):
            return {"0001-26-000001", "0001-26-000099"}

    ctx = SimpleNamespace(smart_money_findings=[
        {"symbol": "FTK", "observations": [{"accession_number": "0001-26-000001"}]},
    ])
    obj = SimpleNamespace(
        macro_store=_MacroStore(None),
        news_store=_NewsStore(),
        smart_money_provider=SimpleNamespace(
            providers=[_Form4()],
            peek_form4_accessions=lambda: {"0001-26-000001", "0001-26-000099"},
        ),
        db=None,
    )
    _bind_reuse(obj)
    findings, accessions = obj._load_remembered_insider_findings(ctx)
    assert "0001-26-000099" in accessions
    kept = obj._carry_forward_insider(ctx)
    assert kept.status != "expired"
    assert findings
    assert kept.same_session is False


def test_daily_quote_move_is_not_a_macro_print_change():
    """VIX reprints every session. That is not a CPI/UNRATE/claims print."""
    stored_prints = {
        "values": {"inflation.core_cpi_yoy": 3.1},
        "observations": {"CPILFESL": "2026-08-01"},
    }
    state = dict(MACRO, date=str(et_today()), series_prints=stored_prints)
    obj = SimpleNamespace(
        macro_store=_MacroStore(state, history=[state]),
        news_store=_NewsStore(),
        macro=SimpleNamespace(
            get_macro_summary=lambda: {
                "inflation": {"core_cpi_yoy": 3.1},
                "vix": {"current": 28.0},
            },
            _run_freshness={
                "CPILFESL": SimpleNamespace(latest_observation=__import__("datetime").date(2026, 8, 1)),
            },
            last_coverage="morning-coverage",
        ),
    )
    _bind_reuse(obj)
    carried = obj._carry_forward_macro()
    assert carried.status == STATUS_CARRIED_FROM_MORNING
    assert obj.macro.last_coverage == "morning-coverage"


def test_missing_stored_fingerprint_is_not_a_print_change():
    """An old last_state that never recorded prints must not invent expiry."""
    state = dict(MACRO, date=str(et_today()))
    obj = SimpleNamespace(
        macro_store=_MacroStore(state, history=[state]),
        news_store=_NewsStore(),
        macro=SimpleNamespace(
            get_macro_summary=lambda: {"inflation": {"core_cpi_yoy": 3.4}},
            _run_freshness={},
        ),
    )
    _bind_reuse(obj)
    carried = obj._carry_forward_macro()
    assert carried.status == STATUS_CARRIED_FROM_MORNING
    assert carried.payload is not None


def test_dated_insider_finding_is_same_session_and_undated_is_not():
    class _Form4:
        def known_accessions(self):
            return {"0001-26-000001"}

        def peek_accessions(self, symbols=None):
            return {"0001-26-000001"}

    obj = SimpleNamespace(
        macro_store=_MacroStore(None),
        news_store=_NewsStore(),
        smart_money_provider=SimpleNamespace(
            providers=[_Form4()],
            peek_form4_accessions=lambda symbols=None: {"0001-26-000001"},
        ),
        db=None,
    )
    _bind_reuse(obj)
    undated = obj._carry_forward_insider(SimpleNamespace(smart_money_findings=[
        {"symbol": "FTK", "observations": [{"accession_number": "0001-26-000001"}]},
    ]))
    assert undated.same_session is False
    assert undated.payload
    dated = obj._carry_forward_insider(SimpleNamespace(smart_money_findings=[
        {
            "symbol": "FTK",
            "as_of": str(et_today()),
            "observations": [{"accession_number": "0001-26-000001"}],
        },
    ]))
    assert dated.same_session is True
    assert dated.status == STATUS_CARRIED_FROM_MORNING
