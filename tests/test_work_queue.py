"""The work queue's job is to be RELUCTANT.

Every test below that matters is a test that it allowed the session to
stop. The owner's constraint is not "never miss an item" — it is "never
burn a session in a loop" — so the cases pinned hardest are the ones where
something is uncertain and the correct answer is to let go.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scripts import work_queue
from scripts.status_board import QueueItem


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


def test_an_empty_hook_payload_does_not_crash():
    assert work_queue.run_hook("") in (0, 1)


def test_the_handback_count_survives_between_turns(state_path):
    work_queue.record_handback("sess", "item 3", state_path)
    work_queue.record_handback("sess", "item 3", state_path)
    assert work_queue.handback_count("sess", "item 3", state_path) == 2
    assert json.loads(state_path.read_text())["sess"]["item 3"] == 2
