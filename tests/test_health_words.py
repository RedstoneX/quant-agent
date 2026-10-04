"""Boundary test: the health report's time words run with no Report built."""

from datetime import datetime, timedelta, timezone

from src.health_words import OWNER_TZ, _duration_words, _plural, _time_words


def test_time_words_carry_the_date_in_owner_time():
    moment = datetime(2026, 10, 4, 17, 5, tzinfo=timezone.utc)
    words = _time_words(moment)
    assert "2026-10-04" in words and "1:05" in words


def test_duration_words_three_bands():
    now = datetime(2026, 10, 4, 12, tzinfo=OWNER_TZ)
    assert _duration_words(now - timedelta(hours=2), now) == "since earlier today"
    assert _duration_words(now - timedelta(days=3), now) == "for 3 days"
    assert _duration_words(now - timedelta(days=20), now).startswith("since 14 September")


def test_plural():
    assert _plural(1) == "" and _plural(2) == "s"
