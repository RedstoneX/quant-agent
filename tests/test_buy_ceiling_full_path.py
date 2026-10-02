"""Board item 218: a BUY written by a full morning session carries the
constructor's MEASURED `structural_ceiling` into the `trades` row.

The guard in `test_buy_insert_evidence_guard.py` proves the insert site PASSES
the kwarg and the ledger test proves the ledger STORES it. Neither proves the
value survives every hop between them (constructor -> PM stage -> risk stage
-> execution -> insert). This drives the real stages end to end on the
rehearsal broker and reads the row back from the real database.

`stop_basis` is deliberately NOT asserted non-null: the constructor ships
`stop_rule=None` for a stop with no level behind it, and recording a
substitute there would be a fabricated audit value.
"""
from __future__ import annotations

from src.models import TradeDecision
from src.storage.db import Database
from tests.test_e2e_morning_session import _run_session


def test_buy_row_carries_the_constructors_measured_ceiling(tmp_path, monkeypatch):
    built: list[tuple[str, bool | None]] = []
    rows: list[tuple] = []
    real_init = TradeDecision.__init__
    real_insert = Database.insert_trade

    def _init(self, *a, **k):
        real_init(self, *a, **k)
        if self.action in ("BUY", "SHORT"):
            built.append((self.symbol, self.structural_ceiling))

    def _insert(self, *a, **k):
        row_id = real_insert(self, *a, **k)
        rows.append(tuple(self.conn.execute(
            "SELECT symbol, action, fill_status, structural_ceiling, entry_atr "
            "FROM trades WHERE id = ?", (row_id,),
        ).fetchone()))
        return row_id

    monkeypatch.setattr(TradeDecision, "__init__", _init)
    monkeypatch.setattr(Database, "insert_trade", _insert)
    result, _trace, _trading = _run_session(tmp_path, monkeypatch)

    assert result["status"] == "executed", result.get("status")
    buys = [r for r in rows if r[1] in ("BUY", "SHORT") and r[2] == "pending_submit"]
    assert len(buys) == 1, f"expected one entry row, got {rows}"
    symbol, _action, _status, stored, entry_atr = buys[0]
    constructed = [v for s, v in built if s == symbol]
    assert constructed and all(isinstance(v, bool) for v in constructed), (
        f"the constructor must emit a real boolean verdict, got {constructed}"
    )
    assert stored is not None, (
        "structural_ceiling was lost between the constructor and the trade row"
    )
    assert bool(stored) == constructed[0]
    assert entry_atr is not None, "entry_atr must reach the same row"
