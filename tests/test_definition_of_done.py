"""The definition-of-done gate — the four checks, and the proof each fails.

`scripts/definition_of_done.py` carries the reasoning; this file is the
part branch protection runs. Two kinds of test live here and the
distinction matters:

  * CONSTRUCTED VIOLATIONS. Each check is handed a change that breaks it
    and must report; handed the compliant version of the same change, it
    must be silent. A gate nobody has watched go red is a gate nobody
    knows the shape of. These build real throwaway git repositories where
    the check reads git, rather than mocking git, because the thing under
    test IS the git read.

  * THE LIVE GATE. Four tests run the checks against THIS change and fail
    the build. That is where blocking happens: `pytest` is the only check
    branch protection requires.

The live four are silent when no base commit can be read, matching
`scripts/definition_of_done.py`. The board-size check in
`tests/test_status_board.py` raises instead, and a required check that
fails for its own infrastructure reasons is a known problem on this desk;
this file does not add a second one. The cost is stated in the module: on
a clone with no reachable base, the gate does not run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import definition_of_done as dod

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# fixtures — real repositories, because the checks really read git
# ---------------------------------------------------------------------------

_ENV = {
    "GIT_AUTHOR_NAME": "dod-test", "GIT_AUTHOR_EMAIL": "dod@example.com",
    "GIT_COMMITTER_NAME": "dod-test", "GIT_COMMITTER_EMAIL": "dod@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, env=_ENV)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    return repo


def _commit(repo: Path, message: str, *paths: str) -> None:
    for rel in paths:
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", message)


def _board(items: str, retired: str) -> str:
    return (
        "## THE FUNNEL QUEUE\n\n"
        f"{items}\n\n"
        f"**Retired item numbers — never reuse.** {retired} were deleted.\n"
    )


def _change(repo: Path, base: str) -> dod.Change:
    """The same object the live gate builds, for a throwaway repository."""
    return dod.Change(
        base=base,
        paths=dod.changed_paths(base, repo),
        messages=dod.commit_messages(base, repo),
        work_md_before=dod.file_at(base, dod.WORK_MD, repo),
        work_md_after=(repo / dod.WORK_MD).read_text()
        if (repo / dod.WORK_MD).exists() else None,
        tree=repo,
    )



def _change_retiring_an_item(messages: str, tmp_path: Path | None = None) -> dod.Change:
    """A Change that retires one item, so the observable check arms.

    The tree is this repository, so a path cited in `messages` resolves the
    way it does in the live gate.
    """
    return dod.Change(
        base="BASE",
        paths=["docs/WORK.md"],
        messages=dod.unwrap_trailers(messages),
        work_md_before=_board("**7. Thing — OPEN.**", "1, 2"),
        work_md_after=_board("", "1, 2, 7"),
        tree=Path(__file__).resolve().parents[1],
    )


def _base_with_board(tmp_path: Path, items: str, retired: str = "1, 2") -> tuple[Path, str]:
    repo = _repo(tmp_path)
    _write(repo, dod.WORK_MD, _board(items, retired))
    _commit(repo, "base board", dod.WORK_MD)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    return repo, base


# ---------------------------------------------------------------------------
# the board's own shapes — parsed, not assumed
# ---------------------------------------------------------------------------

def test_the_real_board_parses_into_items_and_retired_numbers():
    """Every check is built on these two reads of the live docs/WORK.md.

    Pinned against the real file rather than a fixture: if the board's
    heading or retired-line shape ever changes, this fails here instead of
    the whole gate silently matching nothing and passing everything.
    """
    work_md = (REPO / dod.WORK_MD).read_text()
    blocks = dod.item_blocks(work_md)
    assert len(blocks) >= 10, "docs/WORK.md item headings no longer parse"
    retired = dod.retired_numbers(work_md)
    assert len(retired) >= 10, "docs/WORK.md retired-numbers line no longer parses"
    assert not (set(blocks) & retired), (
        "an item number is both live in docs/WORK.md and on the retired line; "
        "items_closed() would mis-read a diff"
    )


def test_criteria_parsing_stops_at_the_end_of_the_bullets():
    block = (
        "**80. Something — OPEN.**\n\n"
        "DONE WHEN:\n"
        "  - [x] the counter is weekend-aware\n"
        "  - [ ] every reader of it is switched over\n"
        "\n"
        "Some prose below that is not a criterion.\n"
        "  - [ ] and a stray bullet that belongs to the prose\n"
    )
    found = dod.criteria(block)
    assert [(o, met) for o, met, _ in found] == [(1, True), (2, False)]


# ---------------------------------------------------------------------------
# CHECK 1 — declared halves
# ---------------------------------------------------------------------------

def test_filing_an_item_without_criteria_fails(tmp_path):
    repo, base = _base_with_board(tmp_path, "**79. Existing — OPEN.**\n\nProse.")
    _write(repo, dod.WORK_MD, _board(
        "**79. Existing — OPEN.**\n\nProse.\n\n"
        "**80. New work — OPEN, filed today.**\n\nSome prose and no criteria.",
        "1, 2"))
    _commit(repo, "file item 80", dod.WORK_MD)
    problems = dod.declared_criteria_problems(_change(repo, base))
    assert len(problems) == 1 and "item 80" in problems[0]
    assert "DONE WHEN" in problems[0]


def test_filing_an_item_with_criteria_passes(tmp_path):
    repo, base = _base_with_board(tmp_path, "**79. Existing — OPEN.**\n\nProse.")
    _write(repo, dod.WORK_MD, _board(
        "**79. Existing — OPEN.**\n\nProse.\n\n"
        "**80. New work — OPEN, filed today.**\n\n"
        "DONE WHEN:\n"
        "  - [ ] the counter is weekend-aware\n"
        "  - [ ] every reader of it is switched over\n", "1, 2"))
    _commit(repo, "file item 80", dod.WORK_MD)
    assert dod.declared_criteria_problems(_change(repo, base)) == []


def test_an_owner_question_may_declare_no_criteria(tmp_path):
    """"He rules or he does not" has no half to leave behind.

    The exemption is a line in the diff, so choosing it is visible in
    review rather than being the default.
    """
    repo, base = _base_with_board(tmp_path, "**79. Existing — OPEN.**\n\nProse.")
    _write(repo, dod.WORK_MD, _board(
        "**79. Existing — OPEN.**\n\nProse.\n\n"
        "**80. Should the desk have one drawdown response or two — OWNER CALL.**\n\n"
        "NO CRITERIA: this is a ruling only the owner can give and nothing "
        "blocks on it.\n", "1, 2"))
    _commit(repo, "file item 80", dod.WORK_MD)
    assert dod.declared_criteria_problems(_change(repo, base)) == []


def test_closing_an_item_and_dropping_a_criterion_fails(tmp_path):
    """The whole point. Item 80 declared two halves, one shipped, and the
    change retires the number. The unmentioned half must not be allowed to
    quietly become permanent."""
    repo, base = _base_with_board(tmp_path,
        "**80. Holding time — OPEN.**\n\n"
        "DONE WHEN:\n"
        "  - [ ] the counter is weekend-aware\n"
        "  - [ ] every reader of it is switched over\n")
    _write(repo, dod.WORK_MD, _board("", "1, 2, 80"))
    _commit(repo, "close item 80\n\nDone-criteria-met: 80/1\n", dod.WORK_MD)
    problems = dod.declared_criteria_problems(_change(repo, base))
    assert len(problems) == 1
    assert "criterion 2" in problems[0] and "neither way" in problems[0]


def test_closing_an_item_accounting_for_both_halves_passes(tmp_path):
    repo, base = _base_with_board(tmp_path,
        "**80. Holding time — OPEN.**\n\n"
        "DONE WHEN:\n"
        "  - [ ] the counter is weekend-aware\n"
        "  - [ ] every reader of it is switched over\n")
    _write(repo, dod.WORK_MD, _board(
        "**81. Readers of the session counter — OPEN, carried from item 80.**\n\n"
        "DONE WHEN:\n  - [ ] every reader of it is switched over\n", "1, 2, 80"))
    _commit(repo,
            "close item 80\n\n"
            "Done-criteria-met: 80/1\n"
            "Done-criteria-deferred: 80/2 -> item 81 (2026-09-18)\n",
            dod.WORK_MD)
    assert dod.declared_criteria_problems(_change(repo, base)) == []


def test_deferring_a_criterion_onto_an_item_that_does_not_exist_fails(tmp_path):
    """A deferral has to land somewhere. Naming an item that was never
    filed is the same silent limbo with a reference number on it."""
    repo, base = _base_with_board(tmp_path,
        "**80. Holding time — OPEN.**\n\n"
        "DONE WHEN:\n  - [ ] the counter is weekend-aware\n")
    _write(repo, dod.WORK_MD, _board("", "1, 2, 80"))
    _commit(repo,
            "close item 80\n\n"
            "Done-criteria-deferred: 80/1 -> item 99 (2026-09-18)\n",
            dod.WORK_MD)
    problems = dod.declared_criteria_problems(_change(repo, base))
    assert len(problems) == 1 and "item 99" in problems[0]


def test_a_grandfathered_item_without_criteria_closes_freely(tmp_path):
    """Deliberate, and the reason this check has almost no bite today: an
    item that carried no criteria at the base is not held to them. Forcing
    the existing board through a new schema in one change is how a gate
    gets disabled."""
    repo, base = _base_with_board(tmp_path, "**80. Old item — OPEN.**\n\nProse only.")
    _write(repo, dod.WORK_MD, _board("", "1, 2, 80"))
    _commit(repo, "close item 80", dod.WORK_MD)
    assert dod.declared_criteria_problems(_change(repo, base)) == []


# ---------------------------------------------------------------------------
# CHECK 2 — consumer completeness
# ---------------------------------------------------------------------------

_DEFINITION = '''\
"""A shared quantity."""


def sessions_held(start, end):
    return 1
'''

_CONSUMER = '''\
from src.calendar_mod import sessions_held


def widen_band(entry):
    return sessions_held(entry, entry)
'''

_SECOND_CONSUMER = '''\
from src.calendar_mod import sessions_held


def pace(entry):
    return sessions_held(entry, entry) / 2
'''


def _quantity() -> dod.SharedQuantity:
    return dod.SharedQuantity(
        name="holding time, in sessions", symbol="sessions_held",
        defined_in="src/calendar_mod.py", why="a test fixture")


def _consumer_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = _repo(tmp_path)
    _write(repo, "src/calendar_mod.py", _DEFINITION)
    _write(repo, "src/band.py", _CONSUMER)
    _write(repo, "src/pace.py", _SECOND_CONSUMER)
    _write(repo, dod.WORK_MD, _board("**79. Item — OPEN.**\n\nProse.", "1, 2"))
    _commit(repo, "base", "src", dod.WORK_MD)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    return repo, base


def test_the_consumer_set_is_derived_from_the_tree(tmp_path):
    """Not from anything the author writes down. This is what makes the
    check's enumeration verifiable rather than another stale artefact."""
    repo, _ = _consumer_repo(tmp_path)
    assert set(dod.derive_consumers(_quantity(), repo)) == {
        "src/band.py:widen_band", "src/pace.py:pace"}


