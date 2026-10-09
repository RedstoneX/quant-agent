"""The seat's OWN verdict on its model's answer, beside the responding model.

`agent_logs.status` only ever meant "the provider call returned", so no
usable-answer rate was computable for any model. These tests pin the two
properties that make it computable: the verdict persists on the same row as
the response, and the reason vocabulary stays closed.
"""

import pytest

from src.agents.base import seat_acceptance_kwargs
from src.refusal_signature import (
    SEAT_ACCEPTANCE_WORDS,
    SEAT_ACCEPTED,
    SEAT_REFUSED,
    SEAT_REFUSAL_REASONS,
)


def test_accepted_when_no_refusal_reason():
    assert seat_acceptance_kwargs(None) == {
        "acceptance": SEAT_ACCEPTED,
        "acceptance_reason": None,
    }


@pytest.mark.parametrize("reason", sorted(SEAT_REFUSAL_REASONS))
def test_registered_reasons_pass_through(reason):
    assert seat_acceptance_kwargs(reason) == {
        "acceptance": SEAT_REFUSED,
        "acceptance_reason": reason,
    }


def test_unregistered_reason_is_flagged_not_silently_stored():
    """An unregistered outcome word has corrupted refusal counting before."""
    out = seat_acceptance_kwargs("something_nobody_registered")
    assert out["acceptance"] == SEAT_REFUSED
    assert out["acceptance_reason"] == "unregistered:something_nobody_registered"


def test_vocabulary_is_a_closed_pair():
    assert SEAT_ACCEPTANCE_WORDS == {"accepted", "refused"}


@pytest.fixture
def db(tmp_path):
    from src.storage.db import Database

    database = Database(str(tmp_path / "seat_acceptance.db"))
    database.initialize()
    yield database
    database.close()


def test_verdict_persists_beside_the_responding_model(db):
    db.insert_agent_log(
        agent_name="risk_manager",
        run_id="r1",
        input_summary="i",
        output_summary="o",
        full_response="{}",
        model="free-model-x",
        tokens_used=1,
        **seat_acceptance_kwargs("risk_manager_unparseable_output"),
    )
    db.insert_agent_log(
        agent_name="risk_manager",
        run_id="r1",
        input_summary="i",
        output_summary="o",
        full_response="{}",
        model="free-model-x",
        tokens_used=1,
        **seat_acceptance_kwargs(None),
    )
    rows = db.execute("SELECT model, acceptance, acceptance_reason FROM agent_logs ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("free-model-x", "refused", "risk_manager_unparseable_output"),
        ("free-model-x", "accepted", None),
    ]
    # The rate the desk could not compute before now falls out of SQL.
    refused, total = db.execute(
        "SELECT SUM(acceptance='refused'), COUNT(*) FROM agent_logs "
        "WHERE model='free-model-x' AND acceptance IS NOT NULL"
    ).fetchone()
    assert (refused, total) == (1, 2)


def test_legacy_rows_stay_null_not_accepted(db):
    db.insert_agent_log(
        agent_name="macro_analyst",
        run_id="r",
        input_summary="i",
        output_summary="o",
        full_response="{}",
        model="m",
        tokens_used=1,
    )
    row = db.execute("SELECT acceptance, acceptance_reason FROM agent_logs").fetchone()
    assert tuple(row) == (None, None)


# --- board item 188: the DECISION seats record the gate's own reason -------
#
# Recording only. These tests pin that the reason the gate itself produced
# survives to the row, instead of being collapsed into one word per seat or
# left behind as prose in a log line.


def _result(gate_reason):
    from src.agents.base import AgentResult

    return AgentResult(
        raw_text="",
        tokens_used=0,
        model="m",
        gate_reason=gate_reason,
    )


def test_gate_reason_beats_the_call_sites_one_word_summary():
    out = seat_acceptance_kwargs(
        "no_valid_grounded_decision",
        result=_result("pm_grounding_error"),
    )
    assert out == {
        "acceptance": SEAT_REFUSED,
        "acceptance_reason": "pm_grounding_error",
    }


def test_gate_reason_is_ignored_on_an_accepted_answer():
    assert seat_acceptance_kwargs(None, result=_result("pm_parse_error")) == {
        "acceptance": SEAT_ACCEPTED,
        "acceptance_reason": None,
    }


def test_absent_gate_reason_falls_back_and_never_guesses():
    for bad in (None, "", object()):
        out = seat_acceptance_kwargs(
            "risk_manager_unparseable_output",
            result=_result(None) if bad is None else _result(bad) if isinstance(bad, str) else bad,
        )
        assert out["acceptance_reason"] == "risk_manager_unparseable_output"


def test_each_decision_seat_names_its_own_refusals():
    import inspect
    from src.agents import portfolio_manager, position_reviewer, risk_manager

    for module, prefix in (
        (portfolio_manager, "pm_"),
        (risk_manager, "risk_"),
        (position_reviewer, "review_"),
    ):
        src = inspect.getsource(module)
        assert "gate_reason" in src, module.__name__
        assert any(w.startswith(prefix) and w in src for w in SEAT_REFUSAL_REASONS), module.__name__


@pytest.mark.parametrize("word", sorted(w for w in SEAT_REFUSAL_REASONS if w.startswith(("pm_", "risk_", "review_"))))
def test_every_gate_word_is_registered_vocabulary(word):
    assert seat_acceptance_kwargs("agent_failure", result=_result(word)) == {
        "acceptance": SEAT_REFUSED,
        "acceptance_reason": word,
    }
