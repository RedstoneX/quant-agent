"""One account per session — REST and the fill socket, or neither.

These tests pin the hazard that was measured before the fix: a session that
declared itself a rehearsal still resolved the desk's delivered key pair,
because the delivery directory was read from one fixed variable regardless of
which session was running. No real credential value appears here; every string
below is an obvious stand-in written in this file.
"""

from __future__ import annotations

import pytest

from src.credentials import _credentials_directory, session_broker_credentials
from src.session_identity import (
    DESK_CREDENTIALS_DIRECTORY_ENV,
    IDENTITY_ENV,
    IdentitySplit,
    assert_identity_whole,
    credentials_directory_var,
    fingerprint,
    session_identity,
    writes_outside_own_checkout,
)

STAND_IN_DESK = "STAND-IN-DESK-PAIR-NOT-A-CREDENTIAL"
STAND_IN_REHEARSAL = "STAND-IN-REHEARSAL-PAIR-NOT-A-CREDENTIAL"


def _delivered(tmp_path, value):
    directory = tmp_path / "delivered"
    directory.mkdir(exist_ok=True)
    (directory / "alpaca_api_key").write_text(value)
    (directory / "alpaca_secret_key").write_text(value)
    return directory


def test_identity_defaults_to_the_desk():
    assert session_identity({}) == "desk"
    assert session_identity({IDENTITY_ENV: "  Rehearsal "}) == "rehearsal"


def test_directory_variable_is_derived_from_the_identity():
    assert credentials_directory_var("desk") == DESK_CREDENTIALS_DIRECTORY_ENV
    assert credentials_directory_var("rehearsal") == "CREDENTIALS_DIRECTORY_REHEARSAL"


def test_a_rehearsal_session_cannot_see_the_desks_delivered_pair(tmp_path):
    """THE REPRODUCED DEFECT. Before the fix this returned the desk's pair."""
    directory = _delivered(tmp_path, STAND_IN_DESK)
    env = {
        DESK_CREDENTIALS_DIRECTORY_ENV: str(directory),
        IDENTITY_ENV: "rehearsal",
        "ALPACA_API_KEY": STAND_IN_REHEARSAL,
        "ALPACA_SECRET_KEY": STAND_IN_REHEARSAL,
    }
    assert _credentials_directory(env) is None
    assert session_broker_credentials(env) == (STAND_IN_REHEARSAL, STAND_IN_REHEARSAL)


def test_the_desk_still_reads_its_own_delivered_pair(tmp_path):
    directory = _delivered(tmp_path, STAND_IN_DESK)
    env = {DESK_CREDENTIALS_DIRECTORY_ENV: str(directory)}
    assert _credentials_directory(env) == directory
    assert session_broker_credentials(env) == (STAND_IN_DESK, STAND_IN_DESK)


def test_socket_refused_when_it_holds_a_different_pair_than_rest():
    with pytest.raises(IdentitySplit) as caught:
        assert_identity_whole(
            rest_key=STAND_IN_REHEARSAL,
            rest_secret=STAND_IN_REHEARSAL,
            socket_key=STAND_IN_DESK,
            socket_secret=STAND_IN_DESK,
            env={IDENTITY_ENV: "rehearsal", "CREDENTIALS_DIRECTORY_REHEARSAL": "/x"},
        )
    message = str(caught.value)
    assert "one account" in message
    assert STAND_IN_DESK not in message and STAND_IN_REHEARSAL not in message


def test_socket_refused_for_a_sandbox_tree_wearing_the_desk_identity(tmp_path):
    with pytest.raises(IdentitySplit) as caught:
        assert_identity_whole(
            rest_key=STAND_IN_DESK,
            rest_secret=STAND_IN_DESK,
            socket_key=STAND_IN_DESK,
            socket_secret=STAND_IN_DESK,
            env={},
            cwd=tmp_path,
        )
    assert IDENTITY_ENV in str(caught.value)


def test_socket_refused_when_a_named_identity_has_no_credentials_of_its_own():
    with pytest.raises(IdentitySplit):
        assert_identity_whole(
            rest_key=STAND_IN_DESK,
            rest_secret=STAND_IN_DESK,
            socket_key=STAND_IN_DESK,
            socket_secret=STAND_IN_DESK,
            env={
                IDENTITY_ENV: "rehearsal",
                DESK_CREDENTIALS_DIRECTORY_ENV: "/somewhere",
            },
        )


def test_a_whole_identity_is_allowed_and_described_without_values():
    from src.data_paths import repo_root

    described = assert_identity_whole(
        rest_key=STAND_IN_DESK,
        rest_secret=STAND_IN_DESK,
        socket_key=STAND_IN_DESK,
        socket_secret=STAND_IN_DESK,
        env={},
        cwd=repo_root(),
    )
    assert "whole" in described
    assert STAND_IN_DESK not in described


def test_fingerprint_reveals_nothing_of_the_value():
    mark = fingerprint(STAND_IN_DESK)
    assert len(mark) == 12
    assert mark not in STAND_IN_DESK
    assert STAND_IN_DESK[:2] not in mark or not mark
    assert fingerprint("") == "absent"
    assert fingerprint(STAND_IN_DESK) != fingerprint(STAND_IN_REHEARSAL)


def test_the_running_checkout_is_not_treated_as_a_sandbox():
    from src.data_paths import repo_root

    assert writes_outside_own_checkout(repo_root()) is False
