"""The SEC ticker map and the per-symbol Yahoo headline feeds replay offline.

Runs inside the rehearsal's own network wall; an empty journal proves the
bodies came from the committed recording and not the internet.
"""

import json

from ops.rehearsal.feed_recording import load, recorded_feeds
from ops.rehearsal.isolation import assert_hermetic, no_network
from src.data import news, sec_client


def test_sec_map_and_yahoo_symbol_feed_served_from_recording_without_network():
    recording = load()
    unavailable: list[str] = []
    with no_network(unavailable) as journal, recorded_feeds(unavailable, recording):
        with sec_client.urlopen(sec_client.Request(sec_client.SEC_TICKERS_URL)) as resp:
            table = json.loads(resp.read())
        yahoo_url = news.YAHOO_PER_SYMBOL_URL_TEMPLATE.format(symbol="AAPL")
        with news.urlopen(news.Request(yahoo_url)) as resp:
            body = resp.read()
    assert len(table) > 1000
    assert b"<rss" in body
    assert not journal and not unavailable
