"""A carried-over answer is DISCLOSED; a lost answer PAGES.

Until 2026-09-23 one predicate, `evidence_gate.counts_as_degraded`,
answered both questions, and `expired` — the state that means the desk
HOLDS a good answer and knows a newer one exists — therefore raised the
owner a red "ran on incomplete research" push.

MEASURED before the change, from the retained production log and its
rotations: 18 data-quality alerts survive, 15 on 2026-09-21, 6 on
2026-09-22 and 3 on 2026-09-23 by CRITICAL log timestamp; 16 of the 18
name `news=expired` and the other two are `macro=partial` and
`macro=release_overdue`. Every `expired` one was an `intra_check`, and
each of those ticks had already told the owner, in its own session report
in the same second, "carried over from earlier, not re-read: the news
research".

`REAL_TICK_*` below is one of those ticks reproduced verbatim from the
2026-09-21 14:22:27 UTC `intra_check` result line. Only the two research
fields are kept — no position, price or P&L figure from the desk appears
in this repo.
"""

from unittest.mock import patch

import pytest

from src import evidence_gate


# --- the 2026-09-21 intra_check tick, verbatim from the production log ---
REAL_TICK_DATA_STATUS = {
    "tech": "ok",
    "macro": "carried_from_morning",
    "news": "expired",
    "earnings": "carried_from_morning",
    "smart_money": "carried_from_morning",
}

REAL_TICK_FRESHNESS = {
    "fresh_seats": ["tech"],
    "carried_seats": ["earnings", "macro", "news", "smart_money"],
    "absent_seats": [],
    "unknown_freshness_seats": [],
    "known_out_of_date_seats": ["news"],
    "seats_total": 5,
    "seats_read_this_tick": 1,
}


def _fired(result, *, mode="intra_check"):
    """Run the session-end alert exactly as `main.py` now runs it."""
    from src.notifier import maybe_alert_data_quality

    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        sent = maybe_alert_data_quality(
            evidence_gate.data_quality_page_input(result), mode=mode,
        )
    body = alert.call_args.args[0] if alert.call_args else ""
    return sent, body


# ===========================================================================
# The regression: the real tick must not page.
# ===========================================================================

def test_the_2026_09_21_carried_over_news_tick_sends_no_red_page():
    result = {
        "run_id": "intra_check-REDACTED",
        "data_status": dict(REAL_TICK_DATA_STATUS),
        "evidence_freshness": dict(REAL_TICK_FRESHNESS),
    }
    sent, body = _fired(result)
    assert sent is False
    assert body == ""


def test_but_that_same_tick_still_says_the_news_was_carried_over():
    """No page is not silence. The session report the owner receives in
    the same second must still name the carried and superseded seat, and
    the seat must still count as degraded everywhere degradation is
    disclosed."""
    from src.notifier import describe_evidence_freshness

    lines = describe_evidence_freshness(dict(REAL_TICK_FRESHNESS))
    joined = " ".join(lines)
    assert "carried over from earlier, not re-read" in joined
    assert "already known to be out of date" in joined
    assert "news research" in joined

    # The disclosure predicate is deliberately UNCHANGED: Risk's ">= 2
    # sources degraded" advisory, the report's "degraded:" line and the
    # postmortem log all still see this seat.
    assert evidence_gate.counts_as_degraded("expired") is True
    degraded = [
        seat for seat, value in REAL_TICK_DATA_STATUS.items()
        if evidence_gate.counts_as_degraded(value)
    ]
    assert degraded == ["news"]


# ===========================================================================
# The thing that must never break: a lost answer still pages.
# ===========================================================================

def test_a_seat_whose_answer_never_arrived_still_pages():
    result = {"data_status": {"tech": "failed", "macro": "ok"}}
    sent, body = _fired(result, mode="morning")
    assert sent is True
    assert "DATA QUALITY ALERT" in body
    assert "tech=failed" in body


