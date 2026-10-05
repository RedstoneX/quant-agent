"""A seat read must SAY when it was read and by which run.

Why this exists, in one paragraph. The owner ruled on 2026-10-01 that any
holding failing the desk's own fresh-entry bar is sold, and that the test is
re-run several times a day. That turns "was this seat read in THIS run?"
into a question a SELL rests on. Before this, nothing recorded it: the
evidence gate classified each seat as fresh / carried / absent, but the
classification carried no run identity and no timestamp, so a reader looking
at a stored row an hour later could only INFER whether the reading was taken
in the run it was sitting next to. The difference between "cut losses fast"
and "sell on stale information" is exactly that record.

This file records only. There is no threshold here, no expiry window and no
decision gated on any of it — `age_seconds` is reported and nothing says
when an age is too old. A cutoff would be an invented number.
"""

import json

from src import evidence_gate


from src.evidence_freshness import build_freshness_reader as _build_freshness_reader

from src import evidence_freshness as _ef



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



MORNING_AT = "2026-10-01T09:30:00+00:00"
INTRA_AT = "2026-10-01T12:00:00+00:00"


def _morning():
    """Every analyst seat read fresh, as the morning run does."""
    return _freshness({
        "tech": "ok", "news": "ok", "macro": "ok",
        "earnings": "ok", "smart_money": "ok",
    }).stamped(run_id="morning-aaa", mode="morning", stamped_at=MORNING_AT)


def _intra(prior):
    """The half-hourly tick: the technical seat is re-read, the rest are
    whatever the morning left behind."""
    return _freshness({
        "tech": "ok",
        "news": "carried_from_morning",
        "macro": "not_run_intraday",
        "earnings": "chose_not_to_refetch",
        "smart_money": "failed",
    }).stamped(run_id="intra-bbb", mode="intra_check", stamped_at=INTRA_AT,
               prior_reads=prior)


def test_refreshed_only_when_the_seat_actually_ran_in_that_run():
    record = _morning().to_evidence()
    for seat in ("tech", "news", "macro", "earnings", "smart_money"):
        state = evidence_gate.seat_read_state(record, seat,
                                              run_id="morning-aaa")
        assert state.refreshed_this_session, seat
        assert state.run_id == "morning-aaa"
        assert state.mode == "morning"
        assert state.at == MORNING_AT


def test_half_hourly_tick_refreshes_only_the_seat_that_ran():
    """The case that matters most: 13 ticks a day, one seat re-read."""
    prior = {
        seat: {"run_id": "morning-aaa", "mode": "morning", "at": MORNING_AT}
        for seat in ("news", "macro", "earnings", "smart_money")
    }
    record = _intra(prior).to_evidence()

    tech = evidence_gate.seat_read_state(record, "tech", run_id="intra-bbb")
    assert tech.state == _ef.READ_REFRESHED
    assert tech.age_seconds == 0

    for seat in ("news", "macro", "earnings"):
        state = evidence_gate.seat_read_state(record, seat,
                                              run_id="intra-bbb")
        assert state.state == _ef.READ_CARRIED, seat
        assert not state.refreshed_this_session
        # Two and a half hours between the morning read and this tick. The
        # age is REPORTED; nothing here judges it.
        assert state.age_seconds == 9000, seat
        assert state.run_id == "morning-aaa", seat
        assert "NOT read in this run" in state.summary


def test_absent_is_not_carried_and_carried_is_not_fresh():
    """The three states never collapse into each other."""
    record = _intra({}).to_evidence()
    assert evidence_gate.seat_read_state(
        record, "smart_money").state == _ef.READ_ABSENT
    assert evidence_gate.seat_read_state(
        record, "news").state == _ef.READ_CARRIED
    assert evidence_gate.seat_read_state(
        record, "tech").state == _ef.READ_REFRESHED
    # A seat nobody ever recorded is absent, not stale.
    assert evidence_gate.seat_read_state(
        record, "congress").state == _ef.READ_ABSENT


def test_carried_with_no_known_earlier_read_says_unknown_not_fresh():
    record = _intra({}).to_evidence()
    state = evidence_gate.seat_read_state(record, "news")
    assert state.state == _ef.READ_CARRIED
    assert state.age_seconds is None
    assert "age unknown" in state.summary


def test_a_stamp_from_another_run_is_not_this_session():
    """Read back later, a morning stamp is carried forward, not fresh."""
    record = _morning().to_evidence()
    state = evidence_gate.seat_read_state(record, "tech", run_id="intra-bbb")
    assert state.state == _ef.READ_CARRIED
    assert not state.refreshed_this_session


def test_an_unclassifiable_status_is_never_reported_as_read():
    record = _freshness({"tech": "something_new"}).stamped(
        run_id="r", mode="morning").to_evidence()
    assert evidence_gate.seat_read_state(
        record, "tech").state == _ef.READ_UNKNOWN


