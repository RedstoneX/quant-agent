"""The work queue's job is to be RELUCTANT.

Every test below that matters is a test that it allowed the session to
stop. The owner's constraint is not "never miss an item" — it is "never
burn a session in a loop" — so the cases pinned hardest are the ones where
something is uncertain and the correct answer is to let go.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from scripts import turn_promises, work_queue
from scripts.status_board import QueueItem
from src.inflight import OpenPR, read_open_pull_requests


BACKLOG = """\
## THE FUNNEL QUEUE — why trades do not happen, ranked by measured cost

**7. Something nobody has started — 3 of 68 (4%). DEFECT.**

Body text.

**2. Something already being built — 5 of 68 (7%). DEFECT. IN FLIGHT.**

Body text.

**9. Something parked — 1 of 68 (1%). DEFECT. PARKED.**

Body text.

**4. Something finished but never ticked off — 2 of 68 (3%). FIXED 2026-09-01.**

Body text.

**~~6. Something finished and ticked off — 2 of 68 (3%). FIXED 2026-09-01.~~**

Body text.

**5. Something by design — 1 of 68 (1%). WORKING AS INTENDED.**

Body text.

## PM TEST GATE — garbage in, garbage out

**1. A gate item nobody has started — DEFECT.**

Body text.

<!-- END PM TEST GATE -->

