"""Recorded macro for a rehearsal: served from the pinned fixture, never invented."""
import pytest

from ops.rehearsal import macro_recording
from src.data.macro import CONFIGURED_SERIES

_KEY_REJECTED = {"ok": False, "error_type": "ValueError",
                 "error": "Bad Request. The value for variable api_key is not a 32 character string"}
_TIMEOUT = {"ok": False, "error_type": "TimeoutError", "error": "The read operation timed out"}


def test_pinned_fixture_covers_every_configured_series():
    series, info = macro_recording.tables_from_pinned_fixture()
    assert set(series) == set(CONFIGURED_SERIES) == set(info)
    assert all(e["ok"] and e["index"] and len(e["index"]) == len(e["values"]) for e in series.values())


def test_key_rejection_from_a_capture_is_not_a_recording_but_a_real_failure_is():
    merged = macro_recording.merge_into({
        "fred_series": {"VIXCLS": _KEY_REJECTED, "DGS10": _TIMEOUT},
        "fred_series_info": {"VIXCLS": _KEY_REJECTED},
    })
    assert merged["fred_series"]["VIXCLS"]["ok"] is True
    assert merged["fred_series"]["DGS10"] == _TIMEOUT
    assert merged["fred_series_info"]["VIXCLS"]["ok"] is True


def test_unrecorded_series_raises_loudly_and_never_goes_live(monkeypatch):
    from ops.rehearsal import feed_recording

    recording = macro_recording.merge_into(None)
    del recording["fred_series"]["ICSA"]
    seen: list[str] = []
    with macro_recording.recorded_feeds_with_macro(seen, recording):
        from src.data.fred_series_client import FredSeriesClient

        client = FredSeriesClient(api_key="x" * 32)  # built like the pipeline's, inside the window
        assert len(client.get_series("VIXCLS")) > 0
        with pytest.raises(feed_recording.RecordedFeedFailure):
            client.get_series("ICSA")
    assert any("ICSA" in n for n in seen)
