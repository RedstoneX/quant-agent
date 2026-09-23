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
    v = evidence_gate.evaluate({"macro": "ok", "tech": "parse_error"})
    assert v.skip is True
    assert v.lost == ["tech"]
    assert "tech=parse_error" in v.reason


@pytest.mark.parametrize("status", ["failed", "parse_error", "provider_error",
                                    "truncated", "content_missing"])
def test_every_lost_status_refuses(status):
    """Every LOST word still refuses — when it is the BLOCKING seat's."""
    assert evidence_gate.evaluate({"tech": status}).skip is True


# ---------- the blocking set is declared, not accidental ----------

@pytest.mark.parametrize("status", ["failed", "parse_error", "provider_error",
                                    "truncated", "content_missing",
                                    "carry_forward_empty", "carry_forward_failed"])
def test_the_technical_seat_blocks_on_every_lost_status(status):
    """Owner mandate 2026-09-18: "Only technical analysis can stop the desk."."""
    v = evidence_gate.evaluate({"tech": status, "macro": "ok"})
    assert v.skip is True
    assert v.blocking_lost == ["tech"]


@pytest.mark.parametrize("seat", ["macro", "news", "earnings", "smart_money",
                                  "sector"])
@pytest.mark.parametrize("status", ["failed", "parse_error", "provider_error",
                                    "truncated", "content_missing",
                                    "carry_forward_empty", "carry_forward_failed"])
def test_no_other_seat_blocks_on_any_lost_status(seat, status):
    """Each advisory seat, individually, loses its answer and the desk still
    decides. The loss is still recorded and still named."""
    v = evidence_gate.evaluate({seat: status, "tech": "ok"})
    assert v.skip is False
    assert v.lost == [seat]
    assert v.advisory_lost == [seat]
    assert v.blocking_lost == []
    assert seat in v.to_evidence()["advisory_lost_seats"]


def test_the_blocking_set_is_declared_in_one_place():
    """It must be a named constant a diff can show, not an emergent property
    of which seat happens to write a LOST word."""
    assert evidence_gate.BLOCKING_SEATS == frozenset({"tech"})
    source = (REPO / "src" / "evidence_gate.py").read_text()
    assert "MANDATE DECISION" in source
    assert "2026-09-18" in source
    assert "Only technical analysis can stop the desk" in source


def test_a_new_status_word_cannot_widen_the_blocking_set():
    """The accident this constant replaces: an advisory seat gaining a new
    failure word used to gain the power to stop the desk with it."""
    patched = dict(evidence_gate.STATUS_CATEGORY)
    patched["brand_new_failure_word"] = evidence_gate.CATEGORY_LOST
    with patch.dict(evidence_gate.STATUS_CATEGORY, patched, clear=True):
        v = evidence_gate.evaluate(
            {"news": "brand_new_failure_word", "tech": "ok"}
        )
    assert v.skip is False
    assert v.lost == ["news"]


def test_remembered_and_chose_not_to_refetch_do_not_refuse():
    """Kind+event remember is an answer, not a lost seat. Same-session
    reuse stays carried_from_morning (#430); cross-day GOOD is remembered;
    a quiet filing day is chose_not_to_refetch."""
    assert evidence_gate.evaluate({"macro": "remembered"}).skip is False
    assert evidence_gate.evaluate({"earnings": "chose_not_to_refetch"}).skip is False
    assert evidence_gate.counts_as_degraded("remembered") is False
    assert evidence_gate.counts_as_degraded("chose_not_to_refetch") is False


def test_expired_is_not_a_lost_answer():
    """2026-09-18. `expired` means the desk HOLDS a good answer and knows a
    newer one exists — neither "nothing to say" nor "the answer never
    arrived". Classifying it as lost contradicted the categorical line this
    module rests on. It is not integrity-clean, so it still feeds the
    degraded advisory, and the freshness disclosure names it."""
    v = evidence_gate.evaluate({"news": "expired", "tech": "ok"})
    assert v.skip is False
    assert v.lost == []
    assert v.expired == ["news"]
    assert evidence_gate.STATUS_CATEGORY["expired"] == evidence_gate.CATEGORY_EXPIRED
    assert evidence_gate.CATEGORY_EXPIRED != evidence_gate.CATEGORY_LOST
    assert evidence_gate.counts_as_degraded("expired") is True