## Something else
"""


@pytest.fixture
def work_md(tmp_path: Path) -> Path:
    p = tmp_path / "WORK.md"
    p.write_text(BACKLOG)
    return p


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "handbacks.json"


def _item(rank: int, headline: str) -> QueueItem:
    return QueueItem(rank=rank, title=headline, classification="", share="",
                     pct=None, done=False, headline=headline)


# --- classification --------------------------------------------------------

def test_the_four_answers_come_from_the_boards_own_buckets(work_md):
    q = work_queue.build_queue(work_md, board_notes=work_md.parent / "none.md")
    # 4 is finished by its own account but never struck through — the
    # write-up IS the work, and leaving it unmarked is the cause of the
    # owner's "déjà vu" complaint. So it is mine, not noise.
    # Both numbered sequences are read, and the gate item's identifier
    # keeps them apart — a funnel "1" and a gate "1" are different things.
    assert [i.ref for i in q.actionable] == [
        "item 4", "item 7", "gate item 1"]
    assert [i.rank for i in q.waiting_external] == [2]
    assert [i.rank for i in q.blocked_on_owner] == [9]
    # Struck through (6) and by-design (5) are nobody's. They vanish.
    assert not q.unreadable


def test_actionable_is_the_backlogs_own_order_not_mine():
    """The 35.5-hour change happened under 'newest first'. Lowest rank wins."""
    ordered = work_queue.classify([_item(9, "Nine — DEFECT."),
                                   _item(3, "Three — DEFECT."),
                                   _item(6, "Six — DEFECT.")])
    assert [i.rank for i in ordered["ACTIONABLE"]] == [3, 6, 9]


def test_an_unknown_bucket_surfaces_rather_than_disappearing(monkeypatch):
    """A bucket added to the board and forgotten here must show up as
    something to look at, never be silently dropped into 'nothing to do'."""
    monkeypatch.setitem(work_queue.BUCKET_OWNERSHIP, "open", "ACTIONABLE")
    item = _item(1, "One — DEFECT.")
    monkeypatch.setattr(type(item), "bucket",
                        property(lambda self: "a_bucket_invented_later"))
    assert work_queue.classify([item])["ACTIONABLE"] == [item]


def test_a_missing_backlog_is_reported_not_treated_as_finished(tmp_path):
    q = work_queue.build_queue(tmp_path / "gone.md", tmp_path / "gone.md")
    assert q.unreadable
    assert not q.actionable


# --- the decision ----------------------------------------------------------

def _queue(actionable=(), unreadable=()):
    return work_queue.Queue(actionable=list(actionable),
                            unreadable=list(unreadable))


def test_it_blocks_when_the_work_is_plainly_mine(state_path):
    d = work_queue.decide(_queue([_item(3, "Three — DEFECT.")]),
                          "sess", False, state_path=state_path,
                          projects_root=Path("/nonexistent"))
    assert d.block
    assert "item 3" in d.reason


def test_an_empty_actionable_list_lets_the_session_stop(state_path):
    assert not work_queue.decide(_queue(), "sess", False,
                                 state_path=state_path).block


def test_an_unreadable_backlog_lets_the_session_stop(state_path):
    """It cannot read the file, so it has no grounds to claim work is in it."""
    d = work_queue.decide(_queue(unreadable=["the shape changed"]),
                          "sess", False, state_path=state_path)
    assert not d.block
    assert "could not be read" in d.reason


def test_a_live_background_agent_lets_the_session_stop(tmp_path, state_path):
    """Blocking here re-sends the whole transcript at $1.59 a firing."""
    subs = tmp_path / "proj" / "sess" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-abc.jsonl").write_text("{}\n")
    d = work_queue.decide(_queue([_item(3, "Three — DEFECT.")]), "sess", False,
                          projects_root=tmp_path, state_path=state_path)
    assert not d.block
    assert "background agent" in d.reason


def test_an_agent_quiet_for_long_enough_no_longer_holds_it_back(tmp_path,
                                                                state_path):
    subs = tmp_path / "proj" / "sess" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-abc.jsonl").write_text("{}\n")
    d = work_queue.decide(_queue([_item(3, "Three — DEFECT.")]), "sess", False,
                          projects_root=tmp_path, state_path=state_path,
                          now=time.time() + work_queue.AGENT_IDLE_SECONDS + 1)
    assert d.block


def test_the_same_item_is_handed_back_at_most_twice(state_path):
    q = _queue([_item(3, "Three — DEFECT.")])
    kw = dict(state_path=state_path, projects_root=Path("/nonexistent"))
    assert work_queue.decide(q, "sess", False, **kw).block
    assert work_queue.decide(q, "sess", True, **kw).block
    third = work_queue.decide(q, "sess", True, **kw)
    assert not third.block
    assert "loop, not diligence" in third.reason


def test_the_brake_is_per_item_not_per_session(state_path):
    """Two attempts at item 3 must not silence item 4."""
    kw = dict(state_path=state_path, projects_root=Path("/nonexistent"))
    three = _queue([_item(3, "Three — DEFECT.")])
    work_queue.decide(three, "sess", False, **kw)
    work_queue.decide(three, "sess", True, **kw)
    assert not work_queue.decide(three, "sess", True, **kw).block
    assert work_queue.decide(_queue([_item(4, "Four — DEFECT.")]),
                             "sess", True, **kw).block


def test_a_state_file_that_cannot_be_written_still_allows_progress(tmp_path):
    """No brake is survivable — the harness caps consecutive blocks at 8.
    A crash here would not be."""
    unwritable = tmp_path / "no-such-dir" / "state.json"
    d = work_queue.decide(_queue([_item(3, "Three — DEFECT.")]), "sess", False,
                          state_path=unwritable,
                          projects_root=Path("/nonexistent"))
    assert d.block


# --- hook plumbing ---------------------------------------------------------

def test_a_malformed_hook_payload_allows_the_stop(capsys):
    assert work_queue.run_hook("not json at all") == 0


def test_an_empty_hook_payload_does_not_crash(monkeypatch):
    monkeypatch.setenv(work_queue.PROMISE_CHECK_ENV, "0")
    monkeypatch.setenv(work_queue.ADVERSARY_CHECK_ENV, "0")
    assert work_queue.run_hook("") in (0, work_queue.BLOCK_EXIT)


def test_the_handback_count_survives_between_turns(state_path):
    work_queue.record_handback("sess", "item 3", state_path)
    work_queue.record_handback("sess", "item 3", state_path)
    assert work_queue.handback_count("sess", "item 3", state_path) == 2
    assert json.loads(state_path.read_text())["sess"]["item 3"] == 2


# --- gap 1: nothing enforced the adversarial review ------------------------
#
# The owner's rule is that no board item closes until the adversary agent has
# argued against closing it. It was skipped on five closures in a row on
# 2026-09-13 because green work produces no signal that it was never
# challenged. These pin the mechanical version of that rule — and, harder,
# pin that a GitHub read which FAILS invents no work.

def _pr(number=1, title="", body="", patch=None, files=("src/x.py",)):
    return OpenPR(number=number, title=title, body=body, files=tuple(files),
                  work_md_patch=patch)


RETIRE_PATCH = ("@@ -970,3 +970,3 @@\n"
                "-**Retired item numbers — never reuse.** 2, 5, 6\n"
                "+**Retired item numbers — never reuse.** 2, 5, 6, 12\n")


def test_editing_the_retired_line_is_a_closure_whatever_the_title_says():
    pr = _pr(title="chore: tidy up", patch=RETIRE_PATCH,
             files=("docs/WORK.md",))
    assert "retires board items" in (work_queue.closes_a_board_item(pr) or "")


def test_naming_an_item_number_in_the_title_is_a_closure():
    assert work_queue.closes_a_board_item(_pr(title="fix(risk): item 18 R/R"))
    assert work_queue.closes_a_board_item(
        _pr(title="Delete three invented seat magnitudes (items 30/31/32)"))
    assert work_queue.closes_a_board_item(_pr(body="Closes item 7."))


def test_citing_an_item_in_passing_is_not_a_closure():
    """Real false positive, caught before shipping: PR 351 mentions item 44
    only to say where a deleted rule came from. Demanding an adversary
    argument for that is the cried wolf this must not become."""
    pr = _pr(title="docs: make README match the desk that actually exists",
             body="`correlation breach` removed from every exit-trigger list "
                  "(deleted 2026-09-13, item 44 — nothing in the desk can "
                  "verify one).")
    assert work_queue.closes_a_board_item(pr) is None


def test_a_change_that_closes_nothing_is_left_alone():
    pr = _pr(title="fix(data): stale price cache", body="No board item.")
    assert work_queue.closes_a_board_item(pr) is None
    assert work_queue.unreviewed_closures([pr]) == []


def test_a_closure_without_an_adversary_line_is_work():
    gaps = work_queue.unreviewed_closures(
        [_pr(number=343, title="fix: item 18", body="Tested. Green.")])
    assert len(gaps) == 1
    assert "343" in gaps[0] and "Adversary" in gaps[0]


def test_a_real_adversary_line_clears_it():
    body = ("Tested.\n\nAdversary: argued the R/R unification hides a real "
            "second gate and that closing this leaves the constructor's own "
            "number unreconciled; checked, it does not.\n")
    assert work_queue.has_adversary_evidence(body)
    assert work_queue.unreviewed_closures(
        [_pr(title="fix: item 18", body=body)]) == []


def test_a_token_adversary_line_does_not_count():
    """'Adversary: yes' is not an argument and must not pass for one."""
    assert not work_queue.has_adversary_evidence("Adversary: yes")
    assert not work_queue.has_adversary_evidence("Adversary: done")


def test_a_failed_github_read_never_manufactures_work(monkeypatch):
    """Not being able to look is not evidence that a review is missing.
    Unauthenticated reads are rate-limited, so this is the common case."""
    monkeypatch.setattr(work_queue, "read_open_pull_requests",
                        lambda *a, **k: ([], "the request limit is used up"))
    assert work_queue.adversary_gaps() == []


def test_a_github_reader_that_throws_never_manufactures_work(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network gone")
    monkeypatch.setattr(work_queue, "read_open_pull_requests", boom)
    assert work_queue.adversary_gaps() == []


def test_an_unreviewed_closure_blocks_even_with_an_empty_backlog(state_path):
    d = work_queue.decide(_queue(), "sess", False, state_path=state_path,
                          projects_root=Path("/nonexistent"),
                          gaps=["PR 343 (...) closes a board item"])
    assert d.block and d.kind == "adversary"


def test_a_live_agent_still_outranks_an_unreviewed_closure(tmp_path,
                                                           state_path):
    """The cost trap outranks every finding: blocking while a subagent writes
    re-sends the whole transcript at $1.59 a firing."""
    subs = tmp_path / "proj" / "sess" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-abc.jsonl").write_text("{}\n")
    d = work_queue.decide(_queue(), "sess", False, projects_root=tmp_path,
                          state_path=state_path, gaps=["PR 343 ..."],
                          promise="Merging now.")
    assert not d.block


def test_the_pr_reader_marks_an_unreadable_file_list_rather_than_guessing():
    """A PR whose files could not be read must not be reported as touching
    nothing — that is how a failed read becomes a false clean bill."""
    calls = []

    def fetch(url, headers):
        calls.append(url)
        if url.endswith("/files?per_page=100"):
            return 403, {}, b""
        body = json.dumps([{"number": 9, "title": "t", "body": "b"}]).encode()
        return 200, {}, body

    prs, problem = read_open_pull_requests(fetch=fetch)
    assert problem is None
    assert prs[0].files is None and prs[0].work_md_patch is None
    assert prs[0].detail_problem


# --- gap 2: a promise the turn made and did not keep -----------------------
#
# Every sentence below is a REAL sentence from this repository's own session
# transcript (session ae440278, 2026-09-13), not an invented one. The
# negative cases are the point: this check fires on ordinary English, and one
# that cries wolf gets switched off, which is worse than not having it.

FIRES = [
    "Dispatching now.",
    "Starting on all of it now.",
    "Writing the doctrine now, then the proxy reads it.",
    "I'll find out where the 40% came from.",
    "Merging now.",
]

DOES_NOT_FIRE = [
    # a question — asking is not promising
    "Want me to dispatch that, or talk through it more first?",
    # conditional
    "If that other session pushes changes to main, I'll need to rebase "
    "anything of mine still open before merging.",
    # waiting on something else to finish
    "I'll confirm here the moment they are.",
    # a promise to communicate: no tool call could have discharged it
    "Nothing needed from you — I'll bring back item 2 as genuinely closed.",
    # somebody else's work
    "#291 and #292 are rebased on the merged drawdown fix, CI running, "
    "and they will merge automatically when green.",
    # past tense — a claim about a tool call, a different defect
    "Dispatched. Nothing needed from you.",
    # a standing habit, not this turn
    "I'll keep research bounded going forward.",
    # a stance, not an action
    "Take your time — I'll keep it simple.",
    "I'll slow down.",
    # a negation
    "Noted — I won't touch WORK.md or INCIDENT_HISTORY.md.",
    # talking ABOUT the check, in quotes
    '"Dispatching now" and "dispatched" look identical in my own transcript.',
    # a glossary line whose bolded gerund reads like an intention
    "- **Reading** — taking the number from what's in front of you right now.",
    # a status report without the word that makes it a commitment
    "Running.",
]


@pytest.mark.parametrize("sentence", FIRES)
def test_a_real_closing_promise_is_caught(sentence):
    assert turn_promises.offending_sentence(sentence) is not None


@pytest.mark.parametrize("sentence", DOES_NOT_FIRE)
def test_real_sentences_that_must_not_be_called_broken_promises(sentence):
    assert turn_promises.offending_sentence(sentence) is None


def test_only_the_closing_line_counts():
    """An intention stated mid-message is usually discharged by the rest of
    the message; one written last has nothing after it."""
    text = "I'll start with what you said.\n\nHere is the answer, in full."
    assert turn_promises.offending_sentence(text) is None


def _transcript(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return p


def _assistant(text=None, tool=None):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool is not None:
        content.append({"type": "tool_use", "name": tool, "input": {}})
    return {"type": "assistant", "message": {"content": content}}


def _user(text="do the thing"):
    return {"type": "user", "message": {"content": text}}


def _tool_result():
    return {"type": "user",
            "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


def test_a_turn_that_did_nothing_and_promised_something_is_caught(tmp_path):
    path = _transcript(tmp_path, [_user(), _assistant(text="Merging now.")])
    assert turn_promises.unkept_promise(path) == "Merging now."


def test_a_turn_that_actually_did_the_work_is_not_caught(tmp_path):
    """'Running now:' written under agents the same turn really dispatched
    is a status report. Calling it a broken promise is the cried wolf."""
    path = _transcript(tmp_path, [
        _user(), _assistant(tool="Agent"), _tool_result(),
        _assistant(text="Running now:")])
    assert turn_promises.unkept_promise(path) is None


def test_a_tool_result_is_not_mistaken_for_the_start_of_the_turn(tmp_path):
    path = _transcript(tmp_path, [
        _user(), _assistant(tool="Bash"), _tool_result(),
        _assistant(text="Dispatching now.")])
    assert turn_promises.unkept_promise(path) is None


def test_a_sidechain_is_not_this_session(tmp_path):
    entry = _assistant(text="Merging now.")
    entry["isSidechain"] = True
    path = _transcript(tmp_path, [_user(), entry])
    assert turn_promises.unkept_promise(path) is None


def test_an_unreadable_transcript_invents_nothing(tmp_path):
    assert turn_promises.unkept_promise(tmp_path / "nope.jsonl") is None
    assert turn_promises.unkept_promise(None) is None
    broken = tmp_path / "broken.jsonl"
    broken.write_text("{not json\n{also not json\n")
    assert turn_promises.unkept_promise(broken) is None


def test_only_the_tail_of_a_huge_transcript_is_read(tmp_path):
    """These files reach 130MB. A Stop hook that loads one is its own outage."""
    entries = [_user()] + [_assistant(text=f"line {i}") for i in range(2000)]
    entries.append(_assistant(text="Merging now."))
    path = _transcript(tmp_path, entries)
    found = turn_promises.final_turn_message(path, tail_lines=5)
    assert found == ("Merging now.", False)


def test_an_unkept_promise_blocks_and_quotes_the_sentence(state_path):
    d = work_queue.decide(_queue(), "sess", False, state_path=state_path,
                          projects_root=Path("/nonexistent"),
                          promise="Dispatching now.")
    assert d.block and d.kind == "promise"
    assert "Dispatching now." in d.reason


def test_the_promise_check_outranks_the_backlog(state_path):
    """It is about THIS turn and costs one sentence to fix."""
    d = work_queue.decide(_queue([_item(3, "Three — DEFECT.")]), "sess", False,
                          state_path=state_path,
                          projects_root=Path("/nonexistent"),
                          promise="Merging now.", gaps=["PR 343 ..."])
    assert d.kind == "promise"


def test_either_check_can_be_switched_off(monkeypatch, tmp_path):
    monkeypatch.setenv(work_queue.PROMISE_CHECK_ENV, "0")
    monkeypatch.setenv(work_queue.ADVERSARY_CHECK_ENV, "off")
    assert not work_queue._enabled(work_queue.PROMISE_CHECK_ENV)
    assert not work_queue._enabled(work_queue.ADVERSARY_CHECK_ENV)
    monkeypatch.delenv(work_queue.PROMISE_CHECK_ENV)
    assert work_queue._enabled(work_queue.PROMISE_CHECK_ENV)


# --- the exit code that actually blocks ------------------------------------

def test_blocking_uses_the_only_code_the_harness_honours(monkeypatch,
                                                         tmp_path):
    """Exit 1 is reported as a hook error and lets the conversation end. This
    returned 1 until 2026-09-13, so even where it loaded it never blocked."""
    assert work_queue.BLOCK_EXIT == 2
    monkeypatch.setenv(work_queue.ADVERSARY_CHECK_ENV, "0")
    monkeypatch.setattr(work_queue, "build_queue", lambda *a, **k: _queue())
    path = _transcript(tmp_path, [_user(), _assistant(text="Merging now.")])
    payload = json.dumps({"session_id": "s", "transcript_path": str(path)})
    assert work_queue.run_hook(str(payload),
                               projects_root=Path("/nonexistent"),
                               state_path=tmp_path / "s.json") == 2


def test_a_payload_that_is_not_an_object_allows_the_stop():
    assert work_queue.run_hook("[1, 2, 3]") == 0


# --- gap 3: the wrapper is safe to install at USER level -------------------
#
# A project-scoped hook only loads for sessions started inside the project
# folder, which is why this had never fired once. Wired globally it runs at
# the end of every session on the machine, so it must be silent and
# harmless in every other repository — and must still pass a block through.

WRAPPER = Path(__file__).resolve().parent.parent / "scripts" / "run_work_queue_hook.sh"

PAYLOAD = json.dumps({"session_id": "s", "transcript_path": "/nope",
                      "hook_event_name": "Stop"})


def _run(cwd: Path):
    return subprocess.run([str(WRAPPER)], cwd=str(cwd), input=PAYLOAD,
                          capture_output=True, text=True, timeout=60)


def _fake_repo(tmp_path: Path, script_exit: int, name: str = "elsewhere") -> Path:
    root = tmp_path / name
    (root / "scripts").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "docs" / "WORK.md").write_text("# not the real backlog\n")
    script = root / "scripts" / "work_queue.py"
    script.write_text("import sys\n"
                      f"sys.exit({script_exit})\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True,
                   capture_output=True)
    return root


def test_a_session_outside_any_repository_is_left_completely_alone(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    r = _run(plain)
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == ""


def test_another_project_on_this_machine_is_left_completely_alone(tmp_path):
    """A git repository that is not this one: no output, no exit code, no
    trace. This is the whole precondition for wiring it at user level."""
    other = tmp_path / "someone-elses-project"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True,
                   capture_output=True)
    (other / "README.md").write_text("hello\n")
    r = _run(other)
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == ""


def test_a_repository_missing_the_backlog_is_left_alone(tmp_path):
    """Both marker files are required — one of them is not this repository."""
    half = tmp_path / "half"
    (half / "scripts").mkdir(parents=True)
    (half / "scripts" / "work_queue.py").write_text("import sys; sys.exit(2)\n")
    subprocess.run(["git", "init", "-q", str(half)], check=True,
                   capture_output=True)
    assert _run(half).returncode == 0


def test_inside_this_repository_a_block_is_passed_through(tmp_path):
    assert _run(_fake_repo(tmp_path, 2)).returncode == 2


def test_a_crashing_checker_never_holds_a_session_hostage(tmp_path):
    """Exit 1 from a traceback used to be the blocking code. It must not be."""
    assert _run(_fake_repo(tmp_path, 1, "crash")).returncode == 0
    assert _run(_fake_repo(tmp_path, 3, "unreadable")).returncode == 0
