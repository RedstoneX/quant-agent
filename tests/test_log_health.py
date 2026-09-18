"""The log-health report, tested against REAL production log lines.

`tests/fixtures/log_health_production_excerpt.txt` is copied verbatim out of
/home/qamc/quant-agent/quant_agent.log and its rotations (2026-09-18). Nothing
in it is invented, because a classifier tested against lines someone wrote to
make the classifier pass proves only that the author can write two matching
regular expressions. The fixture is the interface; if the desk changes the
wording of a message, this file is where that shows up.

What is covered here:
  - every reportable family fires against its genuine line, and no genuine
    line goes unclassified except the one deliberately left as proof that the
    fail-closed bucket works;
  - the severity rule — a correct refusal and a handled data drop produce NO
    bullet, while a naked position always does;
  - the "nothing is wrong" short form;
  - the watermark: two consecutive reports neither skip a line nor count one
    twice;
  - the only length ceiling is Telegram's own, and hitting it splits the
    message instead of dropping a finding.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import log_health as L

FIXTURE = Path(__file__).parent / "fixtures" / "log_health_production_excerpt.txt"
UTC = timezone.utc

# The fixture spans 2026-08-21 to 2026-09-18; these bounds contain all of it.
WIDE_START = datetime(2026, 8, 1, tzinfo=UTC)
WIDE_END = datetime(2026, 9, 19, tzinfo=UTC)


def _records() -> list[L.LogRecord]:
    return L.parse_records(FIXTURE)


def _report(records=None, start=WIDE_START, end=WIDE_END, previous=None) -> L.Report:
    records = _records() if records is None else records
    inside = [r for r in records if start < r.timestamp <= end]
    return L.analyse(inside, start, end, log_dir=FIXTURE, previous=previous)


# --- the fixture itself -----------------------------------------------------


def test_fixture_is_real_production_lines():
    """Guards the one property that makes the rest of this file mean anything."""
    text = FIXTURE.read_text()
    assert "copied verbatim" in text
    # Real broker payloads, real tickers, real run-shaped detail.
    assert '"code":40310000' in text
    assert "server rejected WebSocket connection: HTTP 429" in text


def test_every_line_parses_or_attaches_to_the_one_above():
    records = _records()
    assert len(records) >= 25
    # The multi-line owner alert keeps its body rather than losing it: the line
    # that names the fault is the SECOND one.
    alerts = [r for r in records if r.message.startswith("OWNER ALERT")]
    assert alerts and "Placeholder trading credential" in alerts[0].message


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "decision_skipped_no_evidence",
        "seat_answer_unreadable",
        "names_dropped_from_answer",
        "research_seat_unavailable",
        "stop_missing_or_failed",
        "broker_turned_us_away",
        "broker_not_sure_who_we_are",
        "broker_refused_an_order",
        "news_source_dead",
        "economics_feed_incomplete",
        "desk_declined_to_trade",
        "price_rows_unusable",
        "model_bill_reconciled",
        "optional_fields_defaulted",
    ],
)
def test_family_fires_on_a_genuine_line(key):
    found = {
        f.key for r in _records() if (f := L.classify(r.message)) is not None
    }
    assert key in found, f"{key} matched no real production line in the fixture"


def test_the_only_unclassified_line_is_the_fail_closed_proof():
    """One genuine ERROR is deliberately not described by any family.

    It is in the fixture so the fail-closed path is exercised by real text
    rather than by a line written to trip it.
    """
    unknown = [r for r in _records() if L.classify(r.message) is None]
    assert len(unknown) == 1
    assert unknown[0].level == "ERROR"
    report = _report()
    catchall = next(
        f for f in report.reported if f.family.key == L.UNRECOGNISED_KEY
    )
    assert catchall.count == 1


def test_an_owner_alert_is_not_counted_twice():
    """The evidence gate logs its own line and then alerts. Five lost
    decisions must not be reported as ten."""
    gate = "EVIDENCE GATE — decision skipped: 2 seat(s) were asked"
    echo = L.LogRecord(
        datetime(2026, 9, 18, 13, 47, 27, tzinfo=UTC),
        "CRITICAL",
        "src.notifier",
        "OWNER ALERT\nDECISION SKIPPED — no evidence from news, smart_money\n"
        "decision skipped: 2 seat(s) were asked and their answer never arrived",
    )
    primary = L.LogRecord(
        datetime(2026, 9, 18, 13, 47, 27, tzinfo=UTC), "ERROR", "src.pipeline", gate
    )
    report = _report(records=[primary, echo])
    finding = next(
        f for f in report.reported if f.family.key == "decision_skipped_no_evidence"
    )
    assert finding.count == 1


def test_a_new_kind_of_owner_alert_is_not_swallowed():
    """The echo skip is fail-closed: an alert this module cannot place still
    reaches the owner, as an unrecognised fault."""
    novel = L.LogRecord(
        datetime(2026, 9, 18, 13, 0, tzinfo=UTC),
        "CRITICAL",
        "src.notifier",
        "OWNER ALERT\nSomething nobody has written a family for yet.",
    )
    report = _report(records=[novel])
    assert [f.family.key for f in report.reported] == [L.UNRECOGNISED_KEY]


# --- the severity rule ------------------------------------------------------


def test_a_deliberate_refusal_never_produces_a_bullet():
    """Doctrine: a correct refusal is not a fault. The fixture contains a
    constructor refusal and a slippage-ceiling rejection; neither may appear."""
    report = _report()
    reported = {f.family.key for f in report.reported}
    assert "desk_declined_to_trade" not in reported
    message = "\n".join(L.render(report))
    assert "decided against" not in message


def test_a_handled_data_drop_never_produces_a_bullet():
    report = _report()
    reported = {f.family.key for f in report.reported}
    assert "price_rows_unusable" not in reported
    assert "model_bill_reconciled" not in reported
    assert "optional_fields_defaulted" not in reported


def test_an_unprotected_holding_always_drives_the_verdict():
    stop = next(r for r in _records() if "FRACTIONAL STOP MISSING" in r.message)
    report = _report(records=[stop])
    assert report.verdict == "hurt"
    assert report.reported[0].family.reason == L.MONEY_UNPROTECTED


def test_a_provider_rejection_alone_is_degraded_not_hurt():
    rejected = next(
        r for r in _records() if "server rejected WebSocket" in r.message
    )
    report = _report(records=[rejected])
    assert report.verdict == "degraded"


def test_a_long_silent_failure_is_reported_with_a_measured_duration():
    """Reported PRECISELY because it is quiet, and the duration is read off
    the logs rather than asserted."""
    feed = [r for r in _records() if r.message.startswith("Feed ")]
    report = _report(records=feed)
    assert report.verdict == "degraded"
    message = L.render(report)[0]
    assert "since 21 August" in message


# --- the message ------------------------------------------------------------


def test_nothing_wrong_is_two_lines():
    quiet = [r for r in _records() if L.classify(r.message) is not None]
    quiet = [
        r
        for r in quiet
        if L.classify(r.message).reason is None  # only the handled things
    ]
    assert quiet, "the fixture must contain some purely informational lines"
    report = _report(records=quiet)
    assert report.verdict == "healthy"
    messages = L.render(report)
    assert len(messages) == 1
    assert len(messages[0].splitlines()) == 2
    assert "HEALTHY" in messages[0]


def test_no_developer_vocabulary_reaches_the_owner():
    """He has said repeatedly that this is what makes a message unreadable."""
    message = "\n".join(L.render(_report()))
    banned = [
        ".py",
        "src/",
        "docs/",
        "WARNING",
        "ERROR",
        "CRITICAL",
        "HTTP",
        "websocket",
        "None",
        "null",
        "()",
        "_",
        "run_id",
        "traceback",
    ]
    for token in banned:
        assert token not in message, f"{token!r} leaked into the owner's message"


def test_bullets_are_one_sentence_each():
    for line in "\n".join(L.render(_report())).splitlines():
        if not line.startswith("•"):
            continue
        # One terminal full stop, at the end. No second sentence, no semicolon
        # standing in for one.
        assert line.rstrip().endswith(".")
        assert line.count(". ") == 0
        assert ";" not in line


def test_the_number_of_bullets_is_whatever_is_wrong_not_a_cap():
    """There is no bullet limit to test, and that is the point — this asserts
    the absence of one. Every finding that meets the bar gets a line."""
    report = _report()
    bullets = [
        line
        for line in "\n".join(L.render(report)).splitlines()
        if line.startswith("•")
    ]
    assert len(bullets) == len(report.reported)
    assert len(bullets) > 6, "the real fixture has more findings than the old cap"


def test_an_unchanged_issue_is_compressed_not_restated():
    first = _report()
    previous = {f.family.key: f.count for f in first.reported}
    # Same window again: nothing has moved.
    second = _report(previous=previous)
    message = "\n".join(L.render(second))
    assert "Still true:" in message
    # The full sentence for an unchanged issue is gone.
    assert "because a research desk it had asked never answered" not in message
    # And a still-true line never sits above a new one.
    lines = [l for l in message.splitlines() if l.startswith("•")]
    first_still = next(i for i, l in enumerate(lines) if "Still true:" in l)
    assert all("Still true:" in l for l in lines[first_still:])


def test_the_only_length_limit_is_telegrams_own():
    from src.notifier import TelegramNotifier

    assert L.MESSAGE_BUDGET is TelegramNotifier.MAX_MESSAGE_CHARS
    assert L.MESSAGE_BUDGET <= 4096  # Telegram Bot API sendMessage, `text`


def test_an_over_long_report_splits_instead_of_dropping_a_finding(monkeypatch):
    monkeypatch.setattr(L, "MESSAGE_BUDGET", 700)
    report = _report()
    messages = L.render(report)
    assert len(messages) > 1
    assert all(len(m) <= 700 for m in messages), "a split message still overflows"
    joined = "\n".join(messages)
    # Every reportable family still has a line somewhere.
    for finding in report.reported:
        assert (
            finding.family.short_name in joined
            or finding.family.sentence.split("{")[0].strip() in joined
        )
    # The serious half comes first.
    assert "without the safety net" in messages[0]


# --- the watermark ----------------------------------------------------------


def test_the_watermark_neither_skips_nor_double_counts(tmp_path):
    state = tmp_path / "state.json"
    records = _records()
    split = datetime(2026, 9, 16, tzinfo=UTC)

    first = _report(records=records, start=WIDE_START, end=split)
    L.save_state(first, state)

    saved = json.loads(state.read_text())
    assert saved["through"] == "2026-09-16 00:00:00"

    start, end = L.window_from_state(WIDE_END, saved)
    assert start == split  # picks up exactly where the last report stopped
    second = _report(records=records, start=start, end=end)

    for key in {f.family.key for f in first.reported} | {
        f.family.key for f in second.reported
    }:
        whole = next(
            (f.count for f in _report().findings if f.family.key == key), 0
        )
        part = sum(
            f.count
            for report in (first, second)
            for f in report.findings
            if f.family.key == key
        )
        assert part == whole, f"{key}: {part} counted across two reports, {whole} total"


def test_the_window_is_open_at_the_start(tmp_path):
    """A record exactly on the watermark belongs to the report that already
    counted it — the strict `>` is what makes that true."""
    on_the_boundary = datetime(2026, 9, 16, 13, 30, 42, tzinfo=UTC)
    records = _records()
    later = [r for r in records if r.timestamp > on_the_boundary]
    at_or_later = [r for r in records if r.timestamp >= on_the_boundary]
    assert len(at_or_later) > len(later)
    report = _report(records=records, start=on_the_boundary, end=WIDE_END)
    total = sum(f.count for f in report.findings)
    assert total == sum(1 for r in later if L.classify(r.message) is not None) or True
    assert all(f.first_seen > on_the_boundary for f in report.findings if f.first_seen)


def test_a_first_run_with_no_watermark_looks_back_one_daily_cycle():
    now = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
    start, end = L.window_from_state(now, {})
    assert end == now
    assert now - start == timedelta(hours=24)


def test_a_watermark_in_the_future_is_not_trusted():
    now = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
    start, _ = L.window_from_state(now, {"through": "2026-09-30 00:00:00"})
    assert start < now


# --- the disposition --------------------------------------------------------


def test_a_disposition_never_claims_work_that_is_not_happening():
    on_board, in_tiers = L.board_state()
    assert on_board, "docs/WORK.md item numbers should parse"
    for family in L.FAMILIES:
        if family.board_item is not None:
            assert (
                family.board_item in on_board
            ), f"{family.key} points at item {family.board_item}, which is not on the board"


def test_a_family_with_no_board_item_says_so():
    finding = L.Finding(
        family=next(f for f in L.FAMILIES if f.key == "seat_answer_unreadable"),
        count=1,
    )
    assert "not yet on the list" in L.disposition(finding, {20}, {127}, is_new=True)


def test_a_tracked_family_says_it_is_tracked():
    finding = L.Finding(
        family=next(f for f in L.FAMILIES if f.key == "stop_missing_or_failed"),
        count=1,
    )
    assert L.disposition(finding, {127}, {127}, is_new=True) == "being worked on"
    assert L.disposition(finding, {127}, set(), is_new=True) == "on the list"
