"""Board item 185's closing condition: the screen must RECORD its cross-section.

The volatility ceiling the screen applies (1 / the widest reachable stop ATR
multiple, 33.3% at today's settings) is an arithmetic non-degeneracy floor -
the ATR/price at which the widest legitimate stop sits at or below zero - and
NOT a statement of how volatile a name this desk will hold. The published form
of an eligibility bound is cross-sectional, and this desk has never had a
cross-section: the screen has never executed, so every ATR reading measured so
far came from names it had already admitted.

These tests pin the recording that lifts that blocker. They assert the shape of
the stored distribution, never a threshold, because the whole point is that the
threshold is not known yet.
"""

from __future__ import annotations

from datetime import date

from src.universe_screen import empty_state, record_atr_cross_section


def test_empty_state_carries_the_cross_section() -> None:
    assert empty_state()["atr_cross_section"] == []


def test_a_reading_is_stored_with_its_name_week_and_date() -> None:
    state = empty_state()
    record_atr_cross_section(
        state,
        week="2026-W40",
        today=date(2026, 10, 1),
        symbol="AAA",
        measured={"atr_pct": 3.21},
    )
    runs = state["atr_cross_section"]
    assert len(runs) == 1
    assert runs[0]["date"] == "2026-10-01"
    assert runs[0]["week"] == "2026-W40"
    assert runs[0]["readings"] == {"AAA": 3.21}


def test_one_run_collects_the_whole_cross_section_not_one_row_each() -> None:
    """A quantile needs the run's whole population in one place."""
    state = empty_state()
    for symbol, pct in (("AAA", 1.0), ("BBB", 2.0), ("CCC", 3.0)):
        record_atr_cross_section(
            state,
            week="2026-W40",
            today=date(2026, 10, 1),
            symbol=symbol,
            measured={"atr_pct": pct},
        )
    assert len(state["atr_cross_section"]) == 1
    assert state["atr_cross_section"][0]["readings"] == {
        "AAA": 1.0,
        "BBB": 2.0,
        "CCC": 3.0,
    }


def test_a_later_run_is_a_separate_population() -> None:
    state = empty_state()
    record_atr_cross_section(state, week="2026-W40", today=date(2026, 10, 1), symbol="AAA", measured={"atr_pct": 1.0})
    record_atr_cross_section(state, week="2026-W41", today=date(2026, 10, 8), symbol="AAA", measured={"atr_pct": 4.0})
    assert [r["date"] for r in state["atr_cross_section"]] == [
        "2026-10-01",
        "2026-10-08",
    ]


def test_a_symbol_with_no_measurable_atr_records_nothing() -> None:
    """An absent reading must not enter the distribution as a zero."""
    state = empty_state()
    for measured in ({}, {"last_price": 10.0}, None):
        record_atr_cross_section(state, week="2026-W40", today=date(2026, 10, 1), symbol="AAA", measured=measured)
    assert state["atr_cross_section"] == []


def test_the_recording_survives_a_save_and_load_round_trip(tmp_path) -> None:
    from src.universe_screen import UniverseStore

    state = empty_state()
    record_atr_cross_section(state, week="2026-W40", today=date(2026, 10, 1), symbol="AAA", measured={"atr_pct": 2.5})
    store = UniverseStore(tmp_path)
    store.save(state)
    assert store.load()["atr_cross_section"] == state["atr_cross_section"]
