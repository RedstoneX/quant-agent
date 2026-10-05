"""Witness for src.data.smart_money_stores: built from plain paths in a tmp dir, the provider never imported."""
import json
import os
import time
from pathlib import Path

from src.data.smart_money_stores import SmartMoneyStores, build_smart_money_stores


def _paths(root: Path) -> dict:
    return dict(
        manifest_path=root / "m.json", observations_path=root / "o.json",
        history_path=root / "h.json", tickers_path=root / "t.json", raw_dir=root / "raw",
    )


def test_stores_hold_exactly_the_paths_handed_in(tmp_path):
    paths = _paths(tmp_path)
    stores = SmartMoneyStores(**paths)
    for name, path in paths.items():
        assert getattr(stores, name) is path


def test_absent_files_read_as_the_fallbacks(tmp_path):
    stores = SmartMoneyStores(**_paths(tmp_path))
    assert stores.load_manifest() == {}
    assert stores.load_observations() == []
    assert stores.load_history() == {}
    assert stores.load_tickers() == {}
    assert stores.cached_filing("0000000000-26-000001") is None


def test_writes_round_trip_and_leave_no_temp_file(tmp_path):
    stores = SmartMoneyStores(**_paths(tmp_path))
    stores.save_manifest({"processed_accessions": ["a"]})
    stores.save_observations([{"k": 1}])
    stores.save_history({"1|XYZ": ["2026-01-02|buy"]})
    assert stores.load_manifest() == {"processed_accessions": ["a"]}
    assert stores.load_observations() == [{"k": 1}]
    assert stores.load_history() == {"1|XYZ": ["2026-01-02|buy"]}
    assert not list(tmp_path.glob("*.tmp"))


def test_unreadable_file_falls_back_instead_of_raising(tmp_path):
    paths = _paths(tmp_path)
    paths["manifest_path"].write_text("{not json")
    assert SmartMoneyStores(**paths).load_manifest() == {}


def test_tickers_stale_when_missing_or_older_than_a_day(tmp_path):
    stores = SmartMoneyStores(**_paths(tmp_path))
    assert stores.tickers_stale() and not stores.tickers_cached()
    stores.save_tickers({"fields": [], "data": []})
    assert stores.tickers_cached() and not stores.tickers_stale()
    old = time.time() - 25 * 3600
    os.utime(stores.tickers_path, (old, old))
    assert stores.tickers_stale()


def test_filing_cache_round_trips_bytes_as_text(tmp_path):
    stores = build_smart_money_stores(tmp_path / "data")
    stores.save_filing("0000000000-26-000002", "<x>é</x>".encode())
    assert stores.cached_filing("0000000000-26-000002") == "<x>é</x>"
    assert json.loads(json.dumps(stores.raw_dir.name)) == "filings"


def test_builder_creates_the_directories_and_standard_names(tmp_path):
    stores = build_smart_money_stores(tmp_path / "d")
    assert stores.raw_dir.is_dir()
    assert stores.manifest_path.name == "manifest.json"
    assert stores.history_path.name == "insider_history.json"
