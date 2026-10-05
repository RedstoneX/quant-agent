"""The freshness record read through the gate's real status vocabulary.

Split out of `tests/test_evidence_gate.py` when the freshness reader became
`src/evidence_freshness.py`. These cases exercise the desk's ACTUAL status
words, so they read the gate's tables; the boundary proof that touches no
gate at all is `tests/test_evidence_freshness.py`.
"""
from unittest.mock import patch

from src import evidence_gate
from src.evidence_gate import STATUS_CATEGORY, STATUS_FRESHNESS  # noqa: F401
from tests.test_evidence_gate import _pipeline, _run, build_pipeline  # noqa: F401
from src.evidence_freshness import build_freshness_reader as _build_freshness_reader


def _freshness(data_status):
    """Build the reader from the gate's own tables, as production does."""
    return _build_freshness_reader(
        status_freshness=evidence_gate.STATUS_FRESHNESS,
        expired_statuses=frozenset(
            w for w, c in evidence_gate.STATUS_CATEGORY.items()
            if c == evidence_gate.CATEGORY_EXPIRED
        ),
        fresh_label=evidence_gate.FRESHNESS_FRESH,
        carried_label=evidence_gate.FRESHNESS_CARRIED,
        absent_label=evidence_gate.FRESHNESS_ABSENT,
    ).read(data_status)


def test_every_classified_status_also_has_a_freshness():
    """The two maps must not drift. A status the gate knows about but the
    freshness map does not would be disclosed as 'cannot classify'."""
    missing = sorted(
        set(evidence_gate.STATUS_CATEGORY) - set(evidence_gate.STATUS_FRESHNESS)
    )
    assert not missing, f"no freshness classification for: {missing}"


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
    f = _freshness({"tech": "ok", "news": "some_new_word"})
    assert f.fresh == ["tech"]
    assert f.unknown == ["news"]
    assert f.to_evidence()["seats_read_this_tick"] == 1


def test_freshness_never_raises_on_junk():
    for junk in (None, [], "ok", {"tech": None}, {None: object()}):
        _freshness(junk)


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
    p = build_pipeline(_last_evidence_freshness=_freshness( {"tech": "ok", "news": "failed"} ).to_evidence(), _last_decision_data_status={"tech": "ok", "news": "failed"})
    result = {"status": "intraday_no_trades", "run_id": "intra_check-1"}
    p._attach_evidence_freshness(result)
    assert result["data_status"] == {"tech": "ok", "news": "failed"}
    assert result["evidence_freshness"]["absent_seats"] == ["news"]
    from src.notifier import maybe_alert_data_quality
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        assert maybe_alert_data_quality(result, mode="intra_check") is True
    assert "news=failed" in alert.call_args[0][0]


def test_a_decision_made_short_handed_is_marked_as_such():
    """Board item 154. When a seat is unreachable and the desk PROCEEDS
    (advisory loss, owner mandate 2026-09-18), the owner message must say the
    decision was made short-handed and name the missing seat — the mirror of
    the skip banner, which fires only when the desk REFUSES (item 20)."""
    from src.notifier import describe_short_handed_decision
    record = _freshness(
        {"tech": "ok", "macro": "ok", "news": "failed"}
    ).to_evidence()
    lines = describe_short_handed_decision(record)
    text = "\n".join(lines)
    assert "SHORT-HANDED" in text
    assert "the news research" in text  # named in plain words
    # Owner-facing: no internal seat keys or raw state tokens.
    for banned in ("tech", "macro", "smart_money", "failed", "news="):
        assert banned not in text, f"owner wording still contains {banned!r}"


def test_a_fully_staffed_decision_is_not_marked_short_handed():
    from src.notifier import describe_short_handed_decision
    record = _freshness(
        {"tech": "ok", "macro": "ok", "news": "remembered"}
    ).to_evidence()
    assert describe_short_handed_decision(record) == []


def test_the_short_handed_mark_states_a_fact_and_never_a_verdict():
    """Disclosure, not a threshold — the minimum-seat count is the owner's
    (docs/WORK.md item 20). It may not grade or refuse on the count."""
    from src.notifier import describe_short_handed_decision
    record = _freshness({"tech": "ok", "news": "failed"}).to_evidence()
    text = " ".join(describe_short_handed_decision(record)).lower()
    for verdict_word in ("too few", "insufficient", "minimum", "below",
                         "at least", "not enough", "should not have"):
        assert verdict_word not in text


def test_proceed_short_handed_message_carries_the_mark_but_a_skip_does_not():
    """End to end through the session renderer: an advisory loss that PROCEEDS
    gets the short-handed mark; an evidence-gate refusal does not (its own
    'NOTHING WAS TRADED' banner already speaks for the missing seat)."""
    from src.notifier import _append_evidence_freshness
    proceeded = {
        "status": "ok",
        "evidence_freshness": _freshness(
            {"tech": "ok", "news": "failed"}
        ).to_evidence(),
    }
    lines: list[str] = []
    _append_evidence_freshness(lines, proceeded)
    assert any("SHORT-HANDED" in ln for ln in lines)

    refused = {
        "status": "evidence_gate_skip",
        "evidence_freshness": _freshness(
            {"tech": "failed"}
        ).to_evidence(),
    }
    lines2: list[str] = []
    _append_evidence_freshness(lines2, refused)
    assert not any("SHORT-HANDED" in ln for ln in lines2)


def test_the_intraday_tick_message_shows_the_disclosure_too():
    """The owner's intraday message is a different renderer from the session
    message, and it is the one he actually reads on a scan tick."""
    from src.trader_feed import _append_intraday_evidence_freshness
    record = _freshness({
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