def test_changing_a_shared_quantity_and_missing_a_consumer_fails(
        tmp_path, monkeypatch):
    """The measured defect, reproduced: the definition changes, ONE reader
    is switched over, and the other keeps the old units."""
    repo, base = _consumer_repo(tmp_path)
    _write(repo, "src/calendar_mod.py",
           _DEFINITION.replace("return 1", "return 2  # weekend-aware now"))
    # Edited INSIDE widen_band, which is what makes it an accounted-for
    # consumer. Appending a comment at the end of the same file is not.
    _write(repo, "src/band.py",
           _CONSUMER.replace("return sessions_held(entry, entry)",
                             "return sessions_held(entry, entry)  # switched over"))
    _commit(repo, "make the counter weekend-aware", "src")
    monkeypatch.setattr(dod, "SHARED_QUANTITIES", (_quantity(),))
    problems = dod.consumer_completeness_problems(_change(repo, base))
    assert len(problems) == 1
    assert "src/pace.py:pace" in problems[0]
    assert "src/band.py" not in problems[0]


def test_touching_a_consumers_file_elsewhere_does_not_account_for_it(
        tmp_path, monkeypatch):
    """The property the whole check rests on, and the one that nearly did
    not ship.

    Replayed against cd7aec09 — the real 2026-09-04 commit that added the
    weekend-aware session counter and switched one of its readers — a
    FILE-granularity version of this check reported nothing at all. Every
    reader that was left on calendar days lived in `src/pipeline.py`, and
    so did the edit. Accounting has to be per site or it is theatre.
    """
    repo, base = _consumer_repo(tmp_path)
    _write(repo, "src/calendar_mod.py",
           _DEFINITION.replace("return 1", "return 2"))
    _write(repo, "src/band.py",
           _CONSUMER + "\n\ndef something_else():\n    return None\n")
    _write(repo, "src/pace.py",
           _SECOND_CONSUMER.replace("return sessions_held(entry, entry) / 2",
                                    "return sessions_held(entry, entry)"))
    _commit(repo, "make the counter weekend-aware", "src")
    monkeypatch.setattr(dod, "SHARED_QUANTITIES", (_quantity(),))
    problems = dod.consumer_completeness_problems(_change(repo, base))
    assert len(problems) == 1
    assert "src/band.py:widen_band" in problems[0], problems
    assert "src/pace.py" not in problems[0], problems


