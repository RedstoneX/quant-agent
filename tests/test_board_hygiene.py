"""Daily board-hygiene check: `scripts/check_board_hygiene.py`.

Covers the contract from `docs/WORK.md`'s own standing rule (the board
holds only open work) and the gap a quiet week leaves in the two checks
that already enforce it (the 100,000-byte cap test and the CI gate for a
finished item still marked open): nothing notices between edits. This
script is the daily, read-only tripwire for that gap.

Nothing here touches the network, Telegram, or the production box. The cap
is read from the real `tests/test_status_board.py` in this repository —
deliberately never re-typed — so a change to that cap is picked up
automatically and this file never needs to be told the number.
"""
from __future__ import annotations

from pathlib import Path

import scripts.check_board_hygiene as check_board_hygiene
from scripts.check_board_hygiene import (
    BoardHygieneReport,
    build_report,
    format_message,
    item_numbers,
    read_cap_bytes,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_cap_is_read_from_the_real_test_file_not_invented():
    cap, error = read_cap_bytes(PROJECT_ROOT)
    assert error is None
    assert cap == 100_000


def _write_fixture_repo(repo: Path) -> None:
    (repo / "docs").mkdir()
    (repo / "docs" / "WORK.md").write_text("# Backlog\n\nsmall and tidy\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_status_board.py").write_text(
        "def test_work_md_stays_under_a_hundred_thousand_bytes():\n"
        "    assert size <= 100_000, 'over cap'\n"
    )


def test_healthy_board_produces_no_finding(tmp_path, monkeypatch):
    """A board well under its cap with nothing parked must report nothing
    — a daily "board is fine" message is exactly the noise the owner has
    said to never send."""
    _write_fixture_repo(tmp_path)
    monkeypatch.setattr(
        check_board_hygiene, "_finished_item_check_override", lambda path: []
    )

    report = build_report(str(tmp_path))
    assert report.has_finding is False
    assert format_message(report) == ""


def test_report_fires_when_items_look_finished_but_are_still_parked(
    tmp_path, monkeypatch
):
    _write_fixture_repo(tmp_path)
    monkeypatch.setattr(
        check_board_hygiene,
        "_finished_item_check_override",
        lambda path: ["item 12: FIXED - old work", "item 47: DONE - other"],
    )

    report = build_report(str(tmp_path))
    assert report.has_finding is True
    assert item_numbers(report.parked_items) == ["12", "47"]
    message = format_message(report)
    assert "12" in message and "47" in message
    # Plain English, no jargon, no file paths.
    assert "docs/" not in message
    assert ".py" not in message


def test_report_fires_when_the_board_is_near_its_cap():
    report = BoardHygieneReport(
        work_md_path="irrelevant", cap_bytes=100_000, size_bytes=96_000,
    )
    assert report.near_cap is True
    assert report.has_finding is True
    message = format_message(report)
    assert "96%" in message


def test_report_stays_quiet_comfortably_under_the_near_cap_share():
    report = BoardHygieneReport(
        work_md_path="irrelevant", cap_bytes=100_000, size_bytes=50_000,
    )
    assert report.near_cap is False
    assert report.has_finding is False


def test_finished_item_check_degrades_honestly_when_unavailable(tmp_path, monkeypatch):
    """If `scripts/status_board.py`'s finished-item function is ever
    missing or renamed, this must say so rather than silently reporting
    zero parked items — the difference between "checked, found nothing"
    and "could not check" matters for a hygiene gate."""
    _write_fixture_repo(tmp_path)
    monkeypatch.setattr(check_board_hygiene, "_load_finished_item_check", lambda: None)

    report = build_report(str(tmp_path))
    assert report.parked_items == []
    assert report.parked_check_error is not None


def test_no_llm_or_trading_import_in_the_module():
    """A cheap static guard: this is a read-only reporting script and must
    never gain an LLM client or an order-placing import."""
    source = (PROJECT_ROOT / "scripts" / "check_board_hygiene.py").read_text()
    banned = ("openai", "anthropic", "alpaca", "place_order", "submit_order")
    lowered = source.lower()
    for token in banned:
        assert token not in lowered, f"{token!r} must not appear in a read-only check"
