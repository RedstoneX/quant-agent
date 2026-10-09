"""The per-name coverage record, constructed on its own.

THE POINT OF THIS FILE: nothing here imports `src.evidence_gate`. The
recorder is built from plain values — three seat lists — and every
behaviour of the counting half is exercised against that object alone. If
this file ever needs the gate module back, the boundary was not real.
"""

from src.evidence_name_coverage import (
    NAME_SCOPED_SEATS,
    RUN_SCOPED_SEATS,
    NameCoverage,
    NameCoverageRecorder,
    build_name_coverage_recorder,
)

#: The owner's 2026-09-18 mandate set, written out as a plain value rather
#: than imported, so this file never reaches back into the gate.
BLOCKING_SEATS = frozenset({"tech"})


def _recorder(**kw):
    kw.setdefault("blocking_seats", BLOCKING_SEATS)
    return build_name_coverage_recorder(**kw)


def test_the_recorder_is_built_from_plain_values_and_keeps_each_one():
    """Construction proof: every collaborator is the object handed in."""
    built = build_name_coverage_recorder(
        name_scoped_seats=("tech", "news"),
        run_scoped_seats=("macro",),
        blocking_seats={"news"},
    )
    assert isinstance(built, NameCoverageRecorder)
    assert built._name_scoped == ("tech", "news")
    assert built._run_scoped == ("macro",)
    assert built._blocking_seats == ("news",)
    # The seat lists it was handed are the ones it records against — no
    # module-level set leaks in behind them.
    cov = built.coverage({"AAA"}, {"tech": {"AAA"}})["AAA"]
    assert cov.covered == ["tech"] and cov.uncovered == ["news"]
    assert cov.blocking_missing == ["news"]
    assert built.names_missing_blocking_seat({"AAA": cov}) == {"AAA": ["news"]}


def test_the_mandate_set_has_no_default_so_it_cannot_be_forgotten():
    import pytest

    with pytest.raises(TypeError):
        build_name_coverage_recorder()


def test_a_record_handed_no_blocking_seats_blocks_on_nothing():
    """A bare `NameCoverage` carries no mandate set of its own."""
    bare = NameCoverage(symbol="AAA", uncovered=["tech"])
    assert bare.blocking_missing == []
    assert NAME_SCOPED_SEATS and RUN_SCOPED_SEATS


# --- the counting half: per-name coverage is RECORDED, never scored --------


def test_name_coverage_records_which_seats_answered_about_each_name():
    record = _recorder().coverage(
        {"AAA", "BBB"},
        {"tech": {"AAA", "BBB"}, "earnings": {"AAA"}, "smart_money": set()},
    )
    assert set(record) == {"AAA", "BBB"}
    assert record["AAA"].covered == ["earnings", "tech"]
    assert record["BBB"].covered == ["tech"]
    assert "earnings" in record["BBB"].uncovered


def test_a_seat_that_records_no_per_name_coverage_is_never_assumed_complete():
    """News writes no symbol-scoped row in production; absence of a record
    must read as 'no answer about this name', never as full coverage."""
    record = _recorder().coverage({"AAA"}, {"tech": {"AAA"}})
    assert "news" in record["AAA"].uncovered
    assert "news" not in record["AAA"].covered


def test_a_market_wide_seat_is_never_booked_as_a_per_name_gap():
    record = _recorder().coverage({"AAA"}, {"tech": {"AAA"}})
    assert record["AAA"].run_scoped == ["macro"]
    assert "macro" not in record["AAA"].uncovered
    assert "macro" not in record["AAA"].covered


def test_name_coverage_holds_no_threshold_ratio_or_verdict():
    """Item 20's counting half ships as a record. If a bar ever appears in
    this module it was invented, which the owner's ruling forbids."""
    record = _recorder().coverage({"AAA"}, {"tech": {"AAA"}})
    payload = record["AAA"].to_evidence()
    # Item 220 added two CATEGORICAL list fields (which blocking seat did not
    # answer about this name, and which of those returned something that could
    # not be read). Neither is a count, a ratio or a bar, and the numeric
    # assertions below still bind on every field.
    assert set(payload) == {
        "symbol",
        "covered_seats",
        "uncovered_seats",
        "run_scoped_seats",
        "unreadable_seats",
        "asked_no_answer_seats",
        "never_asked_seats",
        "blocking_seats_missing",
        "summary",
    }
    for value in payload.values():
        assert not isinstance(value, (int, float, bool)), payload


def test_name_coverage_never_raises_on_junk():
    assert _recorder().coverage(None, None) == {}
    assert _recorder().coverage({"AAA"}, {"tech": None})["AAA"].covered == []
    assert _recorder().coverage(["aaa ", ""], {"tech": [" aaa"]})["AAA"].covered == ["tech"]


