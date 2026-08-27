"""TEMPORARY — proves the CI gate blocks a failing test. Delete after verification."""


def test_ci_gate_must_block_this():
    assert 1 == 2, "deliberate failure to prove CI blocks bad merges"
