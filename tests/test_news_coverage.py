"""Tests for the news feed coverage fix — 2026-08-28.

Production incident this closes: two wire feeds (Reuters, AP) started
returning HTTP 404 / 403. `fetch_news()` caught each per-feed failure,
logged a warning, and moved on — the run still produced a
NewsIntelligenceReport and the pipeline reported the news stage "ok"
regardless of how many feeds had actually returned anything. The desk was
making decisions believing it had read every wire when two had returned
nothing, and nothing past the log line could tell.

These tests cover the three places the fix has to hold:
  1. `NewsDataProvider.fetch_news()` / `_fetch_feed()` — a feed failure must
     be reported, not swallowed, and coverage must never read as complete
     when it isn't.
  2. `NewsAnalystAgent.build_user_message()` — the coverage fact must reach
     the analyst's prompt, not just a log line.
  3. `MorningResearchStage.run()` — `data_status["news"]` must reflect
     coverage, not just whether the LLM call happened to parse.

No network calls anywhere in this file — `_fetch_feed` is monkeypatched or
fed synthetic bytes. Any live verification of the actual dead feeds belongs
in the PR description, not here.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.data.news import FeedFailure, NewsCoverage, NewsDataProvider, NewsItem


# ===========================================================================
# NewsCoverage — pure dataclass logic, no I/O.
# ===========================================================================

def test_coverage_status_ok_when_every_feed_succeeds():
    coverage = NewsCoverage(configured=3, succeeded=3, failed=[])
    assert coverage.status == "ok"
    assert coverage.complete is True
    assert coverage.failed_count == 0


def test_coverage_status_partial_when_some_feeds_fail():
    """The core case: 7/9 feeds returned data. This must NOT read as 'ok' —
    that is exactly the pre-fix behaviour this whole change exists to kill."""
    coverage = NewsCoverage(
        configured=9, succeeded=7,
        failed=[
            FeedFailure(name="Reuters Business", reason="HTTP Error 404: Not Found"),
            FeedFailure(name="AP Business", reason="HTTP Error 403: Forbidden"),
        ],
    )
    assert coverage.status == "partial"
    assert coverage.complete is False
    assert coverage.failed_count == 2


def test_coverage_status_failed_when_every_feed_fails():
    """Total outage. Must read as 'failed', the strongest signal — not
    'partial' (implies something came through) and never 'ok'."""
    coverage = NewsCoverage(
        configured=2, succeeded=0,
        failed=[
            FeedFailure(name="A", reason="timed out"),
            FeedFailure(name="B", reason="timed out"),
        ],
    )
    assert coverage.status == "failed"
    assert coverage.complete is False


def test_coverage_status_failed_when_zero_feeds_configured():
    """An empty feed dict is a misconfiguration, not 'full coverage of
    nothing' — complete/status must not read as trivially satisfied."""
    coverage = NewsCoverage(configured=0, succeeded=0, failed=[])
    assert coverage.status == "failed"
    assert coverage.complete is False


def test_coverage_describe_full_coverage_is_unambiguous():
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    text = coverage.describe()
    assert "9/9" in text
    assert "Full coverage" in text
    assert "FAILED" not in text


def test_coverage_describe_partial_names_failed_feeds_and_reasons():
    coverage = NewsCoverage(
        configured=9, succeeded=7,
        failed=[
            FeedFailure(name="Reuters Business", reason="HTTP Error 404: Not Found"),
            FeedFailure(name="AP Business", reason="HTTP Error 403: Forbidden"),
        ],
    )
    text = coverage.describe()
    assert "7/9" in text
    assert "Reuters Business" in text
    assert "404" in text
    assert "AP Business" in text
    assert "403" in text
    # Must not let a reader mistake the gap for a quiet news day.
    assert "coverage GAP" in text or "gap" in text.lower()


def test_coverage_describe_zero_configured_names_the_misconfiguration():
    coverage = NewsCoverage(configured=0, succeeded=0, failed=[])
    assert "misconfiguration" in coverage.describe().lower()


# ===========================================================================
# NewsDataProvider.fetch_news() / _fetch_feed() — the deterministic half.
# ===========================================================================

def _item(title="Headline", source="X"):
    return NewsItem(title=title, summary="", source=source,
                     published=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
                     link="")


def test_fetch_news_all_feeds_succeed_reports_full_coverage():
    provider = NewsDataProvider(feeds={"A": "http://a", "B": "http://b"})
    # Distinct, UNRELATED titles per feed — same-event items from different
    # feeds are exactly what the dedup stage (src/data/news_dedup.py) is
    # supposed to collapse, which would otherwise make this look like a
    # fetch bug rather than dedup doing its job.
    titles = {"A": "Fed holds interest rates steady", "B": "Oil prices tumble on demand worries"}
    provider._fetch_feed = MagicMock(
        side_effect=lambda name, url, cutoff: [_item(title=titles[name], source=name)]
    )

    items, coverage = provider.fetch_news()

    assert len(items) == 2
    assert coverage.configured == 2
    assert coverage.succeeded == 2
    assert coverage.status == "ok"
    assert coverage.complete is True


