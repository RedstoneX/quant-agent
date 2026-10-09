"""CHECK 1 of the definition-of-done gate — the declared halves.

The constructed violations for `scripts/dod_declared_criteria.py`: each of
the three accountings a closure may use for a criterion, each shown passing
when it is honest and REFUSING when it is not. Split out of
`tests/test_definition_of_done.py` with the check itself; the fixtures and
the live gate stay there.
"""

from __future__ import annotations

from scripts import definition_of_done as dod
from tests.dod_repo_fixtures import BOARD, _base_with_board, _board, _change, _commit, _write


def test_filing_an_item_without_criteria_fails(tmp_path):
    repo, base = _base_with_board(tmp_path, "**79. Existing — OPEN.**\n\nProse.")
    _write(
        repo,
        BOARD,
        _board(
            "**79. Existing — OPEN.**\n\nProse.\n\n"
            "**80. New work — OPEN, filed today.**\n\nSome prose and no criteria.",
            "1, 2",
        ),
    )
    _commit(repo, "file item 80", BOARD)
    problems = dod.declared_criteria_problems(_change(repo, base))
    assert len(problems) == 1 and "item 80" in problems[0]
    assert "DONE WHEN" in problems[0]


def test_filing_an_item_with_criteria_passes(tmp_path):
    repo, base = _base_with_board(tmp_path, "**79. Existing — OPEN.**\n\nProse.")
    _write(
        repo,
        BOARD,
        _board(
            "**79. Existing — OPEN.**\n\nProse.\n\n"
            "**80. New work — OPEN, filed today.**\n\n"
            "DONE WHEN:\n"
            "  - [ ] the counter is weekend-aware\n"
            "  - [ ] every reader of it is switched over\n",
            "1, 2",
        ),
    )
    _commit(repo, "file item 80", BOARD)
    assert dod.declared_criteria_problems(_change(repo, base)) == []


def test_an_owner_question_may_declare_no_criteria(tmp_path):
    """ "He rules or he does not" has no half to leave behind.

    The exemption is a line in the diff, so choosing it is visible in
    review rather than being the default.
    """
    repo, base = _base_with_board(tmp_path, "**79. Existing — OPEN.**\n\nProse.")
    _write(
        repo,
        BOARD,
        _board(
            "**79. Existing — OPEN.**\n\nProse.\n\n"
            "**80. Should the desk have one drawdown response or two — OWNER CALL.**\n\n"
            "NO CRITERIA: this is a ruling only the owner can give and nothing "
            "blocks on it.\n",
            "1, 2",
        ),
    )
    _commit(repo, "file item 80", BOARD)
    assert dod.declared_criteria_problems(_change(repo, base)) == []


def test_closing_an_item_and_dropping_a_criterion_fails(tmp_path):
    """The whole point. Item 80 declared two halves, one shipped, and the
    change retires the number. The unmentioned half must not be allowed to
    quietly become permanent."""
    repo, base = _base_with_board(
        tmp_path,
        "**80. Holding time — OPEN.**\n\n"
        "DONE WHEN:\n"
        "  - [ ] the counter is weekend-aware\n"
        "  - [ ] every reader of it is switched over\n",
    )
    _write(repo, BOARD, _board("", "1, 2, 80"))
    _commit(repo, "close item 80\n\nDone-criteria-met: 80/1\n", BOARD)
    problems = dod.declared_criteria_problems(_change(repo, base))
    assert len(problems) == 1
    assert "criterion 2" in problems[0] and "neither way" in problems[0]


def test_closing_an_item_accounting_for_both_halves_passes(tmp_path):
    repo, base = _base_with_board(
        tmp_path,
        "**80. Holding time — OPEN.**\n\n"
        "DONE WHEN:\n"
        "  - [ ] the counter is weekend-aware\n"
        "  - [ ] every reader of it is switched over\n",
    )
    _write(
        repo,
        BOARD,
        _board(
            "**81. Readers of the session counter — OPEN, carried from item 80.**\n\n"
            "DONE WHEN:\n  - [ ] every reader of it is switched over\n",
            "1, 2, 80",
        ),
    )
    _commit(
        repo, "close item 80\n\nDone-criteria-met: 80/1\nDone-criteria-deferred: 80/2 -> item 81 (2026-09-18)\n", BOARD
    )
    assert dod.declared_criteria_problems(_change(repo, base)) == []


def test_deferring_a_criterion_onto_an_item_that_does_not_exist_fails(tmp_path):
    """A deferral has to land somewhere. Naming an item that was never
    filed is the same silent limbo with a reference number on it."""
    repo, base = _base_with_board(
        tmp_path, "**80. Holding time — OPEN.**\n\nDONE WHEN:\n  - [ ] the counter is weekend-aware\n"
    )
    _write(repo, BOARD, _board("", "1, 2, 80"))
    _commit(repo, "close item 80\n\nDone-criteria-deferred: 80/1 -> item 99 (2026-09-18)\n", BOARD)
    problems = dod.declared_criteria_problems(_change(repo, base))
    assert len(problems) == 1 and "item 99" in problems[0]