def test_accounting_for_the_untouched_consumer_passes(tmp_path, monkeypatch):
    repo, base = _consumer_repo(tmp_path)
    _write(repo, "src/calendar_mod.py",
           _DEFINITION.replace("return 1", "return 2"))
    # Edited INSIDE widen_band, which is what makes it an accounted-for
    # consumer. Appending a comment at the end of the same file is not.
    _write(repo, "src/band.py",
           _CONSUMER.replace("return sessions_held(entry, entry)",
                             "return sessions_held(entry, entry)  # switched over"))
    _commit(repo, "make the counter weekend-aware\n\n"
                  "Consumers-unchanged: src/pace.py:pace — pace divides by a "
                  "session horizon already, so the new units are what it "
                  "wanted all along.\n", "src")
    monkeypatch.setattr(dod, "SHARED_QUANTITIES", (_quantity(),))
    assert dod.consumer_completeness_problems(_change(repo, base)) == []


def test_a_declaration_that_does_not_match_the_tree_fails(tmp_path, monkeypatch):
    """A list nobody checks is what this gate is replacing, so the list is
    checked in BOTH directions. Naming a site in the right file that does
    not read the quantity is a stale or invented entry, and it fails even
    though it looks like compliance."""
    repo, base = _consumer_repo(tmp_path)
    _write(repo, "src/calendar_mod.py",
           _DEFINITION.replace("return 1", "return 2"))
    _commit(repo, "make the counter weekend-aware\n\n"
                  "Consumers-unchanged: src/pace.py:some_function_that_moved — "
                  "checked, fine.\n"
                  "Consumers-unchanged: src/band.py:widen_band — fine too "
                  "because the band wanted sessions.\n", "src")
    monkeypatch.setattr(dod, "SHARED_QUANTITIES", (_quantity(),))
    problems = dod.consumer_completeness_problems(_change(repo, base))
    assert any("no longer reads" in p for p in problems), problems
    assert any("src/pace.py:pace" in p for p in problems), problems