def test_an_expired_technical_seat_does_not_block_either():
    """The blocking seat is not an exception to the split: expired is an
    answer the desk holds, whichever seat holds it."""
    assert evidence_gate.evaluate({"tech": "expired"}).skip is False


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
    v = evidence_gate.evaluate(
        {"tech": status, "macro": status, "earnings": "not_run_intraday"}
    )
    assert v.skip is True
    assert v.lost == ["macro", "tech"]
    assert v.blocking_lost == ["tech"]
    assert v.nothing_to_report == ["earnings"]


def test_intraday_status_split_distinguishes_skip_from_miss():
    """The two meanings that used to share one word."""
    cat = evidence_gate.STATUS_CATEGORY
    assert cat["not_run_intraday"] == evidence_gate.CATEGORY_NOTHING_TO_REPORT
    assert cat["carry_forward_empty"] == evidence_gate.CATEGORY_LOST
    assert cat["carry_forward_failed"] == evidence_gate.CATEGORY_LOST
    assert cat["carried_from_morning"] == evidence_gate.CATEGORY_REPORTED


def test_integrity_clean_statuses_are_classified_and_not_lost():
    """A word cannot be both 'usable for Risk' and 'lost for the gate'.
    Adding one without the other is how #428's split failed to reach RM."""
    for status in evidence_gate.INTEGRITY_CLEAN_STATUSES:
        assert status in evidence_gate.STATUS_CATEGORY, status
        assert evidence_gate.STATUS_CATEGORY[status] != evidence_gate.CATEGORY_LOST


@pytest.mark.parametrize("status", sorted(evidence_gate.INTEGRITY_CLEAN_STATUSES))
def test_reuse_and_intentional_skip_are_not_degraded(status):
    """Same-session reuse and an intentional skip are usable, not an
    integrity failure. This is the 2026-09-16 intra veto: Risk treated
    carried_from_morning / not_run_intraday as data_degraded."""
    assert evidence_gate.counts_as_degraded(status) is False


@pytest.mark.parametrize("status", [
    "failed", "parse_error", "provider_error", "truncated", "content_missing",
    "carry_forward_empty", "carry_forward_failed",
    "partial", "low_confidence", "degraded", "symbol_dropped",
    "figures_contradicted",
])
def test_real_failures_and_thin_reads_still_count_as_degraded(status):
    """The 2+ advisory must still fire on actual upstream problems.
    Thin-but-present reads (partial / low_confidence / …) stay degraded;
    only reuse and the intentional skip were taken off the list."""
    assert evidence_gate.counts_as_degraded(status) is True


def test_intra_reuse_package_is_not_two_plus_degraded():
    """The exact intra tick that reached Risk on 2026-09-16: tech re-run
    ok, news/macro reused, earnings an intentional skip. That is ZERO
    degraded seats, not three. The old ok/empty allow-list counted all
    three reuse words and the advisory always fired."""
    status = {
        "tech": "ok",
        "macro": "carried_from_morning",
        "news": "carried_from_morning",
        "earnings": "not_run_intraday",
    }
    degraded = [k for k, v in status.items() if evidence_gate.counts_as_degraded(v)]
    assert degraded == []
    assert evidence_gate.evaluate(status).skip is False


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
        f"src/evidence_gate.py as reported / nothing_to_report / expired / lost"
    )
    unfresh = sorted(written - set(evidence_gate.STATUS_FRESHNESS))
    assert not unfresh, (
        f"data_status value(s) with no freshness classification: {unfresh} — "
        f"classify them in src/evidence_gate.py STATUS_FRESHNESS as fresh / "
        f"carried / absent. An unclassified word is reported as unknown "
        f"freshness, which understates what the owner is told he has."
    )


