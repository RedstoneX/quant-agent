"""Board item 222 — which single-name limit binds must stay READABLE.

Three limits used to claim to bound how much of one name the desk may hold.
Only two are independent: `alloc_cap_by_risk` is the 5% per-position risk
envelope re-expressed in notional units, so it cannot bind an order the
envelope would not. The two that remain are in the same unit (percent of
total account equity, raw notional) against the same denominator
(`total_value`) and cross exactly once, at
`risk_budget_pct / max_position_pct x 100` percent of entry price.

These tests fail if that structure is changed into one where which limit
binds is again unreadable: a third independent clamp in the sizing path, a
config move that invalidates the written sentence, or a ledger row that
stops stating the same answer.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
import yaml

from src.portfolio_constructor import (
    SINGLE_NAME_BINDING_SENTENCE,
    single_name_crossover_stop_pct,
)

REPO = pathlib.Path(__file__).resolve().parents[1]
# The sizing bodies moved VERBATIM to order_build/ (2026-10-02), one file per leg; orders.py keeps thin shims.
CONSTRUCTOR = REPO / "src" / "portfolio_constructor" / "order_build"
SETTINGS = REPO / "config" / "settings.yaml"
LEDGER = REPO / "config" / "number_ledger.yaml"

#: The ONLY upper-bound clamps the single-name sizing path may apply to
#: `allocation_pct` before the sector dial. `alloc_cap_by_risk` is the risk
#: envelope in notional units; `name_headroom_pct` is the notional ceiling
#: less what is already held. A third name here means a third limit whose
#: ordering against these two nobody has written down — exactly the item 222
#: defect. Adding one requires extending `SINGLE_NAME_BINDING_SENTENCE`
#: first, which is why this list is hard-coded rather than derived.
ALLOWED_SIZE_CLAMPS = {"alloc_cap_by_risk", "name_headroom_pct"}

SIZING_FUNCTIONS = ("_build_buy", "_build_short")


def _risk_settings() -> dict:
    return yaml.safe_load(SETTINGS.read_text())["risk"]


def test_no_third_independent_clamp_in_the_sizing_path():
    """A new upper bound on `allocation_pct` makes the ordering unreadable."""
    tree = ast.Module(
        body=[n for p in sorted(CONSTRUCTOR.glob("*_entry.py")) for n in ast.parse(p.read_text()).body], type_ignores=[]
    )
    seen = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in SIZING_FUNCTIONS):
            continue
        names = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            # A clamp may be guarded -- `if cap is not None and
            # allocation_pct > cap:` -- so unwrap a boolean operator and
            # look at every comparison inside it. Reading only a bare
            # Compare made this check blind the moment a guard was added,
            # which is how it missed the risk cap after that cap moved into
            # a shared helper (2026-10-01).
            tests = list(inner.test.values) if isinstance(inner.test, ast.BoolOp) else [inner.test]
            for test in tests:
                if not isinstance(test, ast.Compare):
                    continue
                if not (isinstance(test.left, ast.Name) and test.left.id == "allocation_pct"):
                    continue
                if not isinstance(test.ops[0], ast.Gt):
                    continue
                right = test.comparators[0]
                if isinstance(right, ast.Name):
                    names.add(right.id)
                elif isinstance(right, ast.Constant):
                    continue  # `> 0` style sanity guards are not limits
                else:
                    names.add(ast.unparse(right))
        seen[node.name] = names

    assert set(seen) == set(SIZING_FUNCTIONS), seen
    for fn, names in seen.items():
        assert names == ALLOWED_SIZE_CLAMPS, (
            f"{fn} clamps allocation_pct against {sorted(names)}; item 222 "
            f"allows only {sorted(ALLOWED_SIZE_CLAMPS)}. A new clamp is a new "
            "single-name limit — state where it sits against the other two in "
            "SINGLE_NAME_BINDING_SENTENCE and add it here, or it is unreadable."
        )


def test_the_risk_cap_is_the_envelope_in_notional_units():
    """`alloc_cap_by_risk` must stay a unit conversion, not a new bound."""
    source = "\n".join(p.read_text() for p in sorted(CONSTRUCTOR.glob("*_entry.py")))
    tree = ast.Module(
        body=[n for p in sorted(CONSTRUCTOR.glob("*_entry.py")) for n in ast.parse(p.read_text()).body], type_ignores=[]
    )
    found = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in SIZING_FUNCTIONS):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "alloc_cap_by_risk" for t in inner.targets
            ):
                found += 1
                expr = ast.unparse(inner.value)
                # The arithmetic moved into one shared helper on 2026-10-01
                # so the preview and the constructor could not drift apart.
                # The invariant is unchanged: the cap is built from the
                # ratified envelope and the trade's own risk per share, and
                # it never reaches for the notional ceiling.
                assert "risk_budget_allocation_pct" in expr, expr
                assert "total_value" in expr, expr
                assert "risk_budget_pct" in expr, expr
                assert "max_position_pct" not in expr, (
                    "the risk cap must not reach for the notional ceiling — "
                    "that would fuse the two bounds and hide which one binds"
                )
    assert found == len(SIZING_FUNCTIONS), found

    # And the shared helper really is the envelope in notional units, built
    # from the ratified percentage and the trade's own risk per share.
    helper = (pathlib.Path(__file__).resolve().parents[1] / "src/risk/constants.py").read_text()
    body = helper.split("def risk_budget_allocation_pct", 1)[1]
    body = body[: body.find("\ndef ")] if "\ndef " in body else body
    assert "risk_dollars_allowed" in body
    assert "risk_per_share" in body
    assert "max_position_pct" not in body, (
        "the shared allocation helper must not reach for the notional "
        "ceiling — that would fuse the two bounds inside one function"
    )


@pytest.mark.parametrize("stop_pct,expect", [(2.0, "ceiling"), (20.0, "envelope")])
def test_the_two_bounds_cross_exactly_once(stop_pct, expect):
    risk = _risk_settings()
    budget = float(risk["max_position_risk_pct"])
    ceiling = float(risk["max_position_pct"])
    crossover = single_name_crossover_stop_pct(budget, ceiling)
    # The risk envelope expressed in notional, at this stop distance.
    risk_notional = budget / (stop_pct / 100.0)
    binding = "envelope" if risk_notional < ceiling else "ceiling"
    assert binding == expect
    assert (stop_pct < crossover) == (binding == "ceiling")


def test_the_written_sentence_still_matches_the_live_config():
    risk = _risk_settings()
    budget = float(risk["max_position_risk_pct"])
    ceiling = float(risk["max_position_pct"])
    crossover = single_name_crossover_stop_pct(budget, ceiling)

    assert f"{ceiling:.0f}% of total account equity" in SINGLE_NAME_BINDING_SENTENCE
    assert f"{budget:.0f}% per-position risk envelope" in SINGLE_NAME_BINDING_SENTENCE
    assert f"{crossover:.2f}% of entry price" in SINGLE_NAME_BINDING_SENTENCE, (
        "the config moved but the one sentence answering 'how much of one "
        f"name?' still says something else — crossover is now {crossover:.2f}%"
    )
    # The constructor default must not drift from the setting either, or the
    # sentence is true of one of them and false of the other.
    from src.portfolio_constructor import ConstructorConfig

    assert ConstructorConfig.max_position_pct == ceiling
    assert ConstructorConfig.risk_budget_pct == budget


def test_all_three_ledger_rows_state_the_same_answer():
    rows = yaml.safe_load(LEDGER.read_text())
    entries = rows["numbers"] if isinstance(rows, dict) and "numbers" in rows else rows
    if isinstance(entries, dict):
        entries = [dict(v, id=k) for k, v in entries.items()]
    by_id = {e["id"]: e for e in entries if isinstance(e, dict) and "id" in e}

    risk = _risk_settings()
    ceiling = float(risk["max_position_pct"])
    crossover = single_name_crossover_stop_pct(float(risk["max_position_risk_pct"]), ceiling)
    answer_marks = (f"{ceiling:.0f}% of total account equity", f"{crossover:.2f}%")

    for ident in (
        "src.config.RiskConfig.max_position_risk_pct",
        "src.portfolio_constructor.config.ConstructorConfig.risk_budget_pct",
    ):
        text = " ".join(str(v) for v in by_id[ident].values())
        for mark in answer_marks:
            assert mark in text, (
                f"{ident} no longer states the item 222 answer ({mark!r}); the "
                "three rows must agree or which limit binds is unreadable again"
            )

    ceiling_row = " ".join(
        str(v) for v in by_id["src.portfolio_constructor.config.ConstructorConfig.max_position_pct"].values()
    )
    assert "item 222" in ceiling_row.lower()
    assert f"{crossover:.2f}%" in ceiling_row