def test_editing_the_definitions_file_without_touching_the_symbol_is_quiet(
        tmp_path, monkeypatch):
    """The false-positive control. `src/pipeline.py` is eleven thousand
    lines; arming this check on every edit to a file that happens to
    contain a registered symbol would make it noise inside a week."""
    repo, base = _consumer_repo(tmp_path)
    _write(repo, "src/calendar_mod.py",
           _DEFINITION + "\n\ndef unrelated():\n    return None\n")
    _commit(repo, "add an unrelated helper", "src")
    monkeypatch.setattr(dod, "SHARED_QUANTITIES", (_quantity(),))
    assert dod.consumer_completeness_problems(_change(repo, base)) == []


def test_a_hand_rolled_rival_of_the_quantity_is_found_without_being_called(
        tmp_path, monkeypatch):
    """A site that computes the quantity by hand calls nothing, so a
    caller-only search is blind to it — and that blindness IS the measured
    defect. See `RivalShape`."""
    repo, base = _consumer_repo(tmp_path)
    # The rival site PRE-DATES the change, which is the real situation: a
    # newly added file is entirely inside the diff and so is accounted for
    # by construction.
    _write(repo, "src/legacy.py",
           "def old_pace(today, entry):\n"
           "    days_held = (today - entry).days\n"
           "    return days_held\n")
    _commit(repo, "a site that counts holding time by hand", "src")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True,
                          check=True).stdout.strip()
    _write(repo, "src/band.py",
           _CONSUMER.replace("return sessions_held(entry, entry)",
                             "return sessions_held(entry, entry)  # switched"))
    _write(repo, "src/pace.py",
           _SECOND_CONSUMER.replace("sessions_held(entry, entry) / 2",
                                    "sessions_held(entry, entry)"))
    _write(repo, "src/calendar_mod.py",
           _DEFINITION.replace("return 1", "return 2"))
    _commit(repo, "make the counter weekend-aware", "src")
    quantity = dod.SharedQuantity(
        name="holding time, in sessions", symbol="sessions_held",
        defined_in="src/calendar_mod.py", why="a test fixture",
        rival=dod.RivalShape(target=r"^days_held$", attribute="days",
                             describe="a calendar-day subtraction"))
    monkeypatch.setattr(dod, "SHARED_QUANTITIES", (quantity,))
    assert set(dod.derive_rivals(quantity, repo)) == {"src/legacy.py:old_pace"}
    problems = dod.consumer_completeness_problems(_change(repo, base))
    assert len(problems) == 1
    assert "src/legacy.py:old_pace" in problems[0]
    assert "do not call it at all" in problems[0]


