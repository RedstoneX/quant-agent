"""The merge-driver registration guard: bites on a missing driver, spares built-ins."""

import subprocess
from pathlib import Path

from scripts import check_merge_drivers as cmd

REPO = Path(__file__).resolve().parent.parent


def _tmp_repo(tmp_path, attrs: str) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitattributes").write_text(attrs)
    return tmp_path


def test_real_clone_has_every_driver_registered():
    assert cmd.unregistered(REPO) == []


def test_unregistered_custom_driver_fails(tmp_path):
    repo = _tmp_repo(tmp_path, "a.md merge=somedriver\n")
    assert cmd.main([str(repo)]) == 1


def test_registered_driver_passes(tmp_path):
    repo = _tmp_repo(tmp_path, "a.md merge=somedriver\n")
    subprocess.run(["git", "-C", str(repo), "config", "merge.somedriver.driver", "true %A"], check=True)
    assert cmd.main([str(repo)]) == 0


def test_builtin_union_needs_no_registration(tmp_path):
    repo = _tmp_repo(tmp_path, "a.yaml merge=union\nb binary merge=text\n")
    assert cmd.main([str(repo)]) == 0


def test_driver_pointing_at_missing_script_fails(tmp_path):
    repo = _tmp_repo(tmp_path, "a.md merge=somedriver\n")
    subprocess.run(["git", "-C", str(repo), "config", "merge.somedriver.driver", "scripts/gone.sh %A"], check=True)
    assert cmd.main([str(repo)]) == 1
