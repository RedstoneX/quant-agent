"""The desk-side floor must refuse a session it cannot finish, and be called at start."""
from collections import namedtuple
from pathlib import Path

from src import desk_disk_floor as dg

Usage = namedtuple("Usage", "total used free")
GIB = 2**30


# --- the desk-side floor and its call site --------------------------------

def test_desk_floor_is_the_measured_arithmetic():
    """The floor is two worst-days plus two log ceilings, not a round number."""
    assert dg.DESK_FLOOR_BYTES == 2 * (dg.DESK_WORST_DAY_BYTES + dg.DESK_LOG_CEILING_BYTES)
    assert dg.DESK_LOG_CEILING_BYTES == 6 * 10 * 2**20  # main.py: 10 MiB x (1 + 5)


def test_still_breached_after_reclaim_refuses_with_the_numbers(monkeypatch, tmp_path):
    monkeypatch.setattr(dg.shutil, "disk_usage", lambda p: Usage(100 * GIB, 100 * GIB, 1))
    try:
        dg.require_desk_disk(tmp_path)
    except SystemExit as exc:
        assert "REFUSING TO START" in str(exc)
        assert "378,598,682" in str(exc)  # what it needs
        assert "0.0 GiB free" in str(exc)  # what it found
    else:
        raise AssertionError("a breached floor must refuse the session")


def test_cannot_measure_refuses(monkeypatch, tmp_path):
    def boom(_p):
        raise OSError("no statfs")
    monkeypatch.setattr(dg.shutil, "disk_usage", boom)
    try:
        dg.require_desk_disk(tmp_path)
    except SystemExit as exc:
        assert "CANNOT MEASURE" in str(exc)
    else:
        raise AssertionError("an unmeasurable filesystem must refuse")


def test_session_start_calls_the_disk_check_before_anything_reads_or_writes():
    """A guard nobody invokes is reassurance. Fail if the call site goes away."""
    import ast
    main_py = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(main_py.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.setdefault(node.func.id, node.lineno)
    assert "require_desk_disk" in called, (
        "main() no longer calls require_desk_disk at session start")
    assert "load_config" in called, "anchor moved; re-check this test"
    assert called["require_desk_disk"] < called["load_config"], (
        "the disk check must run before the session reads or writes anything")