# ---------- the freshness disclosure ----------

def test_every_classified_status_also_has_a_freshness():
    """The two maps must not drift. A status the gate knows about but the
    freshness map does not would be disclosed as 'cannot classify'."""
    missing = sorted(
        set(evidence_gate.STATUS_CATEGORY) - set(evidence_gate.STATUS_FRESHNESS)
    )
    assert not missing, f"no freshness classification for: {missing}"


def test_one_fresh_seat_plus_a_carried_book_is_disclosed_as_such():
    """THE failure mode the mandate change opens. Every seat here reports a
    status the desk calls integrity-clean, so nothing else in the codebase
    would say that four fifths of this decision was not read on this tick."""
    v = evidence_gate.evaluate({
        "tech": "ok",
        "macro": "remembered",
        "news": "carried_from_morning",
        "earnings": "chose_not_to_refetch",
        "smart_money": "not_run_intraday",
    })
    assert v.skip is False
    for status in v.data_status.values():
        assert status in evidence_gate.INTEGRITY_CLEAN_STATUSES
    f = v.freshness
    assert f.fresh == ["tech"]
    assert f.carried == ["earnings", "macro", "news", "smart_money"]
    assert f.absent == []
    record = v.to_evidence()
    assert record["seats_read_this_tick"] == 1
    assert record["seats_total"] == 5
    assert record["carried_seats"] == ["earnings", "macro", "news", "smart_money"]


def test_the_disclosure_reaches_the_owner_in_plain_words():
    from src.notifier import describe_evidence_freshness
    record = evidence_gate.evaluate({
        "tech": "ok",
        "macro": "remembered",
        "news": "expired",
        "earnings": "failed",
    }).freshness.to_evidence()
    text = "\n".join(describe_evidence_freshness(record))
    assert "1 of 4 research seats read just now" in text
    assert "the chart research" in text
    assert "the market-backdrop research" in text
    assert "already known to be out of date" in text
    assert "the news research" in text
    assert "no answer at all" in text
    assert "the earnings-filing research" in text
    # Owner-facing: no internal seat keys, no state tokens.
    # "news"/"earnings" are excluded: the approved plain wording for those
    # two seats legitimately contains the English word ("the news research").
    for banned in ("tech", "macro", "smart_money", "remembered",
                   "expired", "failed", "carried_from_morning"):
        assert banned not in text, f"owner wording still contains {banned!r}"


def test_the_disclosure_states_a_count_and_never_a_verdict():
    """Disclosure, not a threshold. The owner reserved the minimum-fresh
    number; nothing here may refuse, warn or grade on the count."""
    from src.notifier import describe_evidence_freshness
    record = evidence_gate.evaluate({"tech": "ok", "macro": "remembered"})
    assert record.skip is False
    text = " ".join(describe_evidence_freshness(record.freshness.to_evidence()))
    for verdict_word in ("too few", "insufficient", "minimum", "below",
                         "at least", "not enough"):
        assert verdict_word not in text.lower()


def test_an_unknown_state_is_never_counted_as_read_on_this_tick():
    """Overstating freshness is the defect this exists to prevent."""
    f = evidence_gate.freshness({"tech": "ok", "news": "some_new_word"})
    assert f.fresh == ["tech"]
    assert f.unknown == ["news"]
    assert f.to_evidence()["seats_read_this_tick"] == 1


def test_freshness_never_raises_on_junk():
    for junk in (None, [], "ok", {"tech": None}, {None: object()}):
        evidence_gate.freshness(junk)


