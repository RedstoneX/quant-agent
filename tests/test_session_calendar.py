"""The session calendar must reproduce today's day exactly, bend on a short
session, say so on a holiday, and refuse loudly when it cannot read."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.session_calendar import (
    PHASE_OFFSETS,
    Session,
    SessionCalendarUnavailable,
    covered_phases,
    et_datetime,
    phase_window,
    session_for,
)
from src.trading_calendar import ET, SESSION_WINDOWS

NORMAL_DAY = date(2026, 10, 5)  # an ordinary Monday
EARLY_CLOSE_DAY = date(2026, 11, 27)  # the day after US Thanksgiving
HOLIDAY = date(2026, 11, 26)


class FakeCalendarBroker:
    """Stands in for the broker's calendar reads, with the broker's own
    failure posture: unknown dates come back as None, exactly as a holiday
    does, which is the ambiguity the module has to resolve."""

    def __init__(self, sessions: dict[date, tuple[int, int]]):
        self._sessions = sessions
        self.calls: list[date] = []

    def _edge(self, on_date: date, which: int):
        self.calls.append(on_date)
        window = self._sessions.get(on_date)
        if window is None:
            return None
        return et_datetime(on_date, window[which])

    def get_session_open(self, on_date=None):
        return self._edge(on_date, 0)

    def get_session_close(self, on_date=None):
        return self._edge(on_date, 1)


def _normal_sessions(around: date, days: int = 40) -> dict[date, tuple[int, int]]:
    """Every weekday in a window around `around` is a plain 09:30-16:00 day."""
    out: dict[date, tuple[int, int]] = {}
    for delta in range(-days, days + 1):
        day = around + timedelta(days=delta)
        if day.weekday() < 5:
            out[day] = (570, 960)
    return out


def test_offsets_reproduce_todays_exact_minutes():
    """The whole re-expression is only safe if it is a no-op on a normal day."""
    assert covered_phases() == frozenset(SESSION_WINDOWS)
    broker = FakeCalendarBroker(_normal_sessions(NORMAL_DAY))
    for phase, (start_min, end_min) in SESSION_WINDOWS.items():
        start, end = phase_window(broker, NORMAL_DAY, phase)
        assert (start, end) == (et_datetime(NORMAL_DAY, start_min), et_datetime(NORMAL_DAY, end_min)), phase


def test_normal_day_resolves_to_the_same_wall_clock():
    broker = FakeCalendarBroker(_normal_sessions(NORMAL_DAY))
    for phase, (start_min, end_min) in SESSION_WINDOWS.items():
        start, end = phase_window(broker, NORMAL_DAY, phase)
        assert start == et_datetime(NORMAL_DAY, start_min), phase
        assert end == et_datetime(NORMAL_DAY, end_min), phase


def test_early_close_pulls_the_end_of_day_review_before_the_close():
    """The defect in one assertion: on a 13:00 close the end-of-day review
    must finish before the bell, not an hour and a half after it."""
    sessions = _normal_sessions(EARLY_CLOSE_DAY)
    sessions[EARLY_CLOSE_DAY] = (570, 780)  # 09:30 - 13:00 ET
    broker = FakeCalendarBroker(sessions)

    close_at = et_datetime(EARLY_CLOSE_DAY, 780)
    for phase in ("midday", "close", "intra_check"):
        start, end = phase_window(broker, EARLY_CLOSE_DAY, phase)
        assert end <= close_at, f"{phase} runs past the real close"
        assert start < close_at, f"{phase} starts after the market has shut"

    # And specifically: the act-on-trigger close review lands in the last half
    # hour of the SHORT session, 12:30-13:00, not at the hardcoded 15:30.
    assert phase_window(broker, EARLY_CLOSE_DAY, "close") == (
        et_datetime(EARLY_CLOSE_DAY, 750),
        close_at,
    )
    # Under today's fixed table this phase would have been 15:30-16:00, i.e.
    # entirely after the close — the regression this test pins down.
    assert SESSION_WINDOWS["close"][0] > 780


def test_holiday_is_not_a_trading_day():
    sessions = _normal_sessions(HOLIDAY)
    del sessions[HOLIDAY]
    broker = FakeCalendarBroker(sessions)

    session = session_for(broker, HOLIDAY)
    assert session.is_trading_day is False
    assert session.open_at is None and session.close_at is None
    with pytest.raises(SessionCalendarUnavailable):
        phase_window(broker, HOLIDAY, "close")


def test_weekend_is_not_a_trading_day():
    saturday = date(2026, 10, 10)
    assert saturday.weekday() == 5
    broker = FakeCalendarBroker(_normal_sessions(saturday))
    assert session_for(broker, saturday).is_trading_day is False


def test_unreadable_calendar_raises_instead_of_assuming_a_normal_session():
    """A broker whose calendar answers nothing anywhere is an outage, and an
    outage must never be served as a holiday or as 09:30-16:00."""
    broker = FakeCalendarBroker({})
    with pytest.raises(SessionCalendarUnavailable) as exc:
        session_for(broker, NORMAL_DAY)
    assert "unreadable" in str(exc.value)
    with pytest.raises(SessionCalendarUnavailable):
        phase_window(broker, NORMAL_DAY, "close")


def test_half_an_answer_is_refused():
    class OneEdge(FakeCalendarBroker):
        def get_session_close(self, on_date=None):
            return None

    broker = OneEdge(_normal_sessions(NORMAL_DAY))
    with pytest.raises(SessionCalendarUnavailable) as exc:
        session_for(broker, NORMAL_DAY)
    assert "only one edge" in str(exc.value)


def test_missing_broker_raises_rather_than_defaulting():
    with pytest.raises(SessionCalendarUnavailable):
        session_for(None, NORMAL_DAY)


def test_no_date_is_hardcoded_in_the_module():
    """A holiday table written into the source is the same bug in a new
    costume, so the module must contain no literal calendar date."""
    import re
    from pathlib import Path

    import src.session_calendar as module

    source = Path(module.__file__).read_text()
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    code = re.sub(r'"""(?:.|\n)*?"""', "", code)
    assert not re.search(r"\b(19|20)\d{2}\s*,\s*\d{1,2}\s*,\s*\d{1,2}\b", code)
    assert not re.search(r"\bdate\(\s*\d", code)


def test_phase_offsets_are_all_anchored_to_a_real_edge():
    for phase, offset in PHASE_OFFSETS.items():
        assert offset.start_anchor in ("open", "close"), phase
        assert offset.end_anchor in ("open", "close"), phase
        start, end = phase_window(FakeCalendarBroker(_normal_sessions(NORMAL_DAY)), NORMAL_DAY, phase)
        assert start < end, phase


def test_session_require_edges_refuses_a_closed_day():
    closed = Session(on_date=HOLIDAY, is_trading_day=False, open_at=None, close_at=None)
    with pytest.raises(SessionCalendarUnavailable):
        closed.require_edges()


def test_resolved_edges_are_eastern_aware():
    broker = FakeCalendarBroker(_normal_sessions(NORMAL_DAY))
    opens, closes = session_for(broker, NORMAL_DAY).require_edges()
    for moment in (opens, closes):
        assert isinstance(moment, datetime)
        assert moment.tzinfo is ET
