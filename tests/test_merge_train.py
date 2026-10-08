"""The merge train sends exactly one change to test at a time, oldest first."""
from scripts.merge_train import plan


def _pr(n, state, created, armed=True, running=False, failed=False, draft=False):
    checks = []
    if running:
        checks.append({"status": "IN_PROGRESS"})
    elif failed:
        checks.append({"status": "COMPLETED", "conclusion": "FAILURE"})
    else:
        checks.append({"status": "COMPLETED", "conclusion": "SUCCESS"})
    return {"number": n, "mergeStateStatus": state, "createdAt": created, "isDraft": draft,
            "autoMergeRequest": {"enabledAt": created} if armed else None, "statusCheckRollup": checks}


def test_busy_while_a_current_change_is_testing():
    action, prs = plan([_pr(1, "BEHIND", "2026-10-01"), _pr(2, "BLOCKED", "2026-10-02", running=True)])
    assert action == "busy" and prs[0]["number"] == 2


def test_advances_oldest_behind_change_first():
    action, prs = plan([_pr(3, "BEHIND", "2026-10-03"), _pr(1, "BEHIND", "2026-10-01")])
    assert action == "advance" and [p["number"] for p in prs] == [1, 3]


def test_a_red_current_change_does_not_stall_the_train():
    action, prs = plan([_pr(1, "BLOCKED", "2026-10-01", failed=True), _pr(2, "BEHIND", "2026-10-02")])
    assert action == "advance" and prs[0]["number"] == 2


def test_unarmed_and_draft_changes_are_left_alone():
    action, _ = plan([_pr(1, "BEHIND", "2026-10-01", armed=False), _pr(2, "BEHIND", "2026-10-02", draft=True)])
    assert action == "idle"


def test_a_behind_change_with_an_old_run_still_going_is_not_busy():
    # Its run tests a stale main; the train must still bring it up to date.
    action, prs = plan([_pr(1, "BEHIND", "2026-10-01", running=True)])
    assert action == "advance" and prs[0]["number"] == 1


def test_board_edit_with_code_is_refused():
    from scripts.check_board_edits_alone import violation
    assert violation(["docs/WORK.md", "src/pipeline.py"]) == ["src/pipeline.py"]


def test_board_edit_alone_or_with_docs_is_allowed():
    from scripts.check_board_edits_alone import violation
    assert violation(["docs/WORK.md", "docs/board_notes/item-044.md"]) == []
    assert violation(["src/pipeline.py"]) == []




def test_stop_hook_hands_back_a_stuck_change_before_new_work(monkeypatch):
    from scripts import work_queue as wq
    q = type("Q", (), {"actionable": [object()], "unreadable": False, "next_item": None})()
    monkeypatch.setattr(wq, "running_agents", lambda *a, **k: [])
    d = wq.decide(q, None, False, open_changes=(["Open change #7 (x) conflicts with main;"], 1))
    assert d.block and d.kind == "finish" and "#7" in d.reason


def test_stop_hook_starts_nothing_new_while_a_change_is_merging(monkeypatch):
    from scripts import work_queue as wq
    q = type("Q", (), {"actionable": [object()], "unreadable": False, "next_item": None})()
    monkeypatch.setattr(wq, "running_agents", lambda *a, **k: [])
    assert not wq.decide(q, None, False, open_changes=([], 2)).block
    assert not wq.decide(q, None, False, open_changes=None).block
