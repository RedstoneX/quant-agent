"""A malformed FRED payload is loud and counted before cache persistence stops."""
from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

from src.data.macro import MacroDataProvider
from src.sentinel import counted
from src.storage.schema.sentinel_tables import ensure_sentinel_tables


class _UnreadableSeries:
    def items(self):
        raise ValueError("unreadable FRED observations")


def test_series_cache_serialisation_failure_is_loud_counted_and_not_saved(
    tmp_path, monkeypatch, caplog,
):
    path = tmp_path / "quant_agent.db"
    conn = sqlite3.connect(path)
    ensure_sentinel_tables(conn=conn)
    conn.close()
    monkeypatch.setattr(counted, "db_path", lambda: path)

    save = Mock()
    provider = SimpleNamespace(series_cache=SimpleNamespace(save=save))
    with caplog.at_level(logging.ERROR, logger="src.data.macro"):
        result = MacroDataProvider._write_cache(
            provider, "DGS10", {}, _UnreadableSeries(), None,
        )

    assert result is None
    save.assert_not_called()
    assert "money-path guard swallowed a fault" in caplog.text
    assert "unreadable FRED observations" in caplog.text

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT kind, agreed, detail FROM reconciliation_runs"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    kind, agreed, detail = row
    assert kind == "guarded:data.macro.series_cache_serialise"
    assert agreed == 0
    assert "DGS10" in detail
    assert "ValueError" in detail