def test_a_clean_morning_carries_the_disclosure_out_to_the_owner():
    """End to end: the gate proceeds, and the result the notifier renders
    still says how much of the evidence was read on this tick."""
    p = _pipeline({"tech": "ok", "macro": "remembered", "news": "remembered"})
    result, _, _ = _run(p)
    assert result["status"] != "evidence_gate_skip"
    record = result["evidence_freshness"]
    assert record["fresh_seats"] == ["tech"]
    assert record["carried_seats"] == ["macro", "news"]
    from src.notifier import describe_evidence_freshness
    assert describe_evidence_freshness(record)


def test_an_intra_result_carries_the_seat_states_out_to_the_alert():
    """An intra_check result dict never carried `data_status` — it did not
    have to, because a lost seat there halted the run instead. Now that an
    advisory loss proceeds, the one alert a mode's noise policy cannot
    suppress reads that field and must find it."""
    p = TradingPipeline.__new__(TradingPipeline)
    p._last_evidence_freshness = evidence_gate.freshness(
        {"tech": "ok", "news": "failed"}
    ).to_evidence()
    p._last_decision_data_status = {"tech": "ok", "news": "failed"}
    result = {"status": "intraday_no_trades", "run_id": "intra_check-1"}
    p._attach_evidence_freshness(result)
    assert result["data_status"] == {"tech": "ok", "news": "failed"}
    assert result["evidence_freshness"]["absent_seats"] == ["news"]
    from src.notifier import maybe_alert_data_quality
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        assert maybe_alert_data_quality(result, mode="intra_check") is True
    assert "news=failed" in alert.call_args[0][0]


def test_a_lost_advisory_seat_still_reaches_the_unsilenceable_alert():
    """It no longer halts the desk, so the one alert a mode's noise policy
    cannot suppress must still be able to see it."""
    from src.notifier import maybe_alert_data_quality
    p = _pipeline({"tech": "ok", "news": "failed"})
    result, _, _ = _run(p)
    assert result["status"] != "evidence_gate_skip"
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        assert maybe_alert_data_quality(result, mode="morning") is True
    assert "news=failed" in alert.call_args[0][0]


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
    p = _pipeline({"macro": "ok", "news": "failed", "tech": "failed"})
    result, write_status, alert = _run(p)
    assert result["status"] == "evidence_gate_skip"
    assert result["lost_seats"] == ["news", "tech"]
    assert result["blocking_lost_seats"] == ["tech"]
    assert result["orders"] == []
    p._decision_stage.assert_not_called()
    write_status.assert_called_once_with("morning", "evidence_gate_skip")
    alert.assert_called_once()
    assert "DECISION SKIPPED" in alert.call_args[0][0]


def test_the_skip_carries_data_status_so_the_standalone_alert_fires():
    """Loudness path 2: main.py's finally block pages off `data_status` in
    the result, independent of the session message's noise policy."""
    from src.notifier import maybe_alert_data_quality
    p = _pipeline({"macro": "failed", "tech": "failed"})
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
    p = _pipeline({"tech": "parse_error"})
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
    p = _pipeline({"tech": "failed"})
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


# ---------- owner-facing rendering of the skip (2026-09-18) ----------
#
# The owner was told the same 11:19 ET skip twice, one minute apart — once
# by the standalone alert below and once by the intraday tick's own
# message — and both copies carried the machine reason verbatim.


def _skip_alert(session: str, data_status: dict):
    """Run `_evidence_gate_skip` for one session and return the owner alert
    text it sent, or None when it deliberately stayed quiet."""
    from src import decision_checkpoint as dc

    p = _pipeline(data_status)
    ctx = MagicMock()
    ctx.data_status = dict(data_status)
    ctx.analyses = []
    with patch.object(dc, "write_status"), \
         patch("src.notifier.send_owner_alert", return_value=True) as alert:
        p._evidence_gate_skip(ctx, "intra_check-cfb08f1c", session=session)
    if not alert.call_args_list:
        return None
    return alert.call_args[0][0]


def test_an_intraday_skip_does_not_also_fire_the_standalone_alert():
    """Defect 1: two messages for one event. The intraday tick message
    always speaks for a skip, so this alert must not duplicate it."""
    assert _skip_alert("intra_check", {"smart_money": "failed", "tech": "failed"}) is None


