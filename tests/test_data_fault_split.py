"""A data fault is not a trade refusal (2026-09-12).

`derive_structural_target` used to decline for six reasons under ONE field,
and the constructor logged every one as "rejected — no target could be
computed". Two of those reasons (no ATR, no usable bars) — plus no price
and no analysis at all, found on the same path — are not judgements about a
trade. A listed instrument always has a price, volatility and bars; when
the desk holds none of them the desk's own data acquisition failed. Filing
that as a rejected trade meant a dead feed and a trade that failed its
rules were the same class of outcome in the record, so nobody could tell
them apart and nobody knew how often either happened.

These tests pin the DecisionStage half of the split — the durable row and
the owner alert. The derivation and constructor halves are in
tests/test_target_derivation.py. The safety posture is unchanged and
asserted throughout: an unmeasurable symbol is NOT traded.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.data.levels import FAULT_NO_STRUCTURE, FAULT_NO_VOLATILITY
from src.models import TargetPosition
from src.pipeline_context import RunContext
from src.pipeline_stages import (
    _alert_unmeasurable_symbols,
    _record_constructor_drops,
)
from src.portfolio_constructor import PortfolioConstructor


def _pipeline_with_constructor():
    pipeline = MagicMock()
    pipeline.db = MagicMock()
    pipeline.portfolio_constructor = PortfolioConstructor()
    return pipeline


def _ctx():
    ctx = RunContext.start("morning")
    ctx.decision_id = "d1"
    return ctx


def _decision(targets: list[str], dropped: list[str]) -> SimpleNamespace:
    """Only `constructor_dropped` is read by the helper under test; a
    namespace keeps the fixture honest about that."""
    return SimpleNamespace(
        targets=[
            TargetPosition(symbol=s, target_weight_pct=5.0, conviction="high",
                           thesis="t")
            for s in targets
        ],
        constructor_dropped=list(dropped),
    )


def _events(pipeline) -> list[dict]:
    out = []
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        kw = call.kwargs
        if kw.get("kind") != "pipeline_event":
            continue
        payload = json.loads(kw["evidence_json"])
        payload["_symbol"] = kw.get("symbol")
        out.append(payload)
    return out


def test_a_faulted_target_is_filed_as_data_fault_never_constructor_dropped():
    """The row that feeds the census. If this ever becomes
    `constructor_dropped`, the desk's own "why didn't we trade" statistic is
    counting a broken feed as a judged trade again."""
    pipeline = _pipeline_with_constructor()
    pipeline.portfolio_constructor._note_data_fault(
        "NVDA", "long", FAULT_NO_VOLATILITY, "DATA FAULT: no ATR",
    )
    # A genuine refusal on another symbol, captured the usual way.
    pipeline.portfolio_constructor.last_drop_reasons = {
        "NVDA": "Constructor: BUY NVDA skipped — UNMEASURABLE ...",
        "AMD": "Constructor: BUY AMD rejected — reward:risk below floor",
    }

    faults = _record_constructor_drops(
        pipeline, _ctx(), _decision(["NVDA", "AMD"], ["NVDA", "AMD"]),
    )

    by_symbol = {e["_symbol"]: e for e in _events(pipeline)}
    assert by_symbol["NVDA"]["reason"] == "data_fault"
    assert by_symbol["NVDA"]["outcome"] == "unmeasurable"
    assert by_symbol["NVDA"]["fault"] == FAULT_NO_VOLATILITY
    assert by_symbol["NVDA"]["targeted"] is True
    assert by_symbol["AMD"]["reason"] == "constructor_dropped"
    assert by_symbol["AMD"]["outcome"] == "blocked"
    assert "rejected" in by_symbol["AMD"]["detail"]
    # Returned for the alert, and drained off the instance.
    assert set(faults) == {"NVDA"}
    assert pipeline.portfolio_constructor.last_data_faults == {}


def test_a_fault_on_a_symbol_the_pm_never_targeted_still_leaves_a_row():
    """The quietest failure: the eligibility preview found the symbol
    unmeasurable, so the PM never proposed it and the constructor never
    dropped it. Before this it left nothing at all."""
    pipeline = _pipeline_with_constructor()
    pipeline.portfolio_constructor._note_data_fault(
        "XYZ", "long", FAULT_NO_STRUCTURE, "DATA FAULT: coverage=no_bars",
    )

    faults = _record_constructor_drops(pipeline, _ctx(), _decision([], []))

    events = _events(pipeline)
    assert len(events) == 1
    assert events[0]["_symbol"] == "XYZ"
    assert events[0]["reason"] == "data_fault"
    assert events[0]["targeted"] is False
    assert set(faults) == {"XYZ"}


def test_a_data_fault_pages_the_owner_once_listing_every_symbol():
    """A symbol silently becoming unanalysable is exactly the failure that
    hides. One standalone message per session, every symbol named, the
    fault code and the fail-closed statement in the text."""
    faults = {
        "NVDA": {"fault": FAULT_NO_VOLATILITY, "detail": "no ATR", "direction": "long"},
        "TSLA": {"fault": FAULT_NO_STRUCTURE, "detail": "coverage=no_bars", "direction": "short"},
    }
    with patch("src.notifier.send_owner_alert") as alert:
        _alert_unmeasurable_symbols(faults)

    assert alert.call_count == 1
    text = alert.call_args.args[0]
    assert "DATA FAULT" in text
    assert "UNMEASURABLE" in text
    assert "NVDA" in text and "TSLA" in text
    assert FAULT_NO_VOLATILITY in text and FAULT_NO_STRUCTURE in text
    assert "none of these was traded" in text
    assert "NOT as trades the desk rejected" in text
    assert sorted(alert.call_args.kwargs["symbols"]) == ["NVDA", "TSLA"]


def test_no_fault_means_no_page():
    """A genuine refusal must never page as a data fault — or the owner
    learns to swipe the alert away."""
    with patch("src.notifier.send_owner_alert") as alert:
        _alert_unmeasurable_symbols({})
    alert.assert_not_called()


def test_an_alert_failure_cannot_break_the_decision_path():
    faults = {"NVDA": {"fault": FAULT_NO_VOLATILITY, "detail": "d", "direction": "long"}}
    with patch("src.notifier.send_owner_alert", side_effect=RuntimeError("down")):
        _alert_unmeasurable_symbols(faults)      # must not raise


def test_a_faulted_symbol_is_still_not_traded_end_to_end():
    """The posture is unchanged: classification, record and alert moved;
    whether the trade happens did not. Through the real constructor."""
    from tests.test_target_derivation import _analysis

    pipeline = _pipeline_with_constructor()
    analysis = _analysis(
        symbol="NVDA", rating="buy", entry=100.0, stop=95.0,
        model_target=130.0, levels=[95.0, 130.0], atr=1.4, horizon=30,
    )
    analysis.atr_14 = None
    decisions = pipeline.portfolio_constructor.construct_orders(
        targets=[TargetPosition(symbol="NVDA", target_weight_pct=8.0,
                                conviction="high", thesis="t")],
        positions=[], analyses=[analysis], total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert decisions == []

    with patch("src.notifier.send_owner_alert") as alert:
        faults = _record_constructor_drops(
            pipeline, _ctx(), _decision(["NVDA"], ["NVDA"]),
        )
        _alert_unmeasurable_symbols(faults)

    events = _events(pipeline)
    assert [e["reason"] for e in events] == ["data_fault"]
    assert alert.call_count == 1