def _withdrawing_repo(tmp_path, note: str):
    """Item 80 with one criterion, retired, its note carrying `note`."""
    repo, base = _base_with_board(
        tmp_path,
        "**80. The rehearsal settles — OPEN.**\n\n"
        "DONE WHEN:\n  - [ ] a real rehearsal against the production snapshot\n",
    )
    _write(repo, BOARD, _board("", "1, 2, 80"))
    _write(repo, "docs/board_notes/item-80.md", note)
    return repo, base


def test_withdrawing_a_criterion_on_a_recorded_ruling_passes(tmp_path):
    """The third accounting. The owner ruled the work will not be done, the
    ruling is written down where the board keeps rulings, and the gate reads
    it rather than taking the author's word."""
    repo, base = _withdrawing_repo(
        tmp_path,
        "## item 80\n\n**OWNER RULING 2026-10-04 — the rehearsal rig is "
        "FROZEN; a second apparatus is not worth maintaining.**\n",
    )
    _commit(
        repo,
        "close item 80\n\nDone-criteria-withdrawn: 80/1 -> ruling 2026-10-04 (docs/board_notes/item-80.md)\n",
        BOARD,
        "docs/board_notes/item-80.md",
    )
    assert dod.declared_criteria_problems(_change(repo, base)) == []


def test_withdrawing_on_a_ruling_that_is_not_recorded_fails(tmp_path):
    """THE ANTI-GAMING PROOF. The whole value of this form is the citation,
    so a withdrawal whose ruling exists only in the trailer must be refused
    exactly as a deferral onto a non-existent item is."""
    repo, base = _withdrawing_repo(tmp_path, "## item 80\n\nOrdinary prose.\n")
    _commit(
        repo,
        "close item 80\n\nDone-criteria-withdrawn: 80/1 -> ruling 2026-10-04 (docs/board_notes/item-80.md)\n",
        BOARD,
        "docs/board_notes/item-80.md",
    )
    problems = dod.declared_criteria_problems(_change(repo, base))
    assert len(problems) == 1
    assert "no RULING line bearing that date" in problems[0]


def test_withdrawing_on_a_ruling_dated_differently_fails(tmp_path):
    """A real ruling does not license withdrawing against a date it does not
    carry; otherwise one old ruling would waive anything forever."""
    repo, base = _withdrawing_repo(tmp_path, "## item 80\n\n**OWNER RULING 2026-09-30 — something else.**\n")
    _commit(
        repo,
        "close item 80\n\nDone-criteria-withdrawn: 80/1 -> ruling 2026-10-04 (docs/board_notes/item-80.md)\n",
        BOARD,
        "docs/board_notes/item-80.md",
    )
    problems = dod.declared_criteria_problems(_change(repo, base))
    assert len(problems) == 1 and "2026-10-04" in problems[0]


def test_withdrawing_against_a_file_outside_the_decisions_record_fails(tmp_path):
    """A ruling the board does not carry cannot be checked by anyone later,
    so pointing at a scratch file in the same diff is not a citation."""
    repo, base = _withdrawing_repo(tmp_path, "## item 80\n\nProse.\n")
    _write(repo, "notes.md", "RULING 2026-10-04 — I say so.\n")
    _commit(
        repo,
        "close item 80\n\nDone-criteria-withdrawn: 80/1 -> ruling 2026-10-04 (notes.md)\n",
        BOARD,
        "docs/board_notes/item-80.md",
        "notes.md",
    )
    problems = dod.declared_criteria_problems(_change(repo, base))
    assert len(problems) == 1 and "decisions record" in problems[0]


def test_a_bare_withdrawal_with_no_citation_accounts_for_nothing(tmp_path):
    """An assertion is not a withdrawal. A trailer with no ruling citation
    does not parse as one, so the criterion stays unaccounted for and the
    original refusal fires."""
    repo, base = _withdrawing_repo(tmp_path, "## item 80\n\nProse.\n")
    _commit(
        repo, "close item 80\n\nDone-criteria-withdrawn: 80/1 not worth doing\n", BOARD, "docs/board_notes/item-80.md"
    )
    problems = dod.declared_criteria_problems(_change(repo, base))
    assert len(problems) == 1 and "neither way" in problems[0]


def test_a_grandfathered_item_without_criteria_closes_freely(tmp_path):
    """Deliberate, and the reason this check has almost no bite today: an
    item that carried no criteria at the base is not held to them. Forcing
    the existing board through a new schema in one change is how a gate
    gets disabled."""
    repo, base = _base_with_board(tmp_path, "**80. Old item — OPEN.**\n\nProse only.")
    _write(repo, BOARD, _board("", "1, 2, 80"))
    _commit(repo, "close item 80", BOARD)
    assert dod.declared_criteria_problems(_change(repo, base)) == []
