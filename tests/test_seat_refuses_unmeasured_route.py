"""Board item 188 — a decision seat refuses rather than answer on a model
nobody has measured at that seat.

The owner's model choice for the trade seat is CLOSED (148 trials). A route
that quietly substitutes an unmeasured model therefore overrides a ruling
that has already been made, and its output looks identical to a good one.
The fix is refusal, not a benchmark and not a different model.
"""
import sqlite3

import pytest

from src import llm_route_journal
from src.agents.llm_tertiary_route import (
    DECISION_SEATS,
    seat_must_refuse_unmeasured_route,
    select_tertiary_route,
)


def test_decision_seat_refuses_only_when_route_3_is_the_substitute():
    for seat in DECISION_SEATS:
        assert seat_must_refuse_unmeasured_route(seat, True) is True
        # The configured, reasoned route 3 (haiku) is NOT refused: routes 1
        # and 2 having failed is an outage, not a silent model substitution.
        assert seat_must_refuse_unmeasured_route(seat, False) is False


def test_specialist_seats_never_refuse():
    # They describe, they do not decide, and their route 3 was never swapped.
    for seat in ("tech_analyst", "news_analyst", "macro_analyst",
                 "evening_analyst", ""):
        assert seat_must_refuse_unmeasured_route(seat, True) is False
        assert seat_must_refuse_unmeasured_route(seat, False) is False


def test_the_refused_route_is_exactly_the_one_the_selector_substitutes():
    """Guards against the rule drifting away from what it is about: the
    substitution only happens when the whole ladder is on one road."""
    alt = ("google", "gemini-3.5-flash-lite")
    tertiary = ("openrouter", "anthropic/claude-haiku-4.5")
    collapsed = select_tertiary_route(
        primary=("openrouter", "openai/gpt-5.5"),
        fallback=("openrouter", "google/gemini-3.5-flash-lite"),
        tertiary=tertiary, alt=alt,
    )
    assert collapsed == alt          # decision seat: substituted
    two_roads = select_tertiary_route(
        primary=("google", "gemini-3.5-flash-lite"),
        fallback=("openrouter", "google/gemini-3.5-flash-lite"),
        tertiary=tertiary, alt=alt,
    )
    assert two_roads == tertiary     # specialist seat: untouched


def test_refusal_is_a_durable_counted_row_asking_what_did_not_succeed(
        tmp_path, monkeypatch):
    """The dashboard trap: a query that enumerates known failure reasons
    misses the new one. The refusal must be findable by asking for every
    event that is NOT a route carrying the call."""
    db = tmp_path / "journal.db"
    monkeypatch.setenv("QUANT_AGENT_DB_PATH", str(db))
    assert "seat_refused" in llm_route_journal.EVENT_TYPES
    assert llm_route_journal.record(
        "seat_refused", agent_name="portfolio_manager", run_id="r1",
        route="google/gemini-3.5-flash-lite", tier=3,
        detail="decision seat refused the last rung",
    ) is True
    conn = sqlite3.connect(str(db))
    try:
        carried = ("route_switch", "route_restored")
        rows = conn.execute(
            "SELECT event_type, agent_name, COUNT(*) FROM llm_route_events "
            "WHERE event_type NOT IN (?,?) GROUP BY event_type, agent_name",
            carried,
        ).fetchall()
    finally:
        conn.close()
    assert ("seat_refused", "portfolio_manager", 1) in rows


def test_a_broken_journal_cannot_abort_the_decision_stage(monkeypatch):
    """Recording must never kill a money path — a recorder that raised into
    the decision path bit this project once already."""
    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(llm_route_journal, "_connect", _boom)
    before = llm_route_journal.write_failures()
    assert llm_route_journal.record(
        "seat_refused", agent_name="risk_manager") is False
    assert llm_route_journal.write_failures() == before + 1
