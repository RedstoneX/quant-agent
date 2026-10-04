"""One persisted row per no-ATR structural stop placement — and nothing stored.

`structural_stop_buffer_pct` is a flat 0.5% nobody measured. The ledger's
route for it asks for the distribution of what that buffer produced at
placement, which an in-memory tally can neither survive nor be re-read from.
These tests pin the four states that distribution depends on: a placement off
a verified level, a placement off the prior bar, a run that found no usable
structure, and the branch never being reached at all (NO row, which is not
the same fact as a zero). The last test pins that a recording failure cannot
move a stop.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from src.portfolio_constructor import PortfolioConstructor
from src.portfolio_constructor.no_atr_buffer_rows import NO_ATR_BUFFER_KIND


class _FakeDB:
    """Captures evidence rows. Holds rows, never a total."""

    def __init__(self):
        self.rows = []

    def insert_specialist_evidence(self, **kwargs):
        self.rows.append(kwargs)
        return len(self.rows)


def _analysis(**overrides):
    base = dict(
        symbol="ACME", atr_14=None, setup_type="breakout", signal_bar_low=None,
        signal_bar_high=None, computed_levels=[], computed_level_touches={},
        expected_horizon_sessions=20, reference_target=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _buffer_rows(db):
    return [
        json.loads(r["evidence_json"]) for r in db.rows
        if r.get("kind") == NO_ATR_BUFFER_KIND
    ]


def _widen(constructor, analysis):
    return constructor._widen_stop_past_noise(
        "ACME", analysis, 100.0, None, direction="long", target_price=None,
    )


def test_a_level_placement_persists_one_row_with_the_measured_halfwidth():
    db = _FakeDB()
    constructor = PortfolioConstructor(db=db)
    analysis = _analysis(
        computed_levels=[95.0], computed_level_touches={95.0: 5},
        computed_level_zones={95.0: (94.0, 96.0)},
        signal_bar_low=99.0, signal_bar_high=101.0,
    )
    stop = _widen(constructor, analysis)
    rows = _buffer_rows(db)
    assert stop is not None
    assert len(rows) == 1
    assert rows[0]["outcome"] == "level"
    assert rows[0]["level"] == 95.0
    assert rows[0]["level_zone_halfwidth"] == 1.0
    assert rows[0]["level_zone_halfwidth_measured"] is True
    assert rows[0]["buffer_pct"] > 0
    assert rows[0]["level_touches"] == 5


def test_a_missing_zone_is_recorded_as_unmeasured_not_as_zero():
    db = _FakeDB()
    constructor = PortfolioConstructor(db=db)
    analysis = _analysis(
        computed_levels=[95.0], computed_level_touches={95.0: 5},
        signal_bar_low=99.0, signal_bar_high=101.0,
    )
    assert _widen(constructor, analysis) is not None
    row = _buffer_rows(db)[0]
    assert row["level_zone_halfwidth"] is None
    assert row["level_zone_halfwidth_measured"] is False


def test_the_prior_bar_tier_is_its_own_outcome():
    db = _FakeDB()
    constructor = PortfolioConstructor(db=db)
    analysis = _analysis(
        computed_levels=[95.0], computed_level_touches={95.0: 2},
        signal_bar_low=97.0,
    )
    assert _widen(constructor, analysis) is not None
    rows = _buffer_rows(db)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "prior_bar"
    assert rows[0]["level"] == 97.0


def test_a_run_that_found_no_structure_still_writes_its_own_row():
    db = _FakeDB()
    constructor = PortfolioConstructor(db=db)
    _widen(constructor, _analysis())
    rows = _buffer_rows(db)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "none"
    assert rows[0]["level"] is None


def test_a_site_never_reached_writes_no_row_at_all():
    db = _FakeDB()
    constructor = PortfolioConstructor(db=db)
    analysis = _analysis(
        atr_14=2.0, computed_levels=[95.0], computed_level_touches={95.0: 5},
    )
    _widen(constructor, analysis)
    assert _buffer_rows(db) == []


def test_a_recording_failure_leaves_the_stop_exactly_where_it_was():
    good = _FakeDB()
    analysis_args = dict(
        computed_levels=[95.0], computed_level_touches={95.0: 5},
        signal_bar_low=99.0, signal_bar_high=101.0,
    )
    expected = _widen(PortfolioConstructor(db=good), _analysis(**analysis_args))

    class _Exploding:
        def insert_specialist_evidence(self, **kwargs):
            raise RuntimeError("disk on fire")

    broken = PortfolioConstructor(db=_Exploding())
    assert _widen(broken, _analysis(**analysis_args)) == expected

    worse = PortfolioConstructor(db=good)

    def _boom():
        raise RuntimeError("no handle")

    worse._stop_rules._read_db = _boom
    assert _widen(worse, _analysis(**analysis_args)) == expected
