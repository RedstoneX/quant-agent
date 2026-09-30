"""The seat's OWN verdict on its model's answer, beside the responding model.

`agent_logs.status` only ever meant "the provider call returned", so no
usable-answer rate was computable for any model. These tests pin the two
properties that make it computable: the verdict persists on the same row as
the response, and the reason vocabulary stays closed.
"""
import pytest

from src.agents.base import seat_acceptance_kwargs
from src.refusal_signature import (
    SEAT_ACCEPTANCE_WORDS, SEAT_ACCEPTED, SEAT_REFUSED, SEAT_REFUSAL_REASONS,
)


def test_accepted_when_no_refusal_reason():
    assert seat_acceptance_kwargs(None) == {
        "acceptance": SEAT_ACCEPTED, "acceptance_reason": None,
    }


@pytest.mark.parametrize("reason", sorted(SEAT_REFUSAL_REASONS))
def test_registered_reasons_pass_through(reason):
    assert seat_acceptance_kwargs(reason) == {
        "acceptance": SEAT_REFUSED, "acceptance_reason": reason,
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
        agent_name="risk_manager", run_id="r1", input_summary="i",
        output_summary="o", full_response="{}", model="free-model-x",
        tokens_used=1,
        **seat_acceptance_kwargs("risk_manager_unparseable_output"),
    )
    db.insert_agent_log(
        agent_name="risk_manager", run_id="r1", input_summary="i",
        output_summary="o", full_response="{}", model="free-model-x",
        tokens_used=1, **seat_acceptance_kwargs(None),
    )
    rows = db.execute(
        "SELECT model, acceptance, acceptance_reason FROM agent_logs "
        "ORDER BY id"
    ).fetchall()
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
        agent_name="macro_analyst", run_id="r", input_summary="i",
        output_summary="o", full_response="{}", model="m", tokens_used=1,
    )
    row = db.execute(
        "SELECT acceptance, acceptance_reason FROM agent_logs"
    ).fetchone()
    assert tuple(row) == (None, None)