def test_the_rival_matcher_stays_off_a_different_calendar_quantity():
    """The false-positive control for the rival shape, measured against the
    real tree. `(event_date - today).days` in `src/data/event_calendar.py`
    is a perfectly good calendar-day count of a DIFFERENT quantity; a
    matcher that flagged it is the one that gets this gate deleted.
    """
    quantity = next(q for q in dod.SHARED_QUANTITIES
                    if q.symbol == "trading_sessions_held")
    sites = dod.derive_rivals(quantity, REPO)
    assert sites, "the rival matcher matches nothing, so it checks nothing"
    files = {s.split(":")[0] for s in sites}
    assert files == {"src/pipeline.py"}, (
        f"the holding-time rival shape reaches beyond src/pipeline.py: "
        f"{sorted(sites)}. Narrow it, or the first unrelated calendar-day "
        f"count added anywhere in src/ blocks somebody's unrelated change."
    )


def test_the_gate_blocks_the_commit_that_created_the_measured_leftover():
    """The historical proof, and the reason check 2 exists in this shape.

    cd7aec09 (2026-09-04, "Three independent exit-management fixes from
    tonight's audit") added `trading_sessions_held` and switched ONE of its
    readers. Two sites kept computing holding time from calendar days, and
    one of them still divides that by a session horizon on main today.

    Replaying the gate against that commit is the only test here that can
    say the gate would have caught a real defect rather than a fixture.
    Skipped rather than failed when the commit is unreachable — a shallow
    CI clone is not evidence about the gate, and a required check that
    fails for its own infrastructure reasons is a known problem here.
    """
    reachable = subprocess.run(
        ["git", "-C", str(REPO), "cat-file", "-e", "cd7aec09^{commit}"],
        capture_output=True, text=True, check=False)
    if reachable.returncode != 0:
        pytest.skip("cd7aec09 is not in this clone")
    parent = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "cd7aec09^1"],
        capture_output=True, text=True, check=False).stdout.strip()
    if not parent:
        pytest.skip("cd7aec09's parent is not in this clone")

    def show(ref: str) -> str | None:
        r = subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:src/trading_calendar.py"],
                           capture_output=True, text=True, check=False)
        return r.stdout if r.returncode == 0 else None

    before, after = show(parent), show("cd7aec09")
    assert before is not None and after is not None
    assert dod._segment(before, "trading_sessions_held") is None
    assert dod._segment(after, "trading_sessions_held") is not None, (
        "cd7aec09 is not the commit that added trading_sessions_held; the "
        "historical proof below is measuring the wrong change"
    )
    # The two sites the commit left behind, read out of ITS tree by the same
    # rival matcher the live gate uses. Both are still on main today, which
    # is what makes this a leftover rather than a historical curiosity.
    quantity = next(q for q in dod.SHARED_QUANTITIES
                    if q.symbol == "trading_sessions_held")
    live_rivals = set(dod.derive_rivals(quantity, REPO))
    assert "src/pipeline.py:_build_position_history" in live_rivals
    assert "src/pipeline.py:_build_thesis_health_context" in live_rivals


def test_every_registered_quantity_still_exists_where_it_says():
    """Registry rot. A registered symbol that has been renamed or moved
    makes this check silently match nothing and pass everything, which is
    the failure mode of every gate this desk has disabled."""
    for quantity in dod.SHARED_QUANTITIES:
        path = REPO / quantity.defined_in
        assert path.exists(), f"{quantity.defined_in} is gone"
        assert dod._segment(path.read_text(), quantity.symbol) is not None, (
            f"{quantity.symbol} is no longer defined in "
            f"{quantity.defined_in}; update SHARED_QUANTITIES or the check "
            f"for {quantity.name} is dead"
        )
        assert dod.derive_consumers(quantity, REPO), (
            f"{quantity.symbol} has no derivable consumer in src/, so "
            f"registering it checks nothing"
        )


# ---------------------------------------------------------------------------
# CHECK 3 — a falsifiable adversary record
# ---------------------------------------------------------------------------

_GOOD_RECORD = (
    "Objection-1: the gate forces a twenty-minute ceremony onto a one-line "
    "typo fix and will be disabled within a week.\n"
    "Response-1: CHANGED docs/WORK.md — every check is now diff-scoped and "
    "fires only on a filing, a closure or a registered quantity.\n"
    "Objection-2: a derived consumer set misses any read done through "
    "getattr and so under-fires exactly where it matters.\n"
    "Response-2: REJECTED — under-firing is the direction that costs a "
    "missed catch rather than a disabled gate, and the module says so "
    "instead of claiming coverage it does not have.\n"
    "Acceptance-observable: the next closure lands with its objections in "
    "the commit message and a reader can check Response-1 against the diff "
    "in docs/WORK.md\n"
)


