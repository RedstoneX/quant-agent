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
    obj._form4_freshness = TradingPipeline._form4_freshness.__get__(obj)
    obj._insider_same_session = TradingPipeline._insider_same_session.__get__(obj)
    obj._specialist_insider_as_of = (
        TradingPipeline._specialist_insider_as_of.__get__(obj)
    )
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
            items = []
            for item in self.titles:
                if isinstance(item, SimpleNamespace):
                    items.append(item)
                else:
                    items.append(SimpleNamespace(title=item, summary=""))
            return items, None

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

    obj.news_provider = _Provider([
        SimpleNamespace(title="Guidance cut after close", summary="AAPL cuts FY outlook"),
    ])
    from_summary = obj._carry_forward_news()
    assert from_summary.status == "expired"


def _freshness(new_filings, *, ok=True, reason="stub"):
    """A provider freshness verdict: "what was FILED since our last read"."""
    return {
        "ok": ok, "new_filings": sorted(new_filings), "read_through": "2026-09-18",
        "checked": 1, "unchecked": [], "reason": reason,
    }


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
            form4_freshness=lambda _s=None: _freshness([]),
        ),
        db=None,
    )
    _bind_reuse(obj)
    kept = obj._carry_forward_insider(ctx)
    assert kept.status in (STATUS_CARRIED_FROM_MORNING, "chose_not_to_refetch") or kept.payload

    obj.smart_money_provider.form4_freshness = lambda _s=None: _freshness(
        ["0001-26-000002"],
    )
    expired = obj._carry_forward_insider(ctx)
    assert expired.status == "expired"


def test_insider_seat_expires_when_freshness_cannot_be_established():
    """A probe that did not answer must NOT read as "nothing new".

    The old peek swallowed its own failure and returned the known set, which
    the caller read as no new filing and REUSED. That let a broken network
    put a decision on research nobody checked was current, while a working
    network that found unread backlog refused the decision — backwards in
    both directions.
    """
    class _Form4:
        def known_accessions(self):
            return {"0001-26-000001"}

    ctx = SimpleNamespace(smart_money_findings=[
        {"symbol": "FTK", "observations": [{"accession_number": "0001-26-000001"}]},
    ])
    for verdict in (
        _freshness([], ok=False, reason="probe failed: ConnectionError"),
        {"ok": False, "new_filings": [], "read_through": "",
         "checked": 0, "unchecked": ["1045810"], "reason": "1 name unchecked"},
    ):
        obj = SimpleNamespace(
            macro_store=_MacroStore(None),
            news_store=_NewsStore(),
            smart_money_provider=SimpleNamespace(
                providers=[_Form4()],
                form4_freshness=lambda _s=None, _v=verdict: _v,
            ),
            db=None,
        )
        _bind_reuse(obj)
        assert obj._carry_forward_insider(ctx).status == "expired"

    # A provider that raises outright falls the same way.
    def _boom(_s=None):
        raise RuntimeError("EDGAR unreachable")

    obj = SimpleNamespace(
        macro_store=_MacroStore(None),
        news_store=_NewsStore(),
        smart_money_provider=SimpleNamespace(
            providers=[_Form4()], form4_freshness=_boom,
        ),
        db=None,
    )
    _bind_reuse(obj)
    assert obj._carry_forward_insider(ctx).status == "expired"


def test_intraday_freshness_does_not_expire_on_backlog_alone():
    """THE ACCEPTANCE CONDITION for 2026-09-18's six lost windows.

    An accession the desk has never read, but which was filed on or before
    the watermark, is BACKLOG — a hole in the producing step, not new
    information. It must not expire the seat, and answering the question
    must not require a full-text crawl.
    """
    class _Form4:
        """Answers only from its own filing history — no EFTS crawl."""

        def __init__(self):
            self.crawled = False

        def known_accessions(self):
            # 000002 exists and was never read: the unread backlog.
            return {"0001-26-000001"}

        def peek_accessions(self, symbols=None):
            self.crawled = True
            return {"0001-26-000001", "0001-26-000002"}

        def form4_freshness(self, symbols=None):
            # Filed 2026-09-15, watermark 2026-09-18 => not "since".
            return _freshness([], reason="nothing filed since 2026-09-18")

    provider = _Form4()
    ctx = SimpleNamespace(smart_money_findings=[
        {"symbol": "FTK", "observations": [{"accession_number": "0001-26-000001"}]},
    ])
    obj = SimpleNamespace(
        macro_store=_MacroStore(None),
        news_store=_NewsStore(),
        smart_money_provider=provider,
        db=None,
    )
    _bind_reuse(obj)
    carried = obj._carry_forward_insider(ctx)
    assert carried.status != "expired"
    assert provider.crawled is False, "the decision tick must not run a crawl"


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
            form4_freshness=lambda _s=None: _freshness([]),
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
            form4_freshness=lambda symbols=None: _freshness([]),
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


def test_specialist_evidence_timestamp_is_the_insider_same_session_date():
    """Production findings have no as_of; the producing row's timestamp is the date."""
    class _Form4:
        def known_accessions(self):
            return {"0001-26-000001"}

        def peek_accessions(self, symbols=None):
            return {"0001-26-000001"}

    class _DB:
        def execute(self, sql, params=None):
            class _Row(dict):
                pass
            if "timestamp" in sql:
                row = _Row(timestamp=f"{et_today()} 14:05:00")
                return SimpleNamespace(fetchone=lambda: row)
            return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])

    obj = SimpleNamespace(
        macro_store=_MacroStore(None),
        news_store=_NewsStore(),
        smart_money_provider=SimpleNamespace(
            providers=[_Form4()],
            form4_freshness=lambda symbols=None: _freshness([]),
        ),
        db=_DB(),
    )
    _bind_reuse(obj)
    carried = obj._carry_forward_insider(SimpleNamespace(smart_money_findings=[
        {"symbol": "FTK", "observations": [{"accession_number": "0001-26-000001"}]},
    ]))
    assert carried.same_session is True
    assert carried.status == STATUS_CARRIED_FROM_MORNING
