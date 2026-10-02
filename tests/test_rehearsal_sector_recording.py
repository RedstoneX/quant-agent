"""The rehearsal's recorded sectors: served when held, UNRECORDED (never invented) when not."""

import json

from ops.rehearsal import sector_recording
from ops.rehearsal.broker import recorded_sector_lookup


def test_pinned_file_holds_the_names_the_morning_session_asked_for():
    table = sector_recording.load()
    for symbol in ("AAPL", "AMD", "ETN", "META", "MRVL", "NET", "NOK", "RKLB", "VLO"):
        assert table.get(symbol), symbol
    assert "Unknown" not in table.values()


def test_market_recordings_own_sector_wins_over_the_pinned_file(tmp_path):
    pinned = tmp_path / "s.json"
    pinned.write_text(json.dumps({"sectors": {"AAPL": "Technology", "ZZZ": "Energy"}}))
    merged = sector_recording.merge_into({"sectors": {"aapl": "Held"}}, pinned)
    assert merged["sectors"] == {"AAPL": "Held", "ZZZ": "Energy"}


def test_a_symbol_with_no_recorded_sector_is_reported_unrecorded_not_invented(tmp_path):
    from src.execution import broker

    record: list[str] = []
    merged = sector_recording.merge_into({}, tmp_path / "absent.json")
    with recorded_sector_lookup(record, merged):
        assert broker.yf.Ticker("NOPE").info == {}
        assert broker.yf.Ticker("nope").info.get("sector") is None
    assert record == ["sector for NOPE (not in the market recording; capture one that includes sectors)"]


def test_missing_file_is_empty_never_a_default(tmp_path):
    assert sector_recording.load(tmp_path / "nope.json") == {}
