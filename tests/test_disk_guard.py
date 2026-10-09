"""disk_guard must refuse when tight and when it cannot measure."""

import importlib.util
from collections import namedtuple
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "disk_guard", Path(__file__).resolve().parents[1] / "scripts" / "disk_guard.py"
)
dg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dg)

Usage = namedtuple("Usage", "total used free")
GIB = 2**30


def _run(monkeypatch, tmp_path, usage):
    monkeypatch.setattr(dg.shutil, "disk_usage", usage)
    monkeypatch.setattr(dg, "checkout_bytes", lambda *a, **k: 52 * 2**20)
    return dg.main(["--scratch", str(tmp_path)])


def test_passes_with_plenty_of_space(monkeypatch, tmp_path, capsys):
    assert _run(monkeypatch, tmp_path, lambda p: Usage(100 * GIB, 10 * GIB, 90 * GIB)) == 0
    assert "ok" in capsys.readouterr().out


def test_refuses_when_space_tight(monkeypatch, tmp_path, capsys):
    (tmp_path / "big").mkdir()
    (tmp_path / "big" / "f").write_bytes(b"x" * 1000)
    assert _run(monkeypatch, tmp_path, lambda p: Usage(100 * GIB, 99 * GIB, 1 * GIB)) == 1
    err = capsys.readouterr().err
    assert "LOW DISK" in err and "1.0 GiB free" in err and "1.0%" in err
    assert str(tmp_path / "big") in err


def test_refuses_when_filesystem_unreadable(monkeypatch, tmp_path, capsys):
    def boom(p):
        raise OSError("no such device")

    assert _run(monkeypatch, tmp_path, boom) == 2
    assert "CANNOT MEASURE" in capsys.readouterr().err


def test_refuses_when_path_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(dg, "checkout_bytes", lambda *a, **k: 52 * 2**20)
    assert dg.main(["--scratch", str(tmp_path / "nope")]) == 2
