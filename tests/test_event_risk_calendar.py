"""Event risk is answered from fetched data, not from the model's memory.

Two recorded defects are pinned here.

(c) `MarketDataProvider.get_next_earnings_date` existed specifically to ground
    `RiskVerdict.reasoning_chain.event_risk` — a REQUIRED narrative field — and
    had ZERO callers anywhere in src/ or tests/. The tests below prove the
    fetched figure now reaches the Risk Manager's actual input text, not merely
    that the function got called.

(d) No macro event calendar existed at all, so "is there an FOMC decision or a
    CPI/NFP release coming?" was answered from recollection.

The degraded paths matter as much as the happy ones: this project's standing
rule is that a labelled absence beats a fabricated figure (`pace_status` /
`unavailable_no_pinned_horizon` is the reference). Every failure mode below is
asserted to REACH the seat as an explicit unknown, never as silence and never
as an empty calendar that reads like a calm one.
"""

import json
import time
from datetime import date, timedelta

import pytest

from src.data.event_calendar import (
    EARNINGS_DEADLINE_EXCEEDED,
    EARNINGS_LOOKUP_FAILED,
    EARNINGS_LOOKUP_TIMEOUT,
    EARNINGS_MEASURED,
    EARNINGS_NO_FETCHED_DATE,
    MACRO_RELEASES,
    UNCOVERED_EVENTS,
    EarningsProximity,
    EventCalendarCoverage,
    MacroEventCalendarProvider,
    fetch_earnings_proximity,
    format_event_risk_block,
    format_macro_events_section,
)
from src.models import PortfolioDecision, Position, ReasoningChain, TradeDecision
from src.trading_calendar import et_today


# --- helpers ---------------------------------------------------------------

def _pm_rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="x", news_check="x", earnings_check="x",
        signal_conflicts="x", sizing_logic="x",
        portfolio_balance="x", cash_target="x",
    )


def _decision(symbols=("NVDA",)) -> PortfolioDecision:
    return PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="BUY", symbol=s, allocation_pct=10.0,
                entry_price=100.0, stop_loss=95.0, take_profit=115.0,
                reasoning="setup",
            )
            for s in symbols
        ],
        portfolio_view="Bullish",
    )


class _FakeMarket:
    """Stands in for MarketDataProvider — only the one method matters."""

    def __init__(self, answers=None, raises=False, hang_s=0.0):
        self.answers = answers or {}
        self.raises = raises
        self.hang_s = hang_s
        self.calls = []

    def get_next_earnings_date(self, symbol):
        self.calls.append(symbol)
        if self.hang_s:
            time.sleep(self.hang_s)
        if self.raises:
            raise RuntimeError("yfinance exploded")
        return self.answers.get(symbol)


def _calendar(payloads, *, deadline_s=20.0, **kwargs):
    """A provider whose transport returns canned payloads.

    `payloads` maps release_id -> payload dict, or a callable raising to
    simulate a transport failure.
    """
    provider = MacroEventCalendarProvider(
        api_key="dummy", total_fetch_deadline_s=deadline_s, **kwargs,
    )
    calls = []

    def _transport(url, timeout):
        calls.append((url, timeout))
        release_id = int(url.split("release_id=")[1].split("&")[0])
        entry = payloads.get(release_id)
        if callable(entry):
            return entry()
        if entry is None:
            raise OSError("connection reset")
        return entry

    provider._http_get_json = _transport
    provider.transport_calls = calls
    return provider


def _dates_payload(dates):
    return {"release_dates": [{"date": d.isoformat()} for d in dates]}


# --- defect (c): the fetched earnings date reaches the Risk Manager ---------

def test_the_fetched_earnings_date_reaches_the_risk_managers_input_text():
    """The load-bearing proof for defect (c).

    Not "the function was called" — that would pass with the number thrown
    away. The exact fetched figure must be visible in the message the model
    actually reads.
    """
    from src.agents.risk_manager import RiskManagerAgent

    market = _FakeMarket({"NVDA": 2, "JPM": 30})
    earnings = fetch_earnings_proximity(
        market, ["NVDA", "JPM"], per_symbol_timeout_s=2.0, total_deadline_s=5.0,
    )
    assert market.calls == ["NVDA", "JPM"]  # the dead function now has callers
    block = format_event_risk_block(
        earnings=earnings,
        events=[],
        coverage=EventCalendarCoverage(configured=7, succeeded=7, failed=[]),
        horizon_days=10,
    )

    agent = RiskManagerAgent.__new__(RiskManagerAgent)
    message = agent.build_user_message(
        portfolio_decision=_decision(("NVDA", "JPM")),
        positions=[],
        macro_summary={},
        rule_violations=[],
        event_risk_block=block,
    )

    assert "NVDA: next earnings ~2 sessions away (fetched)" in message
    assert "INSIDE THE 3-SESSION EVENT WINDOW" in message
    assert "JPM: next earnings ~30 sessions away (fetched)" in message
    assert "do NOT answer this from memory" in message