def _closure(tmp_path: Path, message: str) -> dod.Change:
    repo, base = _base_with_board(tmp_path, "**80. Item — OPEN.**\n\nProse.")
    _write(repo, dod.WORK_MD, _board("", "1, 2, 80"))
    _commit(repo, message, dod.WORK_MD)
    return _change(repo, base)


def test_a_closure_with_no_objections_fails(tmp_path):
    change = _closure(tmp_path, "close item 80\n\nAdversary: argued at length "
                                "that this was a bad idea and I disagreed.\n")
    problems = dod.adversary_record_problems(change)
    assert any("0 adversary objection" in p for p in problems), problems


def test_the_line_that_passes_the_existing_check_does_not_pass_this_one(tmp_path):
    """`scripts/work_queue.py` accepts any `Adversary:` line of six words.
    That line is the thing being replaced, so it must fail here."""
    six_words = "Adversary: argued the unification hides a real regression"
    change = _closure(tmp_path, f"close item 80\n\n{six_words}\n")
    from scripts import work_queue
    assert work_queue.has_adversary_evidence(six_words), (
        "the old presence check no longer accepts its own passing case; "
        "this test is comparing against nothing"
    )
    assert dod.adversary_record_problems(change)


def test_a_changed_claim_citing_a_file_the_diff_never_touched_fails(tmp_path):
    """The load-bearing falsification. "I changed something in response" is
    the claim most worth making falsely, and the only one a machine catches
    red-handed."""
    change = _closure(tmp_path, "close item 80\n\n" + _GOOD_RECORD.replace(
        "CHANGED docs/WORK.md", "CHANGED src/pipeline.py"))
    problems = dod.adversary_record_problems(change)
    assert any("touches none of those files" in p for p in problems), problems


def test_a_changed_claim_citing_no_path_at_all_fails(tmp_path):
    change = _closure(tmp_path, "close item 80\n\n" + _GOOD_RECORD.replace(
        "CHANGED docs/WORK.md —", "CHANGED —"))
    problems = dod.adversary_record_problems(change)
    assert any("cites no path" in p for p in problems), problems


def test_two_copies_of_one_objection_are_one_objection(tmp_path):
    repeated = (
        "Objection-1: the gate forces a ceremony onto a one-line fix and "
        "will be routed around.\n"
        "Response-1: REJECTED — it is diff-scoped so a one-line fix that "
        "files and closes nothing is untouched by all four checks.\n"
        "Objection-2: the gate forces a ceremony onto a one-line fix and "
        "will be routed around.\n"
        "Response-2: REJECTED — same answer as above and the objection is "
        "the same objection written twice over.\n"
        "Acceptance-observable: the next closure carries a record a reader "
        "can check against the diff in docs/WORK.md\n"
    )
    problems = dod.adversary_record_problems(_closure(tmp_path,
                                                      "close item 80\n\n" + repeated))
    assert any("repeats Objection-1" in p for p in problems), problems


def test_an_unanswered_objection_fails(tmp_path):
    change = _closure(tmp_path, "close item 80\n\n" + _GOOD_RECORD.replace(
        "Response-2: REJECTED", "Note-2: rejected"))
    problems = dod.adversary_record_problems(change)
    assert any("has no `Response-2:`" in p for p in problems), problems


def test_a_one_word_rejection_fails(tmp_path):
    change = _closure(tmp_path, "close item 80\n\n" + _GOOD_RECORD.replace(
        "REJECTED — under-firing is the direction that costs a "
        "missed catch rather than a disabled gate, and the module says so "
        "instead of claiming coverage it does not have.", "REJECTED — no."))
    problems = dod.adversary_record_problems(change)
    assert any("rejects an objection in" in p for p in problems), problems


def test_a_complete_record_passes(tmp_path):
    change = _closure(tmp_path, "close item 80\n\n" + _GOOD_RECORD)
    assert dod.adversary_record_problems(change) == []
    assert dod.acceptance_observable_problems(change) == []


def test_a_change_that_closes_nothing_needs_no_record(tmp_path):
    """The ceremony bound. A change that files nothing, closes nothing and
    touches no registered quantity is subject to none of the four."""
    repo, base = _base_with_board(tmp_path, "**80. Item — OPEN.**\n\nProse.")
    _write(repo, "README.md", "a typo fix\n")
    _commit(repo, "fix a typo", "README.md")
    change = _change(repo, base)
    assert dod.run_all(change) == {name: [] for name in dod.CHECKS}