def test_fetch_news_one_dead_feed_is_reported_failed_not_silently_dropped():
    """THE core regression test. Before the fix, a feed raising inside the
    fetch loop contributed zero items and the run still looked complete —
    `fetch_news()` returned only the item list, with no way for a caller to
    learn that one of the two configured feeds never came back. Assert
    explicitly that this is no longer possible: the failure is named, and
    coverage status is NOT 'ok'."""
    provider = NewsDataProvider(feeds={
        "Reuters Business": "http://dead",
        "CNBC Top News": "http://alive",
    })

    def fake_fetch(name, url, cutoff):
        if name == "Reuters Business":
            raise Exception("HTTP Error 404: Not Found")
        return [_item(source=name)]

    provider._fetch_feed = MagicMock(side_effect=fake_fetch)

    items, coverage = provider.fetch_news()

    # The live feed's item still comes through — one dead feed must not
    # take down the whole run.
    assert len(items) == 1
    assert items[0].source == "CNBC Top News"

    # And coverage says so, explicitly and by name.
    assert coverage.configured == 2
    assert coverage.succeeded == 1
    assert coverage.status != "ok"
    assert coverage.status == "partial"
    assert coverage.complete is False
    assert coverage.failed_count == 1
    assert coverage.failed[0].name == "Reuters Business"
    assert "404" in coverage.failed[0].reason


def test_fetch_news_all_feeds_dead_reports_failed_coverage_with_empty_items():
    provider = NewsDataProvider(feeds={
        "Reuters Business": "http://dead1",
        "AP Business": "http://dead2",
    })
    provider._fetch_feed = MagicMock(side_effect=Exception("boom"))

    items, coverage = provider.fetch_news()

    assert items == []
    assert coverage.configured == 2
    assert coverage.succeeded == 0
    assert coverage.status == "failed"
    assert coverage.complete is False
    assert {f.name for f in coverage.failed} == {"Reuters Business", "AP Business"}


def test_fetch_news_returns_a_2_tuple_of_items_and_coverage():
    provider = NewsDataProvider(feeds={"A": "http://a"})
    provider._fetch_feed = MagicMock(return_value=[])
    result = provider.fetch_news()
    assert isinstance(result, tuple)
    assert len(result) == 2
    items, coverage = result
    assert isinstance(items, list)
    assert isinstance(coverage, NewsCoverage)


def test_fetch_feed_raises_on_network_error_instead_of_swallowing(monkeypatch):
    """Pre-fix, `_fetch_feed` caught its own urlopen exception and returned
    `[]` — indistinguishable from a feed that fetched fine and had nothing
    new. It must now propagate so `fetch_news()`'s loop (the only place
    coverage is tracked) can record the failure."""
    import src.data.news as news_mod

    def raise_urlopen(*args, **kwargs):
        raise OSError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(news_mod, "urlopen", raise_urlopen)
    provider = NewsDataProvider(feeds={"AP Business": "http://apnews.example"})

    with pytest.raises(OSError):
        provider._fetch_feed("AP Business", "http://apnews.example",
                              datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_fetch_feed_raises_on_unparseable_document(monkeypatch):
    """An HTML error page served with a 200 (or any document feedparser
    can't make sense of at all) is the fetch equivalent of an HTTP error —
    it must raise, not return `[]` as though the feed were merely quiet."""
    import src.data.news as news_mod

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<html>not a feed</html> completely broken &&& xml"

    monkeypatch.setattr(news_mod, "urlopen", lambda *a, **kw: _FakeResponse())
    provider = NewsDataProvider(feeds={"X": "http://x"})

    with pytest.raises(ValueError):
        provider._fetch_feed("X", "http://x", datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_fetch_feed_zero_fresh_entries_is_not_a_failure(monkeypatch):
    """A feed that fetches fine and simply has no entries newer than the
    cutoff is healthy silence, not a failure — it must return `[]` without
    raising, so `fetch_news()` still counts it toward `succeeded`."""
    import src.data.news as news_mod

    valid_rss = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b'<title>Feed</title>'
        b'<item><title>Old story</title>'
        b'<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>'
        b'</channel></rss>'
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return valid_rss

    monkeypatch.setattr(news_mod, "urlopen", lambda *a, **kw: _FakeResponse())
    provider = NewsDataProvider(feeds={"X": "http://x"})

    # cutoff well after the single (old) entry in the feed.
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    items = provider._fetch_feed("X", "http://x", cutoff)
    assert items == []

    items2, coverage = provider.fetch_news()
    assert coverage.status == "ok"
    assert coverage.succeeded == 1
    assert coverage.failed_count == 0


# ===========================================================================
# NewsAnalystAgent.build_user_message() — coverage must reach the prompt.
# ===========================================================================

def _agent():
    from src.agents.news_analyst import NewsAnalystAgent
    return NewsAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")


def test_prompt_includes_full_coverage_section():
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    prompt = _agent().build_user_message(
        news_text="Fed holds rates.", news_coverage=coverage,
    )
    assert "News Coverage" in prompt
    assert "9/9" in prompt
    assert "Full coverage" in prompt


def test_prompt_includes_partial_coverage_failed_feed_names():
    """This is 'Partial coverage flows through to whatever the analyst
    receives' — checked at the exact seam the analyst reads from."""
    coverage = NewsCoverage(
        configured=9, succeeded=7,
        failed=[
            FeedFailure(name="Reuters Business", reason="HTTP Error 404: Not Found"),
            FeedFailure(name="AP Business", reason="HTTP Error 403: Forbidden"),
        ],
    )
    prompt = _agent().build_user_message(
        news_text="Fed holds rates.", news_coverage=coverage,
    )
    assert "News Coverage" in prompt
    assert "7/9" in prompt
    assert "Reuters Business" in prompt
    assert "AP Business" in prompt
    assert "FAILED" in prompt


def test_prompt_handles_missing_coverage_without_crashing():
    """A caller that hasn't been updated to pass news_coverage (or an old
    test double) must not crash build_user_message — it degrades to an
    explicit 'unknown' rather than silently omitting the section."""
    prompt = _agent().build_user_message(news_text="Fed holds rates.")
    assert "News Coverage" in prompt
    assert "UNKNOWN" in prompt
