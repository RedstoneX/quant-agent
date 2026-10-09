"""Owner ruling 2026-10-09: a short is covered whole or not at all."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.stage_execution_parts import cover_loop
from src.stage_execution_parts.state import SKIP


def _decision(pct):
    return SimpleNamespace(symbol="TSLA", allocation_pct=pct)


def _pipeline():
    p = MagicMock()
    p._full_sell_qty.side_effect = lambda q: q
    return p


def test_partial_cover_is_refused_and_recorded():
    pipeline, ctx = _pipeline(), object()
    with patch.object(cover_loop, "_record_pipeline_event") as rec:
        assert cover_loop.cover_qty_and_label(pipeline, _decision(50.0), 40.0, ctx) is SKIP
    rec.assert_called_once()
    args = rec.call_args.args
    assert args[2:6] == ("TSLA", "order", "refused", cover_loop.PARTIAL_COVER_REFUSED)
    assert rec.call_args.kwargs["allocation_pct"] == 50.0


def test_refusal_survives_a_failed_record_write():
    with patch.object(cover_loop, "_record_pipeline_event", side_effect=RuntimeError("db")):
        assert cover_loop.cover_qty_and_label(_pipeline(), _decision(25.0), 40.0, object()) is SKIP


def test_full_cover_covers_whole():
    with patch.object(cover_loop, "_record_pipeline_event") as rec:
        assert cover_loop.cover_qty_and_label(_pipeline(), _decision(100.0), 40.0, object()) == (40.0, "COVER")
    rec.assert_not_called()


def test_zero_allocation_is_skipped_unrecorded():
    with patch.object(cover_loop, "_record_pipeline_event") as rec:
        assert cover_loop.cover_qty_and_label(_pipeline(), _decision(0), 40.0, object()) is SKIP
    rec.assert_not_called()
