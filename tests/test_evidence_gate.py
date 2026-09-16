"""docs/WORK.md item 20 — do not decide on evidence that never arrived.

Two halves:
  1. the classifier itself, including the vocabulary-completeness test that
     stops the gate gaining bite by accident when a seat gains a new status;
  2. the wiring in `TradingPipeline.run_morning`, including the two
     properties that make this safe to ship on a desk that restarts soon —
     it refuses BEFORE the Portfolio Manager call, and it never refuses on a
     status it does not recognise.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import evidence_gate
from src.pipeline import TradingPipeline

REPO = Path(__file__).resolve().parents[1]


# ---------- the classifier ----------

def test_a_lost_answer_refuses_the_decision():
    v = evidence_gate.evaluate({"macro": "ok", "news": "parse_error"})
    assert v.skip is True
    assert v.lost == ["news"]
    assert "news=parse_error" in v.reason


@pytest.mark.parametrize("status", ["failed", "parse_error", "provider_error",
                                    "truncated", "content_missing"])
def test_every_lost_status_refuses(status):
    assert evidence_gate.evaluate({"earnings": status}).skip is True


@pytest.mark.parametrize("status", ["empty", "release_overdue", "not_run_intraday"])
def test_a_seat_with_nothing_to_report_does_not_refuse(status):
    """The whole reason this gate needs no threshold: "no Form 4 filings
    today" is an answer, not a gap. `not_run_intraday` is the intentional
    skip — this tick chose not to re-fetch the seat — not a lost answer."""
    v = evidence_gate.evaluate({"smart_money": status})
    assert v.skip is False
    assert v.nothing_to_report == ["smart_money"]


@pytest.mark.parametrize("status", ["carry_forward_empty", "carry_forward_failed"])
def test_empty_or_failed_carry_forward_refuses(status):
    """The other meaning that used to hide under `not_run_intraday`:
    this morning's seat never produced a usable today-dated answer, or
    the lookup itself failed. That is a lost answer. Deciding on it is
    fabricating the missing seat."""
    v = evidence_gate.evaluate({"macro": status, "earnings": "not_run_intraday"})
    assert v.skip is True
    assert v.lost == ["macro"]
    assert v.nothing_to_report == ["earnings"]


def test_intraday_status_split_distinguishes_skip_from_miss():
    """The two meanings that used to share one word."""
    cat = evidence_gate.STATUS_CATEGORY
    assert cat["not_run_intraday"] == evidence_gate.CATEGORY_NOTHING_TO_REPORT
    assert cat["carry_forward_empty"] == evidence_gate.CATEGORY_LOST
    assert cat["carry_forward_failed"] == evidence_gate.CATEGORY_LOST
    assert cat["carried_from_morning"] == evidence_gate.CATEGORY_REPORTED


@pytest.mark.parametrize("status", ["ok", "partial", "low_confidence",
                                    "symbol_dropped", "degraded",
                                    "figures_contradicted",
                                    "carried_from_morning"])
def test_a_reported_answer_does_not_refuse(status):
    """Thin, mixed, self-doubting and even provably-wrong answers all ARE
    answers. Judging how much partial is too much is the counting question
    the owner reserved to himself — this gate must not smuggle it in."""
    assert evidence_gate.evaluate({"earnings": status}).skip is False


def test_an_unknown_status_never_refuses():
    """A refusal gate must not gain bite because some other seat gained a
    word. It passes, and says so at ERROR."""
    v = evidence_gate.evaluate({"macro": "brand_new_state"})
    assert v.skip is False
    assert v.unclassified == ["macro"]


def test_evaluate_never_raises_on_junk():
    for junk in (None, [], "ok", {"macro": None}, {1: 2}):
        evidence_gate.evaluate(junk)


def test_no_threshold_number_lives_in_this_module():
    """docs/WORK.md, PERMANENT: no arbitrary numbers. The gate's whole claim
    is that it needs no coverage count — so no bare integer/float literal
    may appear in its executable code."""
    import ast
    tree = ast.parse((REPO / "src" / "evidence_gate.py").read_text())
    numbers = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ]
    assert numbers == [], f"numeric literal(s) in the gate: {numbers}"


def test_every_status_the_codebase_writes_is_classified():
    """MECHANICAL ENFORCEMENT, not a promise to remember. Any new
    `data_status[...] = "x"` anywhere in src/ must be classified in
    STATUS_CATEGORY deliberately, in the same diff — otherwise the gate
    silently treats it as a reported answer forever."""
    written: set[str] = set()
    pattern = re.compile(r"""data_status\[[^\]]+\]\s*=\s*\(?\s*["']([a-z_]+)["']""")
    ternary = re.compile(
        r"""data_status\[[^\]]+\]\s*=\s*["']([a-z_]+)["']\s+if\s+.*?\s+else\s+["']([a-z_]+)["']"""
    )
    for path in (REPO / "src").rglob("*.py"):
        if path.name == "evidence_gate.py":
            continue
        text = path.read_text()
        for m in pattern.finditer(text):
            written.add(m.group(1))
        for m in ternary.finditer(text):
            written.update(m.groups())
    # Two writers do not use subscript assignment and are scanned directly:
    # the intraday dict is a literal, and the earnings status is whatever
    # `_classify_earnings_status` returns.
    written.update({
        "carried_from_morning", "not_run_intraday",
        "carry_forward_empty", "carry_forward_failed",
    })
    import ast
    stages = ast.parse((REPO / "src" / "pipeline_stages.py").read_text())
    for node in ast.walk(stages):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "_classify_earnings_status"):
            written.update(
                r.value.value for r in ast.walk(node)
                if isinstance(r, ast.Return) and isinstance(r.value, ast.Constant)
                and isinstance(r.value.value, str)
            )
    assert {"ok", "failed", "parse_error", "content_missing"} <= written, (
        "the scan found almost nothing — the assignment pattern has drifted "
        "and this test is no longer enforcing anything"
    )
    missing = sorted(written - set(evidence_gate.STATUS_CATEGORY))
    assert not missing, (
        f"unclassified data_status value(s): {missing} — classify them in "
        f"src/evidence_gate.py as reported / nothing_to_report / lost"
    )


# ---------- the wiring ----------

def _pipeline(data_status: dict):
    p = TradingPipeline.__new__(TradingPipeline)
    p._is_trading_day = lambda: True
    p._drain_pending_protection_restores = MagicMock()
    p._reconcile_orphan_pending_submits = MagicMock()
    p._reconcile_stop_coverage = MagicMock(return_value=[])
    p._reconcile_fills = MagicMock()
    p._force_delever = MagicMock(return_value=[])
    p.db = MagicMock()
    p.broker = MagicMock()
    p.broker.get_account.return_value = {
        "cash": 50_000.0, "portfolio_value": 100_000.0, "last_equity": 100_000.0,
    }
    p.broker.get_positions.return_value = []
    p.risk_engine = MagicMock()
    p.risk_engine.check_daily_loss.return_value = None
    p.morning_research_stage = MagicMock()

    def _research(ctx):
        analysis = MagicMock()
        analysis.symbol = "NVDA"
        ctx.analyses = [analysis]
        ctx.data_status = dict(data_status)

    p.morning_research_stage.run.side_effect = _research
    p.decision_stage = MagicMock()
    p._decision_stage = MagicMock()
    p._check_late_breach_and_emergency_liquidate = MagicMock(return_value=None)
    return p


def _run(p):
    from src import decision_checkpoint as dc
    with patch.object(dc, "load", return_value=None), \
         patch.object(dc, "write", return_value=None), \
         patch.object(dc, "write_status") as ws, \
         patch.object(dc, "mark_consumed"), \
         patch("src.notifier.send_owner_alert", return_value=True) as alert:
        return p.run_morning(), ws, alert


def test_morning_skips_before_paying_for_the_portfolio_manager():
    """The saving IS the point: a run on absent evidence must not spend the
    seat that is 93% of the bill."""
    p = _pipeline({"macro": "ok", "news": "failed", "tech": "ok"})
    result, write_status, alert = _run(p)
    assert result["status"] == "evidence_gate_skip"
    assert result["lost_seats"] == ["news"]
    assert result["orders"] == []
    p._decision_stage.assert_not_called()
    write_status.assert_called_once_with("morning", "evidence_gate_skip")
    alert.assert_called_once()
    assert "DECISION SKIPPED" in alert.call_args[0][0]


def test_the_skip_carries_data_status_so_the_standalone_alert_fires():
    """Loudness path 2: main.py's finally block pages off `data_status` in
    the result, independent of the session message's noise policy."""
    from src.notifier import maybe_alert_data_quality
    p = _pipeline({"macro": "failed", "tech": "ok"})
    result, _, _ = _run(p)
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        assert maybe_alert_data_quality(result, mode="morning") is True
    assert "macro=failed" in alert.call_args[0][0]


def test_the_skip_is_not_the_white_nothing_happened_bucket():
    """Loudness path 3. Retired item 11 was a silent zero-proposal day."""
    from src.notifier import _status_emoji
    assert _status_emoji("evidence_gate_skip") == _status_emoji("hard_risk_block")
    assert _status_emoji("evidence_gate_skip") != _status_emoji("no_data")


def test_the_skip_records_a_durable_machine_readable_reason_per_symbol():
    p = _pipeline({"news": "parse_error"})
    _run(p)
    rows = [c.kwargs for c in p.db.insert_specialist_evidence.call_args_list
            if c.kwargs.get("kind") == "pipeline_event"]
    per_symbol = [r for r in rows if r.get("symbol") == "NVDA"]
    assert per_symbol, "no per-symbol row explaining why NVDA was not decided"
    assert "evidence_gate" in per_symbol[-1]["evidence_json"]
    assert "parse_error" in per_symbol[-1]["evidence_json"]
    run_rows = [r for r in rows if r.get("symbol") is None]
    assert any("evidence_coverage" in r["evidence_json"] for r in run_rows)


def test_the_skip_emits_no_target_at_all():
    """A 0% target is read by this system as "sell it". A refusal must DROP
    the decision, never zero one — so nothing target-shaped may exist."""
    p = _pipeline({"news": "failed"})
    result, _, _ = _run(p)
    assert result["orders"] == []
    assert "targets" not in result
    p._decision_stage.assert_not_called()


def test_a_clean_run_is_untouched():
    p = _pipeline({"macro": "ok", "news": "low_confidence",
                   "tech": "partial", "smart_money": "empty"})
    result, _, alert = _run(p)
    assert result["status"] != "evidence_gate_skip"
    p._decision_stage.assert_called_once()
    alert.assert_not_called()


def test_a_gate_crash_lets_the_run_proceed():
    """A gate that can stop the desk trading must not stop it by crashing."""
    p = _pipeline({"macro": "ok"})
    with patch.object(evidence_gate, "evaluate", side_effect=RuntimeError("boom")):
        result, _, _ = _run(p)
    assert result["status"] != "evidence_gate_skip"
    p._decision_stage.assert_called_once()