def test_risk_manager_input_says_unknown_when_the_earnings_lookup_returns_nothing():
    """Degraded path. `get_next_earnings_date` returns None for BOTH "unknown"
    and "nothing scheduled" — its own docstring forbids reading that as "no
    earnings soon". The seat must be told, in words, that the date is unknown.
    """
    from src.agents.risk_manager import RiskManagerAgent

    market = _FakeMarket({"NVDA": None})
    earnings = fetch_earnings_proximity(
        market, ["NVDA"], per_symbol_timeout_s=2.0, total_deadline_s=5.0,
    )
    assert earnings[0].status == EARNINGS_NO_FETCHED_DATE
    assert earnings[0].sessions_away is None

    agent = RiskManagerAgent.__new__(RiskManagerAgent)
    message = agent.build_user_message(
        portfolio_decision=_decision(("NVDA",)),
        positions=[], macro_summary={}, rule_violations=[],
        event_risk_block=format_event_risk_block(
            earnings=earnings, events=[],
            coverage=EventCalendarCoverage(configured=7, succeeded=7, failed=[]),
            horizon_days=10,
        ),
    )
    assert "unavailable_no_fetched_date" in message
    assert "Treat the earnings date as UNKNOWN" in message
    assert "Earnings proximity is UNKNOWN for: NVDA" in message
    # The one reading it must NOT be able to take this as reassurance.
    assert "does NOT mean no report is due" in message


def test_risk_manager_input_says_unknown_when_the_earnings_lookup_raises():
    market = _FakeMarket(raises=True)
    earnings = fetch_earnings_proximity(
        market, ["NVDA"], per_symbol_timeout_s=2.0, total_deadline_s=5.0,
    )
    assert earnings[0].status == EARNINGS_LOOKUP_FAILED
    rendered = format_event_risk_block(
        earnings=earnings, events=[], coverage=None, horizon_days=10,
    )
    assert "LOOKUP FAILED" in rendered
    assert "unavailable_lookup_failed" in rendered


