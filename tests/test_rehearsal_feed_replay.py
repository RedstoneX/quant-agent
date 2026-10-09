"""Board item 202: FRED and the news feeds replay from a recording, offline.

Every test here runs INSIDE the rehearsal's own network wall (`no_network`),
which journals any outbound attempt. An empty journal is the proof that the
replay was served from the recording and not from the internet — the same
discriminator `tests/test_rehearsal_hermetic.py` uses, and the one the
curl_cffi hole needed, since a transport can succeed rather than erroring.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ops.rehearsal.feed_recording import (
    RecordedFeedFailure,
    recorded_feeds,
    url_key,
)
from ops.rehearsal.isolation import MissingRecordedInput, assert_hermetic, no_network

FEED_URL = "https://feeds.marketwatch.com/marketwatch/topstories/"
_RECENT = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S +0000")
_RSS = (
    "<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>"
    "<item><title>Recorded headline</title><description>d</description>"
    f"<link>https://example.invalid/a</link><pubDate>{_RECENT}</pubDate></item>"
    "</channel></rss>"
).encode()


def _recording():
    import base64

    return {
        "captured_utc": "2026-10-01T00:00:00Z",
        "fred_series": {
            "DGS10": {"ok": True, "index": ["2026-09-29", "2026-09-30"], "values": [4.1, 4.2]},
            "T10Y2Y": {"ok": False, "error_type": "timeout", "error": "fetch_deadline_exceeded"},
        },
        "fred_series_info": {
            "DGS10": {"ok": True, "info": {"observation_end": "2026-09-30", "last_updated": "2026-09-30"}},
        },
        "http": {url_key(FEED_URL): {"ok": True, "body_b64": base64.b64encode(_RSS).decode()}},
    }


def test_a_recorded_fred_series_is_served_without_touching_the_network():
    import src.data.macro as macro

    journal, missing = [], []
    with no_network(journal), recorded_feeds(missing, _recording()):
        series = macro.Fred(api_key="unused").get_series("DGS10")
    assert list(series.values) == [4.1, 4.2]
    assert journal == [], f"the replay reached the network: {journal}"
    assert missing == []
    assert_hermetic(journal, missing)


def test_a_recorded_news_feed_is_served_without_touching_the_network():
    from src.data.news import NewsDataProvider

    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    journal, missing = [], []
    with no_network(journal), recorded_feeds(missing, _recording()):
        provider = NewsDataProvider()
        items = provider._fetch_feed("MarketWatch Top", FEED_URL, cutoff)
    assert [i.title for i in items] == ["Recorded headline"]
    assert journal == [], f"the replay reached the network: {journal}"
    assert_hermetic(journal, missing)


def test_a_recorded_fred_failure_replays_as_a_failure_not_as_a_success():
    # Every FRED failure in the retained log is `fetch_deadline_exceeded`, so
    # a recording that only replays successes cannot reproduce the thing the
    # log is full of.
    import src.data.macro as macro

    journal, missing = [], []
    with no_network(journal), recorded_feeds(missing, _recording()):
        with pytest.raises(RecordedFeedFailure) as caught:
            macro.Fred(api_key="unused").get_series("T10Y2Y")
    assert "fetch_deadline_exceeded" in str(caught.value)
    assert journal == []


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: __import__("src.data.macro", fromlist=["Fred"]).Fred(api_key="unused").get_series("UNRECORDED"),
            id="fred",
        ),
        pytest.param(
            lambda: __import__("src.data.news", fromlist=["urlopen"]).urlopen(
                "https://feeds.npr.org/1001/rss.xml", timeout=1
            ),
            id="news",
        ),
    ],
)
def test_a_gap_in_the_recording_fails_loudly_and_is_never_fetched_or_filled_in(call):
    journal, missing = [], []
    with no_network(journal), recorded_feeds(missing, _recording()):
        with pytest.raises(RecordedFeedFailure):
            call()
    assert journal == [], "a gap fell through to the network instead of raising"
    assert missing, "a gap was not recorded as a missing recorded input"
    with pytest.raises(MissingRecordedInput):
        assert_hermetic(journal, missing)


def test_the_recording_key_never_carries_an_api_key():
    key = url_key("https://api.stlouisfed.org/fred/release/dates?release_id=10&api_key=secret")
    assert "secret" not in key and "api_key" not in key
    assert key.startswith("https://api.stlouisfed.org/fred/release/dates?release_id=10")


def test_every_urlopen_holder_on_a_session_path_is_patched_and_restored():
    import importlib

    from ops.rehearsal.feed_recording import _URLOPEN_MODULES

    before = {n: getattr(importlib.import_module(n), "urlopen") for n in _URLOPEN_MODULES}
    missing = []
    with recorded_feeds(missing, _recording()):
        for name in _URLOPEN_MODULES:
            module = importlib.import_module(name)
            assert getattr(module, "urlopen") is not before[name], (
                f"{name} still holds the live urlopen, so its feeds would be fetched"
            )
    for name in _URLOPEN_MODULES:
        assert getattr(importlib.import_module(name), "urlopen") is before[name]
