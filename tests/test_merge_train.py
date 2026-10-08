"""Board-edits-alone and stop-hook finish-first rules."""


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
