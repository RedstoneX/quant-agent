"""The advisory half: does THIS open PR's new board item number collide with
another currently open PR's? See `scripts/check_board_number_claims.py`'s
module docstring for why this is advisory, never a required check."""

from __future__ import annotations

from scripts.check_board_number_claims import find_collisions


def test_a_shared_number_between_two_other_prs_is_found():
    claims = {700: {188}, 701: {188}, 702: {200}}
    out = find_collisions(700, claims)
    assert len(out) == 1
    assert "PR 700 and PR 701" in out[0]
    assert "188" in out[0]


def test_no_collision_when_numbers_are_disjoint():
    claims = {700: {188}, 701: {189}}
    assert find_collisions(700, claims) == []


def test_a_pr_is_never_compared_against_itself():
    claims = {700: {188}}
    assert find_collisions(700, claims) == []


def test_a_pr_with_no_claims_reports_nothing():
    claims = {701: {188}}
    assert find_collisions(700, claims) == []


def test_multiple_overlaps_are_all_reported():
    claims = {700: {188, 189}, 701: {188}, 702: {189}}
    out = find_collisions(700, claims)
    assert len(out) == 2
    joined = " ".join(out)
    assert "188" in joined and "189" in joined


def test_missing_dependency_is_a_problem_not_a_traceback(monkeypatch):
    import builtins
    import sys

    from scripts.board_numbers import read_open_pr_claims

    monkeypatch.delitem(sys.modules, "src.inflight", raising=False)
    real = builtins.__import__

    def deny(name, *a, **k):
        if name == "src.inflight":
            raise ModuleNotFoundError("No module named 'requests'")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", deny)
    claims = read_open_pr_claims()
    assert claims.problem and "requests" in claims.problem
