"""The buy-evidence columns must be non-NULL after a real end-to-end buy.

WHY THIS EXISTS
---------------
The production ledger holds BUY/SHORT rows on which `structural_ceiling`
and `stop_basis` are NULL and `entry_atr` is mostly NULL. Those three
columns are the only record of WHY a position was allowed to be the size
it was and WHAT its stop was measured against; neither fact can be
reconstructed afterwards, because the ATR has moved and the
constructor's stop rule is stored nowhere else.

A column is only evidence if the path a REAL buy takes writes it. This
drives the same hermetic morning session as
`tests/test_e2e_morning_session.py` — real config, real stages, real
agent classes, synthetic bars, stand-in broker, scripted model seats, no
network — and then reads the ledger row the run actually inserted.

The companion test below BREAKS the write and shows this checker goes
red, because a check that has never been seen fail is not evidence.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_e2e_morning_session import SYMBOL, _run_session

# Columns pinned at entry because they cannot be recovered later. Each is
# written by `src/stage_execution.py` on the single `insert_trade` call
# every entry goes through.
EVIDENCE_COLUMNS = ("structural_ceiling", "entry_atr", "stop_level_basis")


def _entry_rows(tmp_path: Path) -> list[sqlite3.Row]:
    """Every BUY/SHORT row the run left behind, from whichever sqlite file
    the pipeline created under the session's own directory."""
    candidates = sorted(tmp_path.rglob("*.db"))
    assert candidates, f"the run wrote no sqlite file under {tmp_path}"
    rows: list[sqlite3.Row] = []
    for path in candidates:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            names = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
            if not names:
                continue
            rows += list(conn.execute(
                "SELECT * FROM trades WHERE action IN ('BUY', 'SHORT')"
            ))
        except sqlite3.DatabaseError:
            continue
        finally:
            conn.close()
    return rows


def _assert_evidence_present(rows) -> None:
    assert rows, "the session executed no entry, so there is no evidence to check"
    for row in rows:
        keys = row.keys()
        for column in EVIDENCE_COLUMNS:
            assert column in keys, f"the ledger has no `{column}` column at all"
            assert row[column] is not None, (
                f"{row['action']} {row['symbol']} was written with "
                f"`{column}` NULL — the desk cannot say why it bought this"
            )


def test_a_real_buy_writes_its_entry_evidence(tmp_path, monkeypatch):
    result, _trace, trading = _run_session(tmp_path, monkeypatch)
    assert result["status"] == "executed", (
        f"the session never reached execution: {result.get('status')} "
        f"{result.get('error', '')}"
    )
    buys = [o for o in trading.submitted if str(o.side).lower().endswith("buy")]
    assert [o.symbol for o in buys] == [SYMBOL], (
        f"no real buy was placed: {[(o.symbol, str(o.side)) for o in trading.submitted]}"
    )
    _assert_evidence_present(_entry_rows(tmp_path))


def test_the_evidence_check_fails_when_the_constructor_stops_pinning_it(
    tmp_path, monkeypatch,
):
    """Sensitivity: break the write at its source and the checker must go
    red. `shipped_stop_level_basis` is the constructor method that pins
    `stop_level_basis` onto the TradeDecision; with it silenced the row is
    written with the column NULL, which is exactly the production symptom."""
    from src.portfolio_constructor.stops import _StopMixin

    with patch.object(
        _StopMixin, "shipped_stop_level_basis",
        lambda self, *a, **k: None,
    ):
        result, _trace, _trading = _run_session(tmp_path, monkeypatch)
    assert result["status"] == "executed", (
        f"the broken run must still execute, or it proves nothing: "
        f"{result.get('status')}"
    )
    with pytest.raises(AssertionError) as caught:
        _assert_evidence_present(_entry_rows(tmp_path))
    assert "stop_level_basis" in str(caught.value), caught.value


# --------------------------------------------------------------------------
# The scale-in cascade — the defect this change fixes
# --------------------------------------------------------------------------
# `stop_basis` is deliberately NOT in EVIDENCE_COLUMNS above. It is written
# from `TradeDecision.stop_rule`, which `shipped_stop_rule` fills ONLY when
# the shipping stop sits at a level the desk computed; None is its honest
# answer otherwise, and the hermetic run above produces a legitimate None.
# It records an exemption from the ATR floor, not what the stop was based
# on — `stop_level_basis` is the column that records that, and it is
# asserted above.

class _Decision:
    def __init__(self, setup_type=None, structural_ceiling=None):
        self.setup_type = setup_type
        self.structural_ceiling = structural_ceiling


def test_a_scale_in_keeps_the_positions_own_pinned_verdict():
    """An ADD must never reclassify a position that already has a verdict."""
    from src.execution.entry_evidence import (
        pinned_setup_type, pinned_structural_ceiling,
    )
    prior = {"setup_type": "breakout", "structural_ceiling": 1}
    fresh = _Decision(setup_type="range", structural_ceiling=False)
    assert pinned_setup_type(prior, fresh, is_scale_in=True) == "breakout"
    assert pinned_structural_ceiling(prior, fresh, is_scale_in=True) is True


def test_a_scale_in_onto_a_pre_feature_row_records_the_fresh_verdict():
    """The cascade: every production ADD copied a NULL `structural_ceiling`
    from a row opened before the column existed, discarding the verdict the
    constructor had computed on that very decision. The hole must not
    propagate."""
    from src.execution.entry_evidence import (
        pinned_setup_type, pinned_structural_ceiling,
    )
    pre_feature = {"setup_type": "range", "structural_ceiling": None}
    fresh = _Decision(setup_type="range", structural_ceiling=True)
    assert pinned_structural_ceiling(pre_feature, fresh, is_scale_in=True) is True
    assert pinned_setup_type(pre_feature, fresh, is_scale_in=True) == "range"
    # A row from before the column was added at all behaves the same way.
    assert pinned_structural_ceiling({}, fresh, is_scale_in=True) is True
    # False is a real verdict, not a hole: it must survive the round trip.
    assert pinned_structural_ceiling(
        {"structural_ceiling": 0}, _Decision(structural_ceiling=True),
        is_scale_in=True,
    ) is False


def test_a_fresh_entry_never_reads_a_prior_row():
    from src.execution.entry_evidence import pinned_structural_ceiling
    assert pinned_structural_ceiling(
        {"structural_ceiling": 1}, _Decision(structural_ceiling=False),
        is_scale_in=False,
    ) is False
