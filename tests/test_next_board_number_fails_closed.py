"""`scripts/next_board_number.py` must refuse rather than guess.

THE FAILURE THESE PIN. On 2026-09-30 the script could not read the open
pull requests (a GitHub request limit), printed a WARNING, and returned a
number with exit 0. Two pull requests then both claimed item 192 and a
human caught it by hand. The open-PR read is not a nicety on the side of
this check — it is the half that catches the parallel-agent race, because
a number is claimed on a branch long before it reaches `docs/WORK.md`. A
tool that cannot run that half has nothing safe to say.

The desk's rule behind this: everything mechanically enforced holds,
everything relying on somebody noticing a warning slips. The old output
relied on somebody noticing a warning.
"""

from __future__ import annotations

import pytest

from scripts import next_board_number
from scripts.board_numbers import OpenPrClaims

#: Enough of the real file's shape for `next_free_number` to work on:
#: one live item and an append-only retired-numbers line.
WORK_MD = """
**7. Something live**

**Retired item numbers — never reuse.**

- retired queue: 3, 5
"""


@pytest.fixture()
def work_md(tmp_path):
    path = tmp_path / "WORK.md"
    path.write_text(WORK_MD)
    return path


def _run(work_md, monkeypatch, claims, *extra):
    monkeypatch.setattr(next_board_number, "read_open_pr_claims", lambda *_a, **_k: claims)
    return next_board_number.main(["--work-md", str(work_md), *extra])


def test_an_unreadable_pr_list_exits_nonzero_and_prints_no_number(work_md, monkeypatch, capsys):
    """The exact 2026-09-30 failure: the read fails, so nothing is offered."""
    code = _run(work_md, monkeypatch, OpenPrClaims(problem="API rate limit exceeded"))
    assert code != 0
    out = capsys.readouterr()
    assert "Next free board item number" not in out.out
    # The number itself must not appear anywhere a caller could lift it.
    assert "8" not in out.out
    assert "could not be read" in out.err
    assert "API rate limit exceeded" in out.err


@pytest.mark.parametrize(
    "problem",
    [
        "API rate limit exceeded",
        "connection refused",
        "HTTP 401 Bad credentials",
    ],
)
def test_it_fails_closed_for_every_kind_of_read_failure(work_md, monkeypatch, capsys, problem):
    """Rate limit, network, auth — the caller's exposure is identical."""
    assert _run(work_md, monkeypatch, OpenPrClaims(problem=problem)) != 0
    assert problem in capsys.readouterr().err


def test_a_successful_read_still_prints_a_number(work_md, monkeypatch, capsys):
    """Failing closed must not mean failing always."""
    code = _run(work_md, monkeypatch, OpenPrClaims(by_pr={701: {9}}))
    assert code == 0
    out = capsys.readouterr().out
    # 9 is claimed by an open PR, so the next free number is 10.
    assert "Next free board item number: 10" in out
    assert "UNCHECKED" not in out


def test_the_opt_out_prints_the_number_labelled_unchecked(work_md, monkeypatch, capsys):
    """The offline escape hatch exists, and it says what it handed over.

    The label sits on the number's own line so it survives being pasted
    somewhere else — a caveat in a later paragraph does not.
    """
    called = []
    monkeypatch.setattr(next_board_number, "read_open_pr_claims", lambda *_a, **_k: called.append(1))
    code = next_board_number.main(["--work-md", str(work_md), "--accept-unchecked-number"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Next free board item number (UNCHECKED): 8" in out
    assert "may already have claimed this number" in out
    assert not called, "the opt-out must not call GitHub at all"


def test_the_old_flag_name_is_gone_rather_than_kept_as_an_alias(work_md):
    """`--no-github` named the plumbing, so it read as a speed switch.

    Keeping it as an alias would leave the casual escape hatch open
    beside the deliberate one, which is the whole thing being closed
    here. Pinned rather than trusted: argparse must reject it outright.
    """
    with pytest.raises(SystemExit) as exc:
        next_board_number.main(["--work-md", str(work_md), "--no-github"])
    assert exc.value.code != 0
