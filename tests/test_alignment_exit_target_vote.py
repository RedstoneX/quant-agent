"""The price target is ONE VOTE toward the alignment exit (owner, 2026-10-08).

"Can tip a close when other signals agree. But never sells alone." Once a
completed close has reached the current target since it took effect, the
give-back is measured from the FIRST lost mark instead of the last. No
lost mark: hold. One lost mark: unchanged. Two: the vote can tip a close.
"""
import sqlite3
import threading
import types
from datetime import date, timedelta

from src.exits.alignment_exit import AlignmentExit
from src.risk.alignment_exit import CODE_EXIT, CODE_HOLD, check_alignment_exit
from src.risk.target_vote import target_reached
from src.storage.target_revisions import build_target_revision_records

ATR = 1.0


def _dates(n: int) -> list[date]:
    return [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]


# LONG: 30 closes at 100, 19 at 120, then 105. MA20 = 119.25 (lost by
# 14.25 ATR), SMA50 = 107.7 (lost by 2.7 ATR, inside the 3.0 band). Today's
# rule reads from the LAST lost mark (SMA50) and holds; with the target
# (118) reached by the 120 closes, it reads from the FIRST (MA20) and exits.
LONG = [100.0] * 30 + [120.0] * 19 + [105.0]
SHORT = [100.0] * 30 + [80.0] * 19 + [95.0]


def _long(**kw):
    return check_alignment_exit(
        thesis_invalid_if="close below the MA20", closes=LONG, atr=ATR,
        bar_dates=_dates(len(LONG)), **kw,
    )


def _short(**kw):
    return check_alignment_exit(
        thesis_invalid_if="close above the MA20", closes=SHORT, atr=ATR,
        is_short=True, bar_dates=_dates(len(SHORT)), **kw,
    )


def test_today_holds_inside_the_band_from_the_last_lost_mark() -> None:
    for v in (_long(), _short()):
        assert v.status == "HOLD" and v.code == CODE_HOLD
        assert v.last_mark and "SMA50" in v.last_mark.source
        assert v.target_vote_applied is False
        assert "NOT APPLIED, no target to read" in v.target_vote
        assert v.target_vote in v.reason


def test_target_not_reached_is_todays_behaviour() -> None:
    for v in (_long(target=125.0, target_effective_date=date(2026, 1, 1)),
              _short(target=75.0, target_effective_date=date(2026, 1, 1))):
        assert v.status == "HOLD" and "SMA50" in v.last_mark.source
        assert v.target_vote_applied is False
        assert "not applied" in v.target_vote and "not reached" in v.target_vote


def test_reached_with_two_lost_marks_tips_the_close() -> None:
    for v in (_long(target=118.0, target_effective_date=date(2026, 1, 1),
                    target_version="entry record"),
              _short(target=82.0, target_effective_date=date(2026, 1, 1),
                     target_version="entry record")):
        assert v.status == "EXIT" and v.code == CODE_EXIT
        assert v.last_mark and "MA20" in v.last_mark.source
        assert v.target_vote_applied is True
        assert "APPLIED" in v.target_vote and "entry record" in v.target_vote
        assert "2026-01-01" in v.target_vote
        assert v.owner_reason and "voted with the chart" in v.owner_reason
        assert "first lost mark" in v.owner_reason


def test_reached_with_one_lost_mark_is_unchanged() -> None:
    """A single mark is both first and last, so the vote moves nothing."""
    base = dict(thesis_invalid_if=None, closes=[100.0] * 5 + [98.0], atr=ATR,
                broken_structural_level=99.0, bar_dates=_dates(6))
    today = check_alignment_exit(**base)
    voted = check_alignment_exit(
        **base, target=100.0, target_effective_date=date(2026, 1, 1),
    )
    assert today.status == voted.status == "HOLD"
    assert voted.target_vote_applied is True
    assert today.breach_atrs == voted.breach_atrs


def test_reached_with_no_lost_mark_never_sells_alone() -> None:
    closes = [100.0] * 30 + [120.0] * 20
    v = check_alignment_exit(
        thesis_invalid_if="close below the MA20", closes=closes, atr=ATR,
        bar_dates=_dates(50), target=110.0, target_effective_date=date(2026, 1, 1),
    )
    assert v.status == "HOLD" and v.code == CODE_HOLD
    assert v.target_vote_applied is True  # reached — and still a hold
    assert "APPLIED" in v.target_vote
    short = check_alignment_exit(
        thesis_invalid_if="close above the MA20", is_short=True, atr=ATR,
        closes=[100.0] * 30 + [80.0] * 20, bar_dates=_dates(50),
        target=90.0, target_effective_date=date(2026, 1, 1),
    )
    assert short.status == "HOLD" and short.target_vote_applied is True


def test_missing_target_is_loud_and_otherwise_today() -> None:
    today = _long()
    missing = _long(target=None, target_version="no target on the opening row")
    assert (today.status, today.last_mark, today.breach_atrs) == (
        missing.status, missing.last_mark, missing.breach_atrs,
    )
    assert "NOT APPLIED, no target to read (no target on the opening row)" in missing.reason
    undated = _long(target=118.0, target_effective_date=None)
    assert undated.status == "HOLD" and "no dates" in undated.target_vote