# --- Board item 220: a seat answer that could not be read is never silence ---


def test_unreadable_answer_is_not_coverage_and_is_named():
    """A seat whose row came back unreadable stays UNCOVERED and is named.

    The technical seat is the timing veto. Booking an unreadable row as an
    answer would let the entry and stay paths read absence of an objection
    as agreement, which is the defect item 220 exists to close.
    """
    cov = _recorder().coverage(
        ["AAA", "BBB"],
        {"tech": ["BBB"], "news": ["AAA", "BBB"], "earnings": ["AAA", "BBB"], "smart_money": ["AAA", "BBB"]},
        unreadable_by_seat={"tech": ["AAA"]},
    )
    assert "tech" not in cov["AAA"].covered
    assert "tech" in cov["AAA"].uncovered
    assert cov["AAA"].unreadable == ["tech"]
    assert cov["AAA"].blocking_missing == ["tech"]
    # The other name is untouched: one lost row is not a lost seat.
    assert "tech" in cov["BBB"].covered
    assert cov["BBB"].unreadable == []
    assert cov["BBB"].blocking_missing == []


def test_unreadable_record_says_did_not_answer_never_neutral():
    cov = _recorder().coverage(
        ["AAA"],
        {"news": ["AAA"]},
        unreadable_by_seat={"tech": ["AAA"]},
    )["AAA"]
    text = cov.summary.lower()
    assert "no answer about this name from" in text
    assert "unreadable" in text
    for word in ("neutral", "no objection", "agree"):
        assert word not in text
    evidence = cov.to_evidence()
    assert evidence["unreadable_seats"] == ["tech"]
    assert evidence["blocking_seats_missing"] == ["tech"]
    # Asked-and-unreadable is NOT never-asked: different cause, different fix.
    assert "tech" not in evidence["never_asked_seats"]
    assert "tech" not in evidence["asked_no_answer_seats"]


def test_names_missing_blocking_seat_lists_only_the_blocked_names():
    cov = _recorder().coverage(
        ["AAA", "BBB"],
        {"tech": ["BBB"], "news": ["AAA"], "earnings": ["BBB"], "smart_money": ["BBB"]},
        unreadable_by_seat={"tech": ["AAA"]},
    )
    gaps = _recorder().names_missing_blocking_seat(cov)
    assert gaps == {"AAA": ["tech"]}


def test_names_missing_blocking_seat_never_raises():
    assert _recorder().names_missing_blocking_seat(None) == {}
    assert _recorder().names_missing_blocking_seat({"AAA": object()}) == {}


def test_coverage_record_carries_no_numeric_bar_for_the_new_fields():
    """The per-name half is disclosure; item 220 must not smuggle in a ratio."""
    cov = (
        _recorder()
        .coverage(
            ["AAA"],
            {"news": ["AAA"]},
            unreadable_by_seat={"tech": ["AAA"]},
        )["AAA"]
        .to_evidence()
    )
    for value in cov.values():
        assert not isinstance(value, (int, float)) or isinstance(value, bool)


def test_three_causes_of_a_missing_seat_are_told_apart_by_the_fields():
    """Asked-and-unreadable, asked-and-silent, and never-asked are distinct.

    A later reader must be able to tell them apart WITHOUT parsing prose:
    they share one consequence (no answer, so no veto satisfied) but have
    three different causes and three different fixes, and collapsing them
    would hide which one is actually happening.
    """
    cov = _recorder().coverage(
        ["AAA", "BBB", "CCC"],
        {"news": ["AAA", "BBB", "CCC"], "earnings": ["AAA", "BBB", "CCC"], "smart_money": ["AAA", "BBB", "CCC"]},
        unreadable_by_seat={"tech": ["AAA"]},
        asked_no_answer_by_seat={"tech": ["BBB"]},
    )
    assert cov["AAA"].unreadable == ["tech"]
    assert cov["AAA"].asked_no_answer == [] and cov["AAA"].never_asked == []
    assert cov["BBB"].asked_no_answer == ["tech"]
    assert cov["BBB"].unreadable == [] and cov["BBB"].never_asked == []
    assert cov["CCC"].never_asked == ["tech"]
    assert cov["CCC"].unreadable == [] and cov["CCC"].asked_no_answer == []
    # All three are the SAME missing veto, whatever the cause.
    for name in ("AAA", "BBB", "CCC"):
        assert cov[name].blocking_missing == ["tech"]


def test_a_row_that_came_back_outranks_asked_and_silent():
    cov = _recorder().coverage(
        ["AAA"],
        {"news": ["AAA"]},
        unreadable_by_seat={"tech": ["AAA"]},
        asked_no_answer_by_seat={"tech": ["AAA"]},
    )["AAA"]
    assert cov.unreadable == ["tech"] and cov.asked_no_answer == []