def test_a_record_written_before_stamping_existed_is_still_answerable():
    """Old rows carry the bucket lists but no stamps. They must degrade to
    an honest answer rather than claiming this run's freshness."""
    legacy = {"fresh_seats": ["tech"], "carried_seats": ["news"],
              "absent_seats": ["macro"]}
    assert evidence_gate.seat_read_state(
        legacy, "tech").state == _ef.READ_REFRESHED
    assert evidence_gate.seat_read_state(legacy, "tech").run_id is None
    assert evidence_gate.seat_read_state(
        legacy, "news").state == _ef.READ_CARRIED
    assert evidence_gate.seat_read_state(
        legacy, "macro").state == _ef.READ_ABSENT


def test_the_predicate_never_raises_on_rubbish():
    for bad in (None, [], "", {"seat_stamps": "nope"},
                {"seat_stamps": {"tech": 7}}):
        assert evidence_gate.seat_read_state(bad, "tech").state in (
            _ef.READ_ABSENT, _ef.READ_UNKNOWN)


# --- the recording must actually record -------------------------------

def test_the_stamps_survive_a_real_round_trip_through_storage(tmp_path):
    """Prove the field is WRITTEN, not merely declared.

    Goes through the real storage methods and the real SQLite file, for
    both report tables, because a recording that records nothing is the
    failure mode this repo has already been bitten by.
    """
    from src.storage.db import Database

    db = Database(str(tmp_path / "desk.db"))
    db.initialize()

    morning = {"evidence_freshness": _morning().to_evidence()}
    db.save_session_report(mode="morning", date="2026-10-01",
                           run_id="morning-aaa", payload=morning)

    back = db.get_session_report("morning", "2026-10-01")
    assert back is not None
    record = back["payload"]["evidence_freshness"] if "payload" in back \
        else json.loads(back["payload_json"])["evidence_freshness"]
    assert record["stamped_run_id"] == "morning-aaa"
    assert record["stamped_mode"] == "morning"
    assert evidence_gate.seat_read_state(
        record, "news", run_id="morning-aaa").refreshed_this_session

    # The read side finds the morning read from the stored row alone.
    prior = db.last_fresh_seat_reads(seats=["news", "macro"])
    assert prior["news"]["run_id"] == "morning-aaa"
    assert prior["news"]["at"] == MORNING_AT

    intra = {"evidence_freshness": _intra(prior).to_evidence()}
    db.save_intra_check_report(run_id="intra-bbb", date="2026-10-01",
                               payload=intra)
    tick = db.get_intra_check_report("intra-bbb")
    assert tick is not None
    stored = tick["payload"]["evidence_freshness"] if "payload" in tick \
        else json.loads(tick["payload_json"])["evidence_freshness"]
    assert evidence_gate.seat_read_state(
        stored, "tech", run_id="intra-bbb").refreshed_this_session
    news = evidence_gate.seat_read_state(stored, "news", run_id="intra-bbb")
    assert news.state == _ef.READ_CARRIED
    assert news.age_seconds == 9000


def test_the_gate_stamps_what_it_persists(tmp_path):
    """The gate's own disclosure must carry the stamp, or the row above is
    never written in production.

    Calls the function-only gate directly with a stub owner holding just a
    real in-memory-file database, and reads the outcome, so it fails when
    the stamp or the prior-read lookup stops happening and not merely when a
    word disappears from the source. No pipeline object is built.
    """
    from types import SimpleNamespace

    from src import pipeline_halt_gates
    from src.storage.db import Database

    db = Database(str(tmp_path / "desk.db"))
    db.initialize()
    db.save_session_report(
        mode="morning", date="2026-10-01", run_id="morning-aaa",
        payload={"evidence_freshness": _morning().to_evidence()},
    )
    owner = SimpleNamespace(db=db, _record_name_coverage=lambda *a, **k: None)
    ctx = SimpleNamespace(
        run_id="intra-bbb", session="intra_check", analyses=[],
        decision_id=None,
        data_status={"tech": "ok", "news": "carried_from_morning"},
    )
    pipeline_halt_gates._evidence_gate_skip(
        owner, ctx, "intra-bbb", session="intra_check")

    record = ctx.evidence_freshness
    assert record["stamped_run_id"] == "intra-bbb"
    assert record["stamped_mode"] == "intra_check"
    tech = evidence_gate.seat_read_state(record, "tech", run_id="intra-bbb")
    assert tech.state == _ef.READ_REFRESHED
    # The prior-read lookup ran: the carried seat names the morning run.
    news = evidence_gate.seat_read_state(record, "news", run_id="intra-bbb")
    assert news.state == _ef.READ_CARRIED
    assert news.run_id == "morning-aaa"