def test_a_hanging_earnings_lookup_is_bounded_and_labelled():
    """A session must never hang on this. yfinance's calendar call has no
    timeout of its own, so the wrapper supplies one."""
    market = _FakeMarket({"NVDA": 4}, hang_s=3.0)
    started = time.monotonic()
    earnings = fetch_earnings_proximity(
        market, ["NVDA"], per_symbol_timeout_s=0.5, total_deadline_s=5.0,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.5
    assert earnings[0].status == EARNINGS_LOOKUP_TIMEOUT
    assert earnings[0].sessions_away is None


def test_symbols_not_reached_inside_the_budget_are_labelled_never_dropped():
    """A symbol silently missing from the list would read to the seat as a
    symbol with nothing to report."""
    market = _FakeMarket({"AAA": 1, "BBB": 2, "CCC": 3}, hang_s=0.4)
    earnings = fetch_earnings_proximity(
        market, ["AAA", "BBB", "CCC"], per_symbol_timeout_s=0.5,
        total_deadline_s=0.6,
    )
    assert [e.symbol for e in earnings] == ["AAA", "BBB", "CCC"]
    assert earnings[-1].status in (
        EARNINGS_DEADLINE_EXCEEDED, EARNINGS_LOOKUP_TIMEOUT,
    )
    rendered = format_event_risk_block(
        earnings=earnings, events=None, coverage=None, horizon_days=10,
    )
    assert "CCC" in rendered


def test_risk_manager_input_never_goes_silent_when_no_block_is_passed():
    """An absent section reads as a calm calendar. The renderer must fire
    anyway and say NOT FETCHED."""
    from src.agents.risk_manager import RiskManagerAgent

    agent = RiskManagerAgent.__new__(RiskManagerAgent)
    message = agent.build_user_message(
        portfolio_decision=_decision(("NVDA",)),
        positions=[], macro_summary={}, rule_violations=[],
    )
    assert "## Event Risk" in message
    assert "NOT FETCHED this run" in message


def test_earnings_proximity_only_reports_a_figure_when_measured():
    """The pace_status contract: a figure exists only under `measured`."""
    assert EarningsProximity("X", 3, EARNINGS_MEASURED).measured is True
    for status in (
        EARNINGS_NO_FETCHED_DATE, EARNINGS_LOOKUP_FAILED,
        EARNINGS_LOOKUP_TIMEOUT, EARNINGS_DEADLINE_EXCEEDED,
    ):
        proximity = EarningsProximity("X", None, status)
        assert proximity.measured is False
        assert status in proximity.describe()


# --- defect (a): a real macro event calendar --------------------------------

def test_calendar_returns_scheduled_releases_inside_the_horizon():
    today = et_today()
    payloads = {
        10: _dates_payload([today + timedelta(days=3), today + timedelta(days=40)]),
        50: _dates_payload([today + timedelta(days=1)]),
    }
    for release in MACRO_RELEASES:
        payloads.setdefault(release.release_id, _dates_payload([]))
    provider = _calendar(payloads)
    events = provider.get_upcoming_events(horizon_days=10)

    labels = [(e.label, e.days_away) for e in events]
    assert ("Employment Situation (NFP)", 1) in labels
    assert ("CPI", 3) in labels
    # The +40d CPI print is outside the horizon and must not be reported.
    assert all(e.days_away <= 10 for e in events)
    # Sorted soonest first, so the seat reads the imminent one first.
    assert events == sorted(events, key=lambda e: (e.event_date, e.label))


def test_calendar_asks_fred_for_dates_with_no_data_because_future_dates_need_it():
    """Live-verified 2026-08-31: without
    `include_release_dates_with_no_data=true` FRED returns count 0 for every
    FUTURE scheduled date, because a date is only "with data" once the data has
    been published. Dropping this parameter would silently empty the calendar.
    """
    payloads = {r.release_id: _dates_payload([]) for r in MACRO_RELEASES}
    provider = _calendar(payloads)
    provider.get_upcoming_events(horizon_days=10)
    assert provider.transport_calls
    for url, _timeout in provider.transport_calls:
        assert "include_release_dates_with_no_data=true" in url
        assert url.startswith("https://api.stlouisfed.org/fred/release/dates?")


def test_a_failed_calendar_is_reported_as_coverage_not_as_an_empty_calendar():
    """The degraded path for defect (a). Every release fetch fails; the seat
    must be told the calendar is UNAVAILABLE, not shown an empty one."""
    provider = _calendar({})  # every release_id -> transport raises
    events = provider.get_upcoming_events(horizon_days=10)
    coverage = provider.last_coverage

    assert events == []
    assert coverage is not None
    assert coverage.status == "failed"
    assert coverage.complete is False
    assert coverage.succeeded == 0
    assert coverage.failed_count == len(MACRO_RELEASES)

    rendered = format_event_risk_block(
        earnings=[EarningsProximity("NVDA", 5, EARNINGS_MEASURED)],
        events=events, coverage=coverage, horizon_days=10,
    )
    assert "the calendar is UNAVAILABLE" in rendered
    assert "means NOT FETCHED, never" in rendered
    assert "does NOT mean an empty calendar" in rendered
    # And it must never say the reassuring thing.
    assert "none lands inside the next" not in rendered


def test_a_partial_calendar_names_the_releases_that_failed():
    today = et_today()
    # Every release answers with a real (far-future) schedule except payrolls,
    # whose transport fails — so `failed` isolates exactly that one.
    payloads = {
        r.release_id: _dates_payload([today + timedelta(days=200)])
        for r in MACRO_RELEASES
    }
    payloads[10] = _dates_payload([today + timedelta(days=2)])
    payloads[50] = None  # transport failure for payrolls only
    provider = _calendar(
        payloads, max_retries=0, retry_backoff_base_s=0.01,
        retry_backoff_max_s=0.01, retry_backoff_jitter_s=0.0,
    )
    events = provider.get_upcoming_events(horizon_days=10)
    coverage = provider.last_coverage

    assert coverage.status == "partial"
    assert [f.label for f in coverage.failed] == ["Employment Situation (NFP)"]
    assert any(e.label == "CPI" for e in events)
    assert "Employment Situation (NFP)" in coverage.describe()
    assert "coverage GAP" in coverage.describe()


def test_a_clean_response_with_no_scheduled_dates_is_an_absence_not_a_success():
    payloads = {r.release_id: _dates_payload([]) for r in MACRO_RELEASES}
    provider = _calendar(payloads)
    provider.get_upcoming_events(horizon_days=10)
    coverage = provider.last_coverage
    assert coverage.status == "failed"
    assert {f.reason for f in coverage.failed} == {"no_scheduled_dates_published"}


def test_an_empty_calendar_reads_as_empty_only_when_coverage_is_ok():
    today = et_today()
    ok = EventCalendarCoverage(configured=7, succeeded=7, failed=[])
    rendered = format_macro_events_section([], ok, 10)
    assert "None. All 7 tracked release schedules fetched successfully" in rendered

    not_fetched = format_macro_events_section([], None, 10)
    assert "NOT FETCHED this run" in not_fetched
    assert "None. All" not in not_fetched
    assert today is not None


def test_the_calendar_respects_its_wall_clock_ceiling():
    """A slow FRED must not stall the session. The deadline is a real ceiling,
    not an upper bound implied by retry arithmetic."""
    def _slow():
        time.sleep(0.6)
        return _dates_payload([])

    payloads = {r.release_id: _slow for r in MACRO_RELEASES}
    provider = _calendar(payloads, deadline_s=1.0, request_timeout_s=1.0)
    started = time.monotonic()
    provider.get_upcoming_events(horizon_days=10)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"calendar overran its ceiling: {elapsed:.1f}s"
    reasons = {f.reason for f in provider.last_coverage.failed}
    assert "fetch_deadline_exceeded" in reasons
    # Skipped releases are REPORTED, never quietly omitted.
    assert (
        provider.last_coverage.succeeded + provider.last_coverage.failed_count
        == len(MACRO_RELEASES)
    )


def test_the_calendar_retries_a_transient_failure_before_degrading():
    today = et_today()
    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("read timed out")
        return _dates_payload([today + timedelta(days=2)])

    payloads = {r.release_id: _dates_payload([]) for r in MACRO_RELEASES}
    payloads[10] = _flaky
    provider = _calendar(
        payloads, max_retries=1, retry_backoff_base_s=0.01,
        retry_backoff_max_s=0.01, retry_backoff_jitter_s=0.0,
    )
    events = provider.get_upcoming_events(horizon_days=10)
    assert attempts["n"] == 2
    assert any(e.label == "CPI" for e in events)


def test_the_calendar_refuses_to_construct_without_a_fred_key():
    with pytest.raises(ValueError, match="FRED_API_KEY"):
        MacroEventCalendarProvider(api_key="  ")


def test_a_malformed_fred_response_degrades_rather_than_raising():
    payloads = {r.release_id: {"unexpected": True} for r in MACRO_RELEASES}
    provider = _calendar(payloads)
    assert provider.get_upcoming_events(horizon_days=10) == []
    assert {f.reason for f in provider.last_coverage.failed} == {"malformed_response"}


# --- the free-source coverage boundary is declared, not hidden --------------

def test_fomc_is_declared_uncovered_rather_than_left_to_memory():
    """FRED's release 101 ("FOMC Press Release") is a DAILY release carrying no
    meeting schedule — live-verified: over 2026-01-01..2026-08-30 it returns all
    240 calendar days. No free FRED endpoint publishes the FOMC calendar, and
    paid sources are permanently refused. The gap is therefore STATED to every
    seat instead of being left as a silence a model would fill from memory."""
    assert 101 not in {r.release_id for r in MACRO_RELEASES}
    assert any("FOMC" in item for item in UNCOVERED_EVENTS)
    for rendered in (
        format_macro_events_section([], None, 10),
        format_event_risk_block(
            earnings=None, events=[],
            coverage=EventCalendarCoverage(configured=7, succeeded=7, failed=[]),
            horizon_days=10,
        ),
    ):
        assert "FOMC" in rendered
        assert "do not supply a date from memory" in rendered


def test_the_tracked_releases_are_the_live_verified_ids():
    """Each id was confirmed against the live FRED API (read-only GET, real
    key) before being wired in — same discipline as the Phase 4.2 series
    additions. A silent id change would fetch someone else's calendar."""
    assert {r.release_id: r.label for r in MACRO_RELEASES} == {
        10: "CPI",
        50: "Employment Situation (NFP)",
        46: "PPI",
        54: "Personal Income and Outlays (PCE)",
        53: "GDP",
        9: "Retail Sales (advance)",
        180: "Initial Jobless Claims",
    }


# --- the macro seat gets the same calendar ----------------------------------

def test_macro_analyst_prompt_carries_the_fetched_calendar():
    from src.agents.macro_analyst import MacroAnalystAgent

    agent = MacroAnalystAgent.__new__(MacroAnalystAgent)
    today = et_today()
    message = agent.build_user_message(
        macro_summary={},
        universe=["SPY"],
        macro_events=[
            type("E", (), {
                "describe": lambda self: f"{today.isoformat()} (TODAY): CPI — x",
            })(),
        ],
        event_coverage=EventCalendarCoverage(configured=7, succeeded=7, failed=[]),
        event_horizon_days=10,
    )
    assert "Scheduled Macro Releases" in message
    assert "CPI" in message
    assert "FOMC" in message


def test_macro_analyst_prompt_says_not_fetched_when_no_calendar_was_passed():
    from src.agents.macro_analyst import MacroAnalystAgent

    agent = MacroAnalystAgent.__new__(MacroAnalystAgent)
    message = agent.build_user_message(macro_summary={}, universe=["SPY"])
    assert "Scheduled Macro Releases" in message
    assert "NOT FETCHED this run" in message


# --- the risk stage assembles it -------------------------------------------

class _StubConfig:
    class event_risk:
        horizon_days = 10
        earnings_deadline_s = 5.0
        earnings_symbol_timeout_s = 2.0


class _StubPipeline:
    def __init__(self, market):
        self.config = _StubConfig()
        self.market = market


def _ctx(decision, events=None, coverage=None):
    from src.pipeline_context import RunContext

    ctx = RunContext(run_id="r1", session="morning")
    ctx.portfolio_decision = decision
    ctx.macro_events = events or []
    ctx.macro_event_coverage = coverage
    return ctx


def test_risk_stage_block_fetches_earnings_for_exactly_the_symbols_under_review():
    from src.pipeline_stages import RiskStage

    market = _FakeMarket({"NVDA": 1, "JPM": None})
    block = RiskStage._build_event_risk_block(
        _StubPipeline(market),
        _ctx(
            _decision(("NVDA", "JPM")),
            events=[],
            coverage=EventCalendarCoverage(configured=7, succeeded=7, failed=[]),
        ),
    )
    assert market.calls == ["NVDA", "JPM"]
    assert "NVDA: next earnings ~1 session away (fetched)" in block
    assert "INSIDE THE 3-SESSION EVENT WINDOW" in block
    assert "JPM" in block and "unavailable_no_fetched_date" in block


def test_risk_stage_block_says_not_fetched_when_research_never_ran():
    """The resume lane: RiskStage runs without MorningResearchStage having
    populated a calendar. The seat is told, not shown an empty calendar."""
    from src.pipeline_stages import RiskStage

    block = RiskStage._build_event_risk_block(
        _StubPipeline(_FakeMarket({"NVDA": 6})),
        _ctx(_decision(("NVDA",))),
    )
    assert "NVDA: next earnings ~6 sessions away (fetched)" in block
    assert "NOT FETCHED this run — the macro event calendar was not consulted" in block


def test_risk_stage_block_survives_a_broken_market_provider():
    from src.pipeline_stages import RiskStage

    class _Broken:
        def get_next_earnings_date(self, symbol):
            raise RuntimeError("provider down")

    block = RiskStage._build_event_risk_block(
        _StubPipeline(_Broken()), _ctx(_decision(("NVDA",))),
    )
    assert "unavailable_lookup_failed" in block


def test_risk_stage_reuses_the_research_stages_calendar_without_refetching():
    """One session, one FRED calendar sweep — RiskStage reads ctx rather than
    building its own provider."""
    from src.pipeline_stages import RiskStage

    today = et_today()
    event = type("E", (), {
        "describe": lambda self: f"{today.isoformat()} (TODAY): CPI — inflation",
    })()
    block = RiskStage._build_event_risk_block(
        _StubPipeline(_FakeMarket({"NVDA": 9})),
        _ctx(
            _decision(("NVDA",)), events=[event],
            coverage=EventCalendarCoverage(configured=7, succeeded=6, failed=[]),
        ),
    )
    assert "CPI — inflation" in block