def test_revision_after_the_reach_resets_reached() -> None:
    """A target revised on the last session was never reached since it
    took effect, so the vote does not apply even though earlier closes
    stood beyond it."""
    last_day = _dates(len(LONG))[-1]
    v = _long(target=118.0, target_effective_date=last_day,
              target_version="applied revision")
    assert v.status == "HOLD" and v.target_vote_applied is False
    assert "not reached" in v.target_vote
    s = _short(target=82.0, target_effective_date=last_day)
    assert s.status == "HOLD" and s.target_vote_applied is False


def test_target_reached_is_close_only_on_or_after_the_effective_date() -> None:
    closes = [100.0, 118.0, 105.0]
    days = _dates(3)
    assert target_reached(closes, days, 118.0, days[0]) is True
    assert target_reached(closes, days, 118.0, days[1]) is True  # on the date
    assert target_reached(closes, days, 118.0, days[2]) is False
    assert target_reached(closes, days, 118.01, days[0]) is False  # no tolerance
    assert target_reached(closes, days, 100.0, days[0], is_short=True) is True
    assert target_reached(closes, days, 99.0, days[0], is_short=True) is False
    assert target_reached(closes, days, None, days[0]) is None
    assert target_reached(closes, [], 118.0, days[0]) is None
    assert target_reached(closes, days, 118.0, "2026-01-02 15:00:00") is True


# --- the caller: which target, and since when ---------------------------

def _exit(db) -> AlignmentExit:
    return AlignmentExit(
        alignment_exit_cached=None, structural_protection_for_holding=None,
        config=None, db=db, market=None,
    )


def _db(row, revisions=None, open_ts=None):
    return types.SimpleNamespace(
        get_symbol_last_buy=lambda sym, action="BUY": row,
        get_position_open_timestamp=lambda r: open_ts,
        get_target_revisions=lambda syms, **kw: {"AAA": revisions or []},
    )


def test_caller_dates_the_target_from_entry_or_the_latest_applied_revision() -> None:
    row = {"take_profit": 118.0, "timestamp": "2026-01-05 15:00:00"}
    t, eff, ver = _exit(_db(row))._target_for_holding(symbol="AAA", is_short=False)
    assert (t, eff) == (118.0, "2026-01-05") and "entry record" in ver
    # the position's OWN open (a scale-in does not move it) wins over the last buy
    t, eff, _ = _exit(_db(row, open_ts="2026-01-02T10:00:00"))._target_for_holding(
        symbol="AAA", is_short=False,
    )
    assert eff == "2026-01-02"
    revs = [
        {"timestamp": "2026-01-09 16:00:00", "applied": False, "new_price": 130.0},
        {"timestamp": "2026-01-08 16:00:00", "applied": True, "new_price": 118.0,
         "run_id": "r8", "code": "TRIGGER_X", "prior_price": 110.0},
        {"timestamp": "2026-01-01 16:00:00", "applied": True, "new_price": 90.0},
    ]
    t, eff, ver = _exit(_db(row, revs))._target_for_holding(symbol="AAA", is_short=False)
    assert (t, eff) == (118.0, "2026-01-08")
    assert "applied revision of 2026-01-08" in ver and "r8" in ver
    # only a revision filed BEFORE this open exists: it belonged to an earlier trade
    t, eff, ver = _exit(_db(row, revs[2:]))._target_for_holding(symbol="AAA", is_short=False)
    assert eff == "2026-01-05" and "entry record" in ver


def test_caller_reports_a_missing_or_unreadable_target_loudly() -> None:
    assert _exit(_db({"timestamp": "2026-01-05"}))._target_for_holding(
        symbol="AAA", is_short=False,
    ) == (None, None, "no target on the opening row")
    boom = types.SimpleNamespace(
        get_symbol_last_buy=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")),
    )
    t, eff, why = _exit(boom)._target_for_holding(symbol="AAA", is_short=True)
    assert t is None and eff is None and "target read failed" in why


def test_revision_limit_is_per_symbol() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE specialist_evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "run_id TEXT, decision_id TEXT, agent_name TEXT, kind TEXT, scope TEXT, "
        "symbol TEXT, evidence_json TEXT, "
        "timestamp TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    revs = build_target_revision_records(conn=conn, lock=threading.Lock())
    for i in range(3):
        revs.record_target_revision(run_id=f"a{i}", symbol="AAA", code=f"A{i}",
                                    seat="risk", evidence="e")
    revs.record_target_revision(run_id="b", symbol="BBB", code="B0", seat="risk",
                                evidence="e")
    out = revs.get_target_revisions(["AAA", "BBB", "aaa"], limit=2)
    assert len(out["AAA"]) == 2 and out["AAA"][0]["code"] == "A2"
    assert [r["code"] for r in out["BBB"]] == ["B0"]  # not crowded out