# ---------------------------------------------------------------------------
# CHECK 4 — acceptance observable
# ---------------------------------------------------------------------------

def test_a_closure_with_no_acceptance_observable_fails(tmp_path):
    change = _closure(tmp_path, "close item 80\n\n" + _GOOD_RECORD.replace(
        "Acceptance-observable:", "Notes:"))
    problems = dod.acceptance_observable_problems(change)
    assert len(problems) == 1 and "Acceptance-observable" in problems[0]


def test_an_observable_citing_a_path_that_does_not_exist_fails(tmp_path):
    change = _closure(tmp_path, "close item 80\n\n" + _GOOD_RECORD.replace(
        "in docs/WORK.md", "in src/does_not_exist.py"))
    problems = dod.acceptance_observable_problems(change)
    assert any("does not exist after this change" in p for p in problems), problems


def test_a_three_word_observable_fails(tmp_path):
    change = _closure(tmp_path, "close item 80\n\n" + _GOOD_RECORD.replace(
        _GOOD_RECORD[_GOOD_RECORD.index("Acceptance-observable:"):],
        "Acceptance-observable: it works, see src/pipeline.py\n"))
    problems = dod.acceptance_observable_problems(change)
    assert any("words" in p for p in problems), problems


# ---------------------------------------------------------------------------
# CHECK 4's other half — per-item deployment, pinned here, run on the box
# ---------------------------------------------------------------------------

def test_the_retiring_commit_of_an_item_is_found_from_the_retired_line_alone(
        tmp_path):
    """`scripts/check_item_deployment.py` decides from git reachability, so
    its one piece of interpretation is "which commit retired item N". That
    is derived from the retired-numbers line and no board prose, and it is
    pinned here because the script itself cannot run in CI — the production
    checkout is not reachable from a GitHub runner."""
    from scripts import check_item_deployment as cid

    repo, _ = _base_with_board(tmp_path, "**80. Item — OPEN.**\n\nProse.")
    _write(repo, dod.WORK_MD, _board("", "1, 2, 80"))
    _commit(repo, "close item 80", dod.WORK_MD)
    retiring = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True,
                              check=True).stdout.strip()
    _write(repo, "later.txt", "work that came after the closure\n")
    _commit(repo, "later work", "later.txt")
    _git(repo, "branch", "origin/main", "main")

    found = cid.retiring_commits(repo, ref="origin/main")
    assert found.get("80") == retiring, found

    deployed_before_the_fix = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD~2"],
        capture_output=True, text=True, check=True).stdout.strip()
    findings = cid.undeployed_closures(repo, deployed_before_the_fix,
                                       ref="origin/main")
    assert [f.item for f in findings] == ["80"]
    assert cid.undeployed_closures(repo, retiring, ref="origin/main") == []


# ---------------------------------------------------------------------------
# THE LIVE GATE — this is where the build goes red
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live_change() -> dod.Change | None:
    return dod.Change.from_git()


@pytest.mark.parametrize("check", sorted(dod.CHECKS))
def test_this_change_meets_the_definition_of_done(live_change, check):
    """The four checks, against THIS change.

    Silent when no base commit can be read — see this file's docstring for
    why that is a deliberate choice and what it costs.
    """
    if live_change is None:
        pytest.skip("no base commit to measure this change against")
    problems = dod.CHECKS[check](live_change)
    assert not problems, "\n".join(f"- {p}" for p in problems)


# ---------------------------------------------------------------------------
# A wrapped trailer is a typographic accident, not a failed declaration
# ---------------------------------------------------------------------------

def test_a_wrapped_trailer_is_joined_before_any_check_reads_it():
    """Four pull requests were re-cut on 2026-09-26 for this and nothing else.

    Every check captures a trailer's value to end-of-line, so a trailer written
    across several physical lines arrived truncated to its first line — an
    `Acceptance-observable:` wrapped at column 72 read as eight words with no
    path. Because the gate reads EVERY occurrence in `git log base..HEAD`, a
    later commit could not correct it, and with force-push blocked the only
    remedy was re-cutting the branch. The work was never the problem.
    """
    wrapped = (
        "Some subject line\n"
        "\n"
        "Acceptance-observable: tests/test_definition_of_done.py fails when a\n"
        "    trailer is wrapped across several physical lines instead of one\n"
        "Objection-1: joining continuation lines could swallow the next\n"
        "    trailer and hide a missing declaration\n"
        "Response-1: REJECTED a continuation must be indented and must not itself "
        "look like a key, so the next trailer always ends the one above it\n"
    )
    joined = dod.unwrap_trailers(wrapped)
    observable = dod.trailer(joined, "Acceptance-observable")
    assert len(observable) == 1
    assert "physical lines instead of one" in observable[0]
    assert len(observable[0].split()) >= 10
    objections = dod.OBJECTION.findall(joined)
    assert len(objections) == 1
    assert "hide a missing declaration" in objections[0][1]
    assert len(dod.RESPONSE.findall(joined)) == 1


