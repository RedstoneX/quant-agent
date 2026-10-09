"""Boundary witness: the pieces lifted out of the trade ledger stand on their own.

position_chain is pure functions; backfills, recovery_queues and excursions
take the TradeLedger as their first argument, so each is exercised here on a
bare in-memory connection with no Database or pipeline object behind it.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from src.storage.db import Database
from src.storage.schema.manager import DatabaseSchema
from src.storage.trades import ledger as ledger_module
from src.storage.trades import backfills, excursions, exit_reasons, position_chain, recovery_queues
from src.storage.trades.ledger import TradeLedger
from tests.boundary_harness import check_boundary

_PIECES = ("position_chain", "exit_reasons", "backfills", "recovery_queues", "excursions")


@pytest.mark.parametrize("piece", _PIECES)
def test_piece_passes_the_boundary_harness(piece):
    verdict = check_boundary(f"src.storage.trades.{piece}")
    assert not verdict.failures, verdict.failures


def _bare_ledger() -> TradeLedger:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    DatabaseSchema(conn=conn)._create_tables()
    return TradeLedger(
        conn=conn,
        lock=threading.Lock(),
        locked_write=lambda do, *, label="write": do(),
        executed_trade_predicate=Database._executed_trade_predicate,
        sqlite_utc_timestamp=Database._sqlite_utc_timestamp,
        et_day_utc_bounds=Database._et_day_utc_bounds,
    )


def test_every_piece_exposes_its_lifted_bodies():
    assert callable(backfills.backfill_position_ids)
    assert callable(recovery_queues.insert_pending_repeg)
    assert callable(excursions.sync_positions)


def test_lifted_helpers_are_one_definition_reexported_by_the_ledger():
    for name in ("_assign_position_ids", "_POSITION_EXIT_ACTIONS"):
        assert getattr(ledger_module, name) is getattr(position_chain, name)
    for name in ("_categorize_exit_reason", "_resolve_decision_id_status", "_extract_pm_targets"):
        assert getattr(ledger_module, name) is getattr(exit_reasons, name)


def test_recovery_queue_and_excursion_bodies_run_on_a_bare_ledger():
    ledger = _bare_ledger()
    assert ledger.get_pending_repegs() == []
    assert ledger.get_pending_protection_restores() == []
    assert ledger.sync_positions([]) is None
    assert ledger.prune_pending_repegs(1) == 0
