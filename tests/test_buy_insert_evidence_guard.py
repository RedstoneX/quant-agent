"""Guard (board item 218): every site that can write an ENTRY trade row must
supply the three stop-floor evidence values.

Background. `structural_ceiling`, `entry_atr` and `stop_basis` were measured
NULL on all 80 production trade rows (2026-10-01) although the kwargs are
passed where the entry row is written. The fact the desk lacked was WHICH
code writes its buys. Enumerated from source: exactly one call site writes a
BUY/SHORT row (`src/stage_execution.py`, the write-ahead `pending_submit`
insert); every other `insert_trade` call writes an exit/hold/cover row, and
the exit reconciler's raw INSERT writes exits only.

An entry site is recognised mechanically: its `action=` is a BUY/SHORT
literal or `<x>.action` taken straight off a TradeDecision, or it passes any
of the entry-pinned kwargs. Such a call must pass all three kwargs, and none
may be a literal (a constant would be a fabricated default, strictly worse
than a NULL). The baseline below is SHRINK-ONLY and starts empty.
"""

from __future__ import annotations

import ast
import pathlib

from src.storage.db import Database

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
EVIDENCE = ("structural_ceiling", "entry_atr", "stop_basis")
ENTRY_PINNED = frozenset(EVIDENCE) | {
    "setup_type",
    "stop_level_basis",
    "expected_horizon_sessions",
    "conviction",
    "requested_risk_pct",
    "allocated_risk_pct",
}
# Sites allowed to lack evidence, as "relpath:line-independent key". Shrink only.
GRANDFATHERED: frozenset[str] = frozenset()


def _is_entry(call: ast.Call) -> bool:
    kws = {k.arg: k.value for k in call.keywords if k.arg}
    if kws.keys() & ENTRY_PINNED:
        return True
    act = kws.get("action")
    if isinstance(act, ast.Constant):
        return act.value in ("BUY", "SHORT")
    return isinstance(act, ast.Attribute) and act.attr == "action"


def violations(root: pathlib.Path = SRC) -> list[str]:
    out = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "insert_trade"
            ):
                continue
            if any(k.arg is None for k in node.keywords) or not _is_entry(node):
                continue
            kws = {k.arg: k.value for k in node.keywords if k.arg}
            rel = f"{path.relative_to(root.parent)}"
            for name in EVIDENCE:
                if name not in kws:
                    out.append(f"{rel}:{node.lineno} entry insert_trade omits {name}")
                elif isinstance(kws[name], ast.Constant):
                    out.append(f"{rel}:{node.lineno} {name} is a literal, not derived")
    return [v for v in out if v.split(":")[0] not in GRANDFATHERED]


def test_no_entry_insert_site_omits_the_evidence_values():
    assert violations() == [], (
        "an entry trade-insert site must pass structural_ceiling, entry_atr "
        "and stop_basis, each derived (never a literal default): " + "; ".join(violations())
    )


def test_the_guard_sees_the_one_real_buy_site():
    """A guard that finds no entry site proves nothing."""
    sites = [
        p
        for p in SRC.rglob("*.py")
        for n in ast.walk(ast.parse(p.read_text()))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "insert_trade"
        and _is_entry(n)
    ]
    assert [p.name for p in sites] == ["entry_record.py"]


def test_the_guard_goes_red_on_a_broken_site(tmp_path):
    (tmp_path / "src").mkdir()
    bad = tmp_path / "src" / "x.py"
    bad.write_text(
        "db.insert_trade(symbol='A', action='BUY', qty=1, price=1, reasoning='r',"
        " run_id='x', structural_ceiling=None, entry_atr=1.5)\n"
        "db.insert_trade(symbol='A', action=d.action, stop_basis='x',"
        " structural_ceiling=True, entry_atr=2.0)\n"
    )
    found = violations(tmp_path / "src")
    assert any("omits stop_basis" in v for v in found)
    assert any("structural_ceiling is a literal" in v for v in found)


def test_a_buy_written_through_the_ledger_carries_all_three(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    row = db.insert_trade(
        symbol="ZZZ",
        action="BUY",
        qty=1.0,
        price=10.0,
        reasoning="r",
        run_id="run",
        stop_loss=9.0,
        take_profit=12.0,
        fill_status="pending_submit",
        setup_type="range",
        structural_ceiling=True,
        entry_atr=0.7,
        stop_basis="structural",
    )
    got = db.conn.execute(
        "SELECT structural_ceiling, entry_atr, stop_basis FROM trades WHERE id=?",
        (row,),
    ).fetchone()
    assert tuple(got) == (1, 0.7, "structural")


def test_the_constructor_always_hands_the_insert_a_measured_ceiling_verdict():
    """Item 218, measured 2026-10-01: production `structural_ceiling` is NULL
    on all 41 entries. The constructor's own decision is a real bool for both
    a level-backed and an unbacked stop, so the NULL is not "never computed".
    (`stop_rule` IS legitimately None when no level backs the stop: it has one
    non-None code, so a NULL `stop_basis` means "not level-honoured", not lost.)
    """
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    import test_risk_based_sizing as t
    from src.portfolio_constructor import PortfolioConstructor

    for computed in ([t._TIGHT_STOP, t._UPPER_LEVEL], []):
        out = PortfolioConstructor().construct_orders(
            targets=[t._risk_target("MSFT", 1.0)],
            positions=[],
            analyses=[t._vol_analysis("MSFT", t._ENTRY, t._TIGHT_STOP, t._UPPER_LEVEL, atr=t._ATR, computed=computed)],
            total_value=t.EQUITY,
            price_map={"MSFT": t._ENTRY},
        )
        assert out[0].structural_ceiling is (len(computed) > 0)