def test_unwrapping_leaves_an_unwrapped_message_byte_identical():
    """The normaliser must be invisible to every message that did not need it."""
    plain = (
        "Subject\n"
        "\n"
        "A paragraph of ordinary prose that happens to mention a ratio of 2:1\n"
        "and continues on the next line without being a trailer at all.\n"
        "\n"
        "Acceptance-observable: one physical line citing scripts/definition_of_done.py "
        "and carrying well over the ten words this check requires\n"
    )
    assert dod.unwrap_trailers(plain) == plain


def test_a_continuation_never_swallows_the_next_trailer():
    """An indented line that is itself `Key:` starts a new trailer, not a tail."""
    text = (
        "Objection-1: the first argument runs to a reasonable length here\n"
        "    Objection-2: an indented second objection is still its own trailer\n"
    )
    assert len(dod.OBJECTION.findall(dod.unwrap_trailers(text))) == 2


def test_an_unindented_continuation_is_joined_too():
    """The indented-only rule rescued none of the five PRs it shipped for.

    Git's trailer convention says a continuation is indented. Nobody writing
    these indents them, so the first version of `unwrap_trailers` described a
    convention this desk does not follow and left every blocked pull request
    exactly where it was. A rule nobody follows is not a rule.
    """
    wrapped = (
        "Subject\n"
        "\n"
        "Acceptance-observable: tests/test_definition_of_done.py refuses a\n"
        "declaration whose first physical line is too short to carry a claim\n"
    )
    values = dod.trailer(dod.unwrap_trailers(wrapped), "Acceptance-observable")
    assert len(values) == 1
    assert values[0].endswith("carry a claim")
    assert len(values[0].split()) >= 10


def test_prose_after_a_non_gate_colon_line_is_left_alone():
    """Joining is restricted to the keys this module reads.

    A commit body is full of colons. If any `Word:` line could absorb the
    sentence under it, the normaliser would be silently rewriting prose it has
    no business touching — so only the gate's own keys pull a continuation.
    """
    text = (
        "Note: this paragraph explains the change\n"
        "and continues on a second line that must not be joined.\n"
    )
    assert dod.unwrap_trailers(text) == text


def test_a_blank_line_ends_a_continuation():
    """Otherwise one trailer would swallow the whole rest of the message."""
    text = (
        "Objection-1: the first argument is long enough to clear the floor\n"
        "and wraps onto this line\n"
        "\n"
        "An unrelated paragraph that belongs to nobody.\n"
    )
    joined = dod.unwrap_trailers(text)
    objections = dod.OBJECTION.findall(joined)
    assert len(objections) == 1
    assert "wraps onto this line" in objections[0][1]
    assert "unrelated paragraph" not in objections[0][1]


def test_one_compliant_observable_rescues_a_weaker_one_beside_it():
    """A later commit must be able to repair an earlier one.

    Requiring EVERY occurrence to pass, on a repository where force-push is
    blocked, meant one weak sentence in a branch's first commit condemned the
    whole branch to being re-cut. That cost nine pull requests on 2026-09-26,
    four of them carrying observables genuinely better than the rule — they
    named the rendered text, the log row and the database row a reader would
    actually look at, and cited no file. The claim this check enforces is that
    the change declares SOMETHING confirmable; one declaration establishes it.
    """
    weak = "Acceptance-observable: the ranking reads better than it did before"
    good = (
        "Acceptance-observable: tests/test_definition_of_done.py fails when a "
        "closure declares nothing a human could confirm after a live session"
    )
    change = _change_retiring_an_item(messages=weak + "\n" + good + "\n")
    assert dod.acceptance_observable_problems(change) == []


def test_every_weak_observable_is_reported_when_none_qualifies():
    """The messages are the author's guide, so they must all still appear."""
    messages = (
        "Acceptance-observable: too short to count here\n"
        "Acceptance-observable: long enough to clear the word floor easily but "
        "naming no file anywhere in this repository at all\n"
    )
    problems = dod.acceptance_observable_problems(
        _change_retiring_an_item(messages=messages))
    assert len(problems) == 2
    assert any("words" in p for p in problems)
    assert any("cites no path" in p for p in problems)