def test_a_lost_seat_still_pages_on_a_tick_that_also_has_a_carried_one():
    """The `expired` seat must not drag a genuinely lost one down with it."""
    status = dict(REAL_TICK_DATA_STATUS)
    status["tech"] = "provider_error"
    sent, body = _fired({"data_status": status})
    assert sent is True
    assert "tech=provider_error" in body
    # and the page names what is actually wrong, not the held answer
    assert "news=expired" not in body


@pytest.mark.parametrize(
    "status",
    sorted(
        value for value, category in evidence_gate.STATUS_CATEGORY.items()
        if category == evidence_gate.CATEGORY_LOST
    ),
)
def test_every_lost_status_still_warrants_a_page(status):
    assert evidence_gate.warrants_data_quality_page(status) is True
    sent, _ = _fired({"data_status": {"macro": status}}, mode="morning")
    assert sent is True


def test_an_unclassified_status_still_pages():
    """Fail loud, same default as `counts_as_degraded`: a word nobody has
    classified must not slip out of the alert by accident."""
    assert evidence_gate.warrants_data_quality_page("some_new_word") is True


# ===========================================================================
# The distinction itself, pinned by name.
# ===========================================================================

def test_the_page_exemption_is_exactly_expired_and_nothing_else():
    assert evidence_gate.DISCLOSE_ONLY_STATUSES == frozenset({"expired"})
    assert evidence_gate.warrants_data_quality_page("expired") is False


def test_nothing_but_a_held_answer_may_ever_be_page_exempt():
    """The membership rule, mechanically. A status may sit in
    DISCLOSE_ONLY_STATUSES only if this module already classifies it as
    CATEGORY_EXPIRED — the desk HOLDS an answer. Adding a LOST word here
    would silence the hazard the alert exists for."""
    for status in evidence_gate.DISCLOSE_ONLY_STATUSES:
        assert evidence_gate.STATUS_CATEGORY.get(status) == (
            evidence_gate.CATEGORY_EXPIRED
        ), status


def test_paging_is_strictly_narrower_than_degraded():
    """Everything that pages is degraded; the reverse is not true."""
    vocabulary = set(evidence_gate.STATUS_CATEGORY) | {"an_unknown_word"}
    pages = {s for s in vocabulary if evidence_gate.warrants_data_quality_page(s)}
    degraded = {s for s in vocabulary if evidence_gate.counts_as_degraded(s)}
    assert pages < degraded
    assert degraded - pages == {"expired"}


# ===========================================================================
# The filter itself.
# ===========================================================================

def test_page_worthy_statuses_drops_only_the_held_answers():
    assert evidence_gate.page_worthy_statuses(REAL_TICK_DATA_STATUS) == {}
    assert evidence_gate.page_worthy_statuses(
        {"tech": "truncated", "news": "expired", "macro": "ok"}
    ) == {"tech": "truncated"}


def test_the_filter_returns_the_result_untouched_when_nothing_is_dropped():
    result = {"data_status": {"tech": "failed"}}
    assert evidence_gate.data_quality_page_input(result) is result


def test_the_filter_never_raises_and_never_swallows_a_result():
    assert evidence_gate.data_quality_page_input(None) is None
    assert evidence_gate.data_quality_page_input("not a dict") == "not a dict"
    odd = {"data_status": "not a dict either"}
    assert evidence_gate.data_quality_page_input(odd) is odd


def test_the_filter_does_not_mutate_the_session_result():
    """The same dict goes on to the durable record and the session
    message; the alert's narrower view must not reach either."""
    result = {"data_status": dict(REAL_TICK_DATA_STATUS)}
    evidence_gate.data_quality_page_input(result)
    assert result["data_status"] == REAL_TICK_DATA_STATUS


def test_the_notifiers_own_per_seat_exemption_still_applies_on_top():
    """Tech's per-symbol `low_confidence` exemption lives in the notifier
    and is NOT duplicated in the gate. A tick carrying only that plus an
    expired seat must page for neither — the bug this fix exists for was
    exactly a leftover seat sneaking a page through."""
    result = {"data_status": {"tech": "low_confidence", "news": "expired"}}
    sent, _ = _fired(result)
    assert sent is False