def test_a_morning_skip_still_tells_the_owner_once():
    """The condition is never silenced — only de-duplicated."""
    text = _skip_alert("morning", {"smart_money": "failed", "tech": "failed"})
    assert text is not None
    assert "DECISION SKIPPED" in text


def test_the_owner_alert_carries_no_machine_text():
    """Defects 2 and 3: a source-file reference, an internal seat key, a
    raw state token, "N seat(s)" and a run identifier all reached him."""
    text = _skip_alert("morning", {"smart_money": "failed", "tech": "failed"})
    for banned in ("docs/WORK.md", "item 20", "smart_money", "seat(s)",
                   "=failed", "intra_check-cfb08f1c", "run "):
        assert banned not in text, f"owner alert still contains {banned!r}"
    # congress_enabled is on (src/config.py default since the 2026-09-20
    # owner ruling), so this must say "insider-and-congressional", not the
    # insider-only wording used while the switch was off.
    assert "the insider-and-congressional-trading feed" in text


def test_the_owner_alert_is_bullets_not_a_paragraph():
    """Defect 4: his approved format is a bold conclusion line then short
    bullets, one idea each."""
    lines = [ln for ln in _skip_alert(
        "morning", {"smart_money": "failed", "tech": "failed"}).split("\n") if ln.strip()]
    assert lines[0].startswith("<b>") and lines[0].endswith("</b>")
    assert all(ln.strip().startswith("•") for ln in lines[1:])


def test_the_stored_machine_reason_names_only_the_blocking_seat():
    """The durable record is machine text and stays machine text. It now
    names the seat that actually stopped the run — an advisory loss is
    reported, but it is not the reason the desk refused."""
    v = evidence_gate.evaluate({"smart_money": "failed", "tech": "failed"})
    assert v.reason == (
        "decision skipped: 1 blocking seat(s) were asked and their answer "
        "never arrived — tech=failed. A decision resting on an answer the "
        "desk never received is not a degraded decision, it is a fabricated "
        "one (docs/WORK.md item 20)."
    )


def test_the_stored_reason_says_so_when_an_advisory_seat_was_lost():
    """A proceed with a lost advisory seat must not read as a clean run."""
    reason = evidence_gate.evaluate({"news": "failed", "tech": "ok"}).reason
    assert reason.startswith("decision proceeded:")
    assert "news=failed" in reason
    assert "only the technical seat can stop the desk" in reason


def test_every_lost_status_has_plain_owner_wording():
    """A lost seat is the one category that always reaches the owner, so
    none of its statuses may fall through to the raw-token description."""
    from src.notifier import _DATA_STATUS_WORDS

    lost = sorted(
        s for s, cat in evidence_gate.STATUS_CATEGORY.items()
        if cat == evidence_gate.CATEGORY_LOST
    )
    missing = [s for s in lost if s not in _DATA_STATUS_WORDS]
    assert not missing, f"no plain wording for lost status(es): {missing}"


def test_the_intraday_tick_message_shows_the_disclosure_too():
    """The owner's intraday message is a different renderer from the session
    message, and it is the one he actually reads on a scan tick."""
    from src.trader_feed import _append_intraday_evidence_freshness
    record = evidence_gate.freshness({
        "tech": "ok", "macro": "remembered", "news": "carry_forward_empty",
    }).to_evidence()
    lines: list[str] = []
    _append_intraday_evidence_freshness(
        lines, {"evidence_freshness": record}, None,
    )
    text = "\n".join(lines)
    assert "1 of 3 research seats read just now" in text
    assert "the chart research" in text
    assert "the market-backdrop research" in text
    assert "no answer at all" in text
    # A tick with no record renders nothing rather than guessing.
    empty: list[str] = []
    _append_intraday_evidence_freshness(empty, {}, None)
    assert empty == []
