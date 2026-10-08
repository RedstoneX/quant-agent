"""A recording holds answers or nothing: no committed recorded input may be an error."""

import gzip
import json
from pathlib import Path

import pytest

from ops.rehearsal import feed_recording

RECORDINGS = Path(__file__).resolve().parent.parent / "ops" / "rehearsal" / "recordings"


def _load(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        return json.load(handle)


def _error_shaped(node, where=""):
    """Paths of every entry that is `ok: false`, has an error key, or has status >= 400."""
    found = []
    if isinstance(node, dict):
        status = node.get("status")
        if (node.get("ok") is False or "error" in node or "error_type" in node
                or (isinstance(status, int) and not isinstance(status, bool) and status >= 400)):
            found.append(where or "<root>")
        else:
            for key, child in node.items():
                found.extend(_error_shaped(child, f"{where}/{key}"))
    elif isinstance(node, list) and len(node) < 50:
        for n, child in enumerate(node):
            found.extend(_error_shaped(child, f"{where}[{n}]"))
    return found


def _files():
    return sorted(p for p in RECORDINGS.iterdir() if p.suffix in (".json", ".gz"))


def test_recordings_exist():
    assert _files()


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_no_recorded_input_is_error_shaped(path):
    bad = _error_shaped(_load(path))
    assert not bad, f"{path.name}: {len(bad)} error-shaped recorded inputs, e.g. {bad[:3]}"


def test_capture_refuses_failures_and_records_nothing(tmp_path, monkeypatch):
    from src.data import fred_series_client

    class _Broken:
        def __init__(self, **_):
            pass

        def get_series(self, sid):
            raise ValueError("Bad Request. api_key rejected")

        get_series_info = get_series

    monkeypatch.setattr(fred_series_client, "FredSeriesClient", _Broken)
    out = tmp_path / "feeds.json"
    result = feed_recording.capture(["DGS10"], ["http://127.0.0.1:9/nothing"], out)
    assert result["fred_series"] == {} and result["fred_series_info"] == {} and result["http"] == {}
    assert len(result["refused"]) == 3
    assert not _error_shaped(_load(out))


def test_merge_purges_error_entries_already_held(tmp_path):
    out = tmp_path / "feeds.json"
    out.write_text(json.dumps({"fred_series": {"X": {"ok": False, "error": "e"}}, "http": {}}))
    result = feed_recording.capture([], [], out, merge=True)
    assert result["fred_series"] == {}
