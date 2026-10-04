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
    from src.portfolio_constructor.stops import StopRules

    with patch.object(
        StopRules, "shipped_stop_level_basis",
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
    from src.entry_evidence import (
        pinned_setup_type, pinned_structural_ceiling,
    )
    prior = {"setup_type": "breakout", "structural_ceiling": 1}
    fresh = _Decision(setup_type="range", structural_ceiling=False)
    assert pinned_setup_type(prior, fresh, is_scale_in=True) == "breakout"
    assert pinned_structural_ceiling(prior, fresh, is_scale_in=True) is True


def test_a_scale_in_onto_a_pre_feature_row_reports_the_hole_honestly():
    """A position whose entry verdict is genuinely MISSING stays missing.

    Backfilling it from a later top-up's reasoning would silently
    reclassify why the desk holds something it bought weeks ago on
    different reasoning. "We do not know why this was bought" is the
    true answer for those rows.
    """
    from src.entry_evidence import (
        pinned_setup_type, pinned_structural_ceiling,
    )
    pre_feature = {"setup_type": "range", "structural_ceiling": None}
    fresh = _Decision(setup_type="breakout", structural_ceiling=True)
    assert pinned_structural_ceiling(pre_feature, fresh, is_scale_in=True) is None
    # The field the position DOES hold is still carried, not reclassified.
    assert pinned_setup_type(pre_feature, fresh, is_scale_in=True) == "range"
    # A row from before the column was added at all behaves the same way.
    assert pinned_structural_ceiling({}, fresh, is_scale_in=True) is None
    assert pinned_setup_type({}, fresh, is_scale_in=True) is None
    # False is a real verdict, not a hole: it must survive the round trip.
    assert pinned_structural_ceiling(
        {"structural_ceiling": 0}, _Decision(structural_ceiling=True),
        is_scale_in=True,
    ) is False


def test_the_add_records_its_own_verdict_as_its_own_evidence():
    """The evidence the old fix threw away is kept — ALONGSIDE, attributed
    to the top-up, in a record nothing reads as a position's entry verdict."""
    import json

    from src.entry_evidence import scale_in_own_verdict

    pre_feature = {"setup_type": "range", "structural_ceiling": None}
    fresh = _Decision(setup_type="breakout", structural_ceiling=True)
    payload = json.loads(scale_in_own_verdict(pre_feature, fresh))
    assert payload["fields"]["structural_ceiling"] == {
        "add_verdict": True, "position_already_held_a_verdict": False,
    }
    assert payload["fields"]["setup_type"] == {
        "add_verdict": "breakout", "position_already_held_a_verdict": True,
    }
    # An add that computed nothing writes no evidence row at all.
    assert scale_in_own_verdict(pre_feature, _Decision()) is None


def test_a_fresh_entry_never_reads_a_prior_row():
    from src.entry_evidence import pinned_structural_ceiling
    assert pinned_structural_ceiling(
        {"structural_ceiling": 1}, _Decision(structural_ceiling=False),
        is_scale_in=False,
    ) is False


class _StubDb:
    """The two ledger calls the stage reaches for, and nothing else: the
    pinning seam must be buildable without the execution stage."""

    def __init__(self, prior=None, fail=False):
        self.prior, self.fail = prior, fail
        self.reads: list[tuple] = []
        self.evidence: list[dict] = []

    def get_symbol_last_buy(self, symbol, action="BUY"):
        self.reads.append((symbol, action))
        return self.prior

    def insert_specialist_evidence(self, **row):
        if self.fail:
            raise RuntimeError("ledger closed")
        self.evidence.append(row)
        return len(self.evidence)


class _Logged:
    def __init__(self):
        self.warnings: list[tuple] = []

    def warning(self, *a):
        self.warnings.append(a)


def test_resolve_entry_pins_reads_the_prior_row_only_on_a_scale_in():
    from src.entry_evidence import resolve_entry_pins

    prior = {"setup_type": "breakout", "structural_ceiling": 0}
    fresh = _Decision(setup_type="range", structural_ceiling=True)
    fresh.symbol = "TEST"
    db = _StubDb(prior=prior)
    row, st, sc = resolve_entry_pins(db, fresh, is_short=True, is_scale_in=True)
    assert (row, st, sc) == (prior, "breakout", False)
    assert db.reads == [("TEST", "SHORT")]  # a short add reads the SHORT open
    db = _StubDb(prior=prior)
    row, st, sc = resolve_entry_pins(db, fresh, is_short=False, is_scale_in=False)
    assert (row, st, sc) == (None, "range", True)
    assert db.reads == []  # a fresh entry never reads a prior row


def test_record_scale_in_own_verdict_files_alongside_and_never_raises():
    import json

    from src.entry_evidence import (
        SCALE_IN_EVIDENCE_AGENT, record_scale_in_own_verdict,
    )

    fresh = _Decision(setup_type="breakout", structural_ceiling=True)
    fresh.symbol = "TEST"
    prior = {"setup_type": "range", "structural_ceiling": None}
    db, log = _StubDb(), _Logged()
    record_scale_in_own_verdict(
        db, log, run_id="r1", decision_id="d1", decision=fresh,
        prior_row=prior, is_scale_in=True,
    )
    [row] = db.evidence
    assert (row["run_id"], row["decision_id"], row["symbol"]) == ("r1", "d1", "TEST")
    assert row["agent_name"] == SCALE_IN_EVIDENCE_AGENT and row["scope"] == "symbol"
    assert json.loads(row["evidence_json"])["fields"]["structural_ceiling"] == {
        "add_verdict": True, "position_already_held_a_verdict": False,
    }
    # Not a scale-in: nothing is filed.
    db = _StubDb()
    record_scale_in_own_verdict(
        db, log, run_id="r1", decision_id="d1", decision=fresh,
        prior_row=None, is_scale_in=False,
    )
    assert db.evidence == []
    # A ledger failure is logged, never raised into the buy path.
    record_scale_in_own_verdict(
        _StubDb(fail=True), log, run_id="r1", decision_id="d1",
        decision=fresh, prior_row=prior, is_scale_in=True,
    )
    assert len(log.warnings) == 1 and log.warnings[0][1] == "TEST"
